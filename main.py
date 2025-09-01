import os
import tempfile
import whisper
import boto3
import gradio as gr
import subprocess
import shutil
import re

# Load Whisper model
model = whisper.load_model("small")  # better accuracy

# AWS S3 client
s3_client = boto3.client("s3")

# Path to ffmpeg/ffprobe fallback
FFMPEG_FALLBACK = r"C:\\ffmpeg\\bin\\ffmpeg.exe"
FFPROBE_FALLBACK = r"C:\\ffmpeg\\bin\\ffprobe.exe"

# Simple English-to-gloss dictionary (extendable)
GLOSS_DICT = {
    "hello": "HELLO",
    "welcome": "WELCOME",
    "thankyou": "THANKYOU",
    "thank": "THANKYOU",
    "you": "YOU",
    "smile": "SMILE",
    "i": "I",
    "am": "AM",
}


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


def normalize_word(w: str):
    return re.sub(r"[^a-zA-Z]", "", w).lower()


def text_to_gloss(text: str):
    words = text.split()
    glosses = []
    for w in words:
        key = normalize_word(w)
        if key in ("thank", "you") and "thankyou" in GLOSS_DICT:
            glosses.append("THANKYOU")
        elif key in GLOSS_DICT:
            glosses.append(GLOSS_DICT[key])
    return glosses


def build_sigml(glosses):
    sigml_content = "<sigml>\n"
    for g in glosses:
        sigml_content += f"  <sign gloss=\"{g}\"/>\n"
    sigml_content += "</sigml>"
    return sigml_content


def generate_sign_avatar(text: str):
    glosses = text_to_gloss(text)
    if not glosses:
        return None, "<div style='color:red'>No glosses found for input text.</div>"

    sigml_content = build_sigml(glosses)
    sigml_file = tempfile.NamedTemporaryFile(suffix=".sigml", delete=False, mode="w", encoding="utf-8")
    sigml_file.write(sigml_content)
    sigml_file.close()

    # For MVP: Upload to S3 (public-read) so JASigning player can access
    bucket_name = os.environ.get("SIGN_BUCKET")
    sigml_url = None
    if bucket_name:
        key = os.path.basename(sigml_file.name)
        try:
            s3_client.upload_file(sigml_file.name, bucket_name, key, ExtraArgs={'ACL': 'public-read'})
            sigml_url = f"https://{bucket_name}.s3.amazonaws.com/{key}"
        except Exception as e:
            return None, f"<div style='color:red'>Failed to upload sigml: {e}</div>"

    # Embed JASigning player iframe with sigml URL
    if sigml_url:
        iframe = f"""
        <iframe src='https://vh.cmp.uea.ac.uk/player?sigml_url={sigml_url}'
                width='400' height='400' frameborder='0'></iframe>
        """
        return sigml_file.name, iframe
    else:
        return sigml_file.name, "<div style='color:red'>SigML file generated locally but no hosting available. Set SIGN_BUCKET env var to host.</div>"


def transcribe_video(video_path, upload_to_s3=False, bucket_name=None, text_input=""):
    if text_input.strip():
        _, iframe_html = generate_sign_avatar(text_input)
        return text_input, None, None, iframe_html

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

    s3_urls = []
    if upload_to_s3 and bucket_name:
        video_filename = os.path.basename(video_path)
        try:
            s3_client.upload_file(video_path, bucket_name, video_filename)
            s3_urls.append(f"s3://{bucket_name}/{video_filename}")
        except Exception:
            pass

    _, iframe_html = generate_sign_avatar(" ".join([seg["text"] for seg in result["segments"]]))

    return transcription_text, "\n".join(s3_urls) if s3_urls else None, None, iframe_html


# Gradio webapp
demo = gr.Interface(
    fn=transcribe_video,
    inputs=[
        gr.Video(label="Upload Video"),
        gr.Checkbox(label="Upload to S3"),
        gr.Textbox(label="S3 Bucket", placeholder="my-bucket"),
        gr.Textbox(label="Text to Sign (optional)", placeholder="Type text here to generate sign avatar"),
    ],
    outputs=[
        gr.Textbox(label="Transcription", lines=15),
        gr.Textbox(label="S3 URLs"),
        gr.Video(label="Original Video"),
        gr.HTML(label="Sign Language Avatar"),
    ],
    title="🎙️ Video Transcriber + Sign Avatar",
    description="Upload a video or text to get transcription and view a signing avatar generated from gloss using JASigning (via SiGML).",
    theme="default"
)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
