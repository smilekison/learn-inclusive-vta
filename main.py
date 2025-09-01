import os
import tempfile
import whisper
import boto3
import gradio as gr
import subprocess
import shutil
import re
import json

# Load Whisper model
model = whisper.load_model("small")  # better accuracy

# AWS S3 client
s3_client = boto3.client("s3")

# Path to ffmpeg/ffprobe fallback
FFMPEG_FALLBACK = r"C:\\ffmpeg\\bin\\ffmpeg.exe"
FFPROBE_FALLBACK = r"C:\\ffmpeg\\bin\\ffprobe.exe"

SIGNS_DIR = "signs"


def get_ffmpeg_cmd():
    return shutil.which("ffmpeg") or (FFMPEG_FALLBACK if os.path.exists(FFMPEG_FALLBACK) else None)


def get_ffprobe_cmd():
    return shutil.which("ffprobe") or (FFPROBE_FALLBACK if os.path.exists(FFPROBE_FALLBACK) else None)


def extract_audio(video_path, output_audio_path):
    ffmpeg_cmd = get_ffmpeg_cmd()
    if ffmpeg_cmd is None:
        raise FileNotFoundError("ffmpeg not found. Please install ffmpeg and add it to PATH, or update fallback path.")

    if not output_audio_path.endswith(".wav"):
        output_audio_path += ".wav"

    ffprobe_cmd = get_ffprobe_cmd()
    if ffprobe_cmd:
        probe = subprocess.run([
            ffprobe_cmd, "-i", video_path, "-show_streams", "-select_streams", "a", "-loglevel", "error"
        ], capture_output=True, text=True)
        if not probe.stdout.strip():
            raise RuntimeError("No audio stream found in this video file.")

    command = [
        ffmpeg_cmd,
        "-i", video_path,
        "-map", "0:a:0",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        output_audio_path,
        "-y"
    ]

    subprocess.run(command, check=True, capture_output=True, text=True)
    if os.path.getsize(output_audio_path) == 0:
        raise RuntimeError("ffmpeg produced an empty audio file. The audio stream may be invalid.")


def save_srt(transcription_result, srt_path):
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, segment in enumerate(transcription_result["segments"], start=1):
            start = segment["start"]
            end = segment["end"]
            text = segment["text"].strip()
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(start)} --> {format_timestamp(end)}\n")
            f.write(f"{text}\n\n")


def format_timestamp(seconds: float):
    millis = int((seconds % 1) * 1000)
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    return f"{hrs:02}:{mins:02}:{secs:02},{millis:03}"


def normalize_word(w: str):
    return re.sub(r"[^a-zA-Z]", "", w).lower()


