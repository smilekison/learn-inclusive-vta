import os
import tempfile
import whisper
import boto3
import gradio as gr
import subprocess
import shutil

# Load Whisper model
model = whisper.load_model("base")

# AWS S3 client
s3_client = boto3.client("s3")

# Path to ffmpeg/ffprobe fallback (update if needed)
FFMPEG_FALLBACK = r"C:\\ffmpeg\\bin\\ffmpeg.exe"
FFPROBE_FALLBACK = r"C:\\ffmpeg\\bin\\ffprobe.exe"


def get_ffmpeg_cmd():
    return shutil.which("ffmpeg") or (FFMPEG_FALLBACK if os.path.exists(FFMPEG_FALLBACK) else None)


def get_ffprobe_cmd():
    return shutil.which("ffprobe") or (FFPROBE_FALLBACK if os.path.exists(FFPROBE_FALLBACK) else None)


def extract_audio(video_path, output_audio_path):
    ffmpeg_cmd = get_ffmpeg_cmd()
    if ffmpeg_cmd is None:
        raise FileNotFoundError("ffmpeg not found. Please install ffmpeg and add it to PATH, or update FFMPEG_FALLBACK.")

    # Ensure output path has correct extension
    if not output_audio_path.endswith(".wav"):
        output_audio_path += ".wav"

    # Check if video has audio streams using ffprobe
    ffprobe_cmd = get_ffprobe_cmd()
    if ffprobe_cmd:
        probe = subprocess.run([
            ffprobe_cmd, "-i", video_path, "-show_streams", "-select_streams", "a", "-loglevel", "error"
        ], capture_output=True, text=True)
        if not probe.stdout.strip():
            raise RuntimeError("No audio stream found in this video file.")

    # Extract first audio stream
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

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if os.path.getsize(output_audio_path) == 0:
            raise RuntimeError("ffmpeg produced an empty audio file. The audio stream may be invalid.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg failed. STDERR: {e.stderr}")


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


def transcribe_video(video_path, upload_to_s3=False, bucket_name=None):
    # Extract audio to WAV
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_audio:
        audio_path = tmp_audio.name
    try:
        extract_audio(video_path, audio_path)
    except RuntimeError as e:
        return f"Error: {e}", None

    # Force Whisper to use ffmpeg fallback path if needed
    os.environ["PATH"] += os.pathsep + os.path.dirname(FFMPEG_FALLBACK)

    # Transcription
    result = model.transcribe(audio_path, task="transcribe")
    transcription_with_timestamps = []
    for segment in result["segments"]:
        start, end, text = segment["start"], segment["end"], segment["text"]
        transcription_with_timestamps.append(f"[{start:.2f} - {end:.2f}] {text}")
    transcription_text = "\n".join(transcription_with_timestamps)

    # Save SRT file
    srt_path = tempfile.NamedTemporaryFile(suffix=".srt", delete=False).name
    save_srt(result, srt_path)

    # Optional S3 upload (video + srt)
    s3_urls = []
    if upload_to_s3 and bucket_name:
        video_filename = os.path.basename(video_path)
        srt_filename = os.path.splitext(video_filename)[0] + ".srt"

        s3_client.upload_file(video_path, bucket_name, video_filename)
        s3_client.upload_file(srt_path, bucket_name, srt_filename)

        s3_urls.append(f"s3://{bucket_name}/{video_filename}")
        s3_urls.append(f"s3://{bucket_name}/{srt_filename}")

    return transcription_text, "\n".join(s3_urls) if s3_urls else None


# Minimalistic Gradio webapp
demo = gr.Interface(
    fn=transcribe_video,
    inputs=[
        gr.Video(label="Upload Video"),
        gr.Checkbox(label="Upload to S3"),
        gr.Textbox(label="S3 Bucket", placeholder="my-bucket"),
    ],
    outputs=[
        gr.Textbox(label="Transcription", lines=15),
        gr.Textbox(label="S3 URLs"),
    ],
    title="🎙️ Video Transcriber",
    description="Upload a video to get audio transcription with timestamps. Optionally upload video + subtitles to AWS S3.",
    theme="default"
)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