def build_sign_video_from_text(text: str, segments=None, output_path="sign_output.mp4"):
    ffmpeg_cmd = get_ffmpeg_cmd()
    if not os.path.isdir(SIGNS_DIR):
        return None, "<div style='color:red'>No signs directory found.</div>"

    raw_words = text.split()
    norm_words = [normalize_word(w) for w in raw_words if normalize_word(w)]

    # handle bigrams like "thank you"
    i = 0
    mapped = []
    while i < len(norm_words):
        if i < len(norm_words) - 1:
            bigram = norm_words[i] + norm_words[i + 1]
            bigram_path = os.path.join(SIGNS_DIR, f"{bigram}.gif")
            if os.path.exists(bigram_path):
                mapped.append((bigram, bigram_path))
                i += 2
                continue
        fname = os.path.join(SIGNS_DIR, f"{norm_words[i]}.gif")
        mapped.append((norm_words[i], fname))
        i += 1

    clips, durations, timeline = [], [], []
    base_dur = 2.0

    if segments:
        total_duration = sum(seg["end"] - seg["start"] for seg in segments)
        per_word = total_duration / max(1, len(mapped))
    else:
        per_word = base_dur

    current_time = 0.0
    for w, path in mapped:
        if os.path.exists(path):
            clips.append(path)
            durations.append(per_word)
            timeline.append({
                "word": w,
                "file": os.path.basename(path),
                "start": current_time,
                "end": current_time + per_word
            })
            current_time += per_word

    if not clips:
        return None, "<div style='color:red'>No matching sign GIFs found.</div>"

    txtlist = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
    for clip, dur in zip(clips, durations):
        txtlist.write(f"file '{os.path.abspath(clip)}'\n")
        txtlist.write(f"duration {dur}\n")
    txtlist.close()

    cmd = [ffmpeg_cmd, "-f", "concat", "-safe", "0", "-i", txtlist.name,
           "-vf", "scale=320:-1,fps=10", "-pix_fmt", "yuv420p", output_path, "-y"]
    subprocess.run(cmd, check=True)

    # build HTML timeline
    timeline_json = json.dumps(timeline)
    html = f"""
    <style>
      .word-list span {{padding:4px; margin:2px; display:inline-block; border-radius:6px;}}
      .active-word {{ background: yellow; font-weight: bold; }}
    </style>
    <div class='word-list' id='word-list'></div>
    <script>
      const timeline = {timeline_json};
      const video = document.querySelector("video");
      const container = document.getElementById("word-list");
      container.innerHTML = timeline.map((t,i)=>`<span id=word${{i}}>${{t.word}}</span>`).join(" ");
      if(video){{
        video.addEventListener("timeupdate", ()=>{{
          const ct = video.currentTime;
          timeline.forEach((t,i)=>{{
            const el = document.getElementById("word"+i);
            if(ct>=t.start && ct<t.end){{
              el.classList.add("active-word");
            }} else {{
              el.classList.remove("active-word");
            }}
          }});
        }});
      }}
    </script>
    """

    return output_path, html


def transcribe_video(video_path, upload_to_s3=False, bucket_name=None, text_input=""):
    if text_input.strip():
        sign_video, debug_html = build_sign_video_from_text(text_input)
        return text_input, None, sign_video, debug_html

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_audio:
        audio_path = tmp_audio.name
    extract_audio(video_path, audio_path)

    os.environ["PATH"] += os.pathsep + os.path.dirname(FFMPEG_FALLBACK)

    result = model.transcribe(audio_path, task="transcribe", language="en", temperature=0)
    transcription_with_timestamps = []
    for segment in result["segments"]:
        start, end, text = segment["start"], segment["end"], segment["text"]
        transcription_with_timestamps.append(f"[{start:.2f} - {end:.2f}] {text}")
    transcription_text = "\n".join(transcription_with_timestamps)

    srt_path = tempfile.NamedTemporaryFile(suffix=".srt", delete=False).name
    save_srt(result, srt_path)

    s3_urls = []
    if upload_to_s3 and bucket_name:
        video_filename = os.path.basename(video_path)
        srt_filename = os.path.splitext(video_filename)[0] + ".srt"
        try:
            s3_client.upload_file(video_path, bucket_name, video_filename)
            s3_client.upload_file(srt_path, bucket_name, srt_filename)
            s3_urls.append(f"s3://{bucket_name}/{video_filename}")
            s3_urls.append(f"s3://{bucket_name}/{srt_filename}")
        except Exception:
            pass

    all_text = " ".join([seg["text"] for seg in result["segments"]])
    sign_video, debug_html = build_sign_video_from_text(all_text, segments=result["segments"])

    return transcription_text, "\n".join(s3_urls) if s3_urls else None, sign_video, debug_html


# Gradio webapp
demo = gr.Interface(
    fn=transcribe_video,
    inputs=[
        gr.Video(label="Upload Video"),
        gr.Checkbox(label="Upload to S3"),
        gr.Textbox(label="S3 Bucket", placeholder="my-bucket"),
        gr.Textbox(label="Text to Sign (optional)", placeholder="Type text here to generate sign video directly"),
    ],
    outputs=[
        gr.Textbox(label="Transcription", lines=15),
        gr.Textbox(label="S3 URLs"),
        gr.Video(label="Sign Language Video"),
        gr.HTML(label="Live Sign Debug"),
    ],
    title="🎙️ Video Transcriber + Live Sign Generator",
    description="Upload a video or type text to get transcription and a synchronized sign-language video using local GIFs. The debug log highlights each sign word live as the video plays.",
    theme="default"
)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
