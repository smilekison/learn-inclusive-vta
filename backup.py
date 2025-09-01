import os
import re
import uuid
import tempfile
from typing import List

import gradio as gr
import gradio_client.utils

from fastapi import Response

os.environ["GRADIO_DISABLE_API_INFO"] = "1"

# Monkeypatch to bypass JSON schema inspection

def _no_schema(*args, **kwargs):
    return "Any"
gradio_client.utils.json_schema_to_python_type = _no_schema

# ---------- Minimal “glossing” rules ----------
GLOSS_DICT = {
    "hello": "HELLO",
    "welcome": "WELCOME",
    "thank": "THANK",
    "you": "YOU",
    "thankyou": "THANKYOU",  # allow single-token variant
    "i": "I",
    "am": "AM",
    "smile": "SMILE",
    # extend as needed
}

def _norm(word: str) -> str:
    return re.sub(r"[^a-zA-Z]", "", word).lower()

def text_to_glosses(text: str, logs: List[str]) -> List[str]:
    words = text.split()
    i = 0
    glosses: List[str] = []
    while i < len(words):
        w = _norm(words[i])
        # merge phrase “thank you” → THANKYOU
        if i < len(words) - 1:
            w2 = _norm(words[i + 1])
            if (w, w2) == ("thank", "you"):
                glosses.append("THANKYOU")
                i += 2
                continue
        if w in GLOSS_DICT:
            glosses.append(GLOSS_DICT[w])
        else:
            if w:
                logs.append(f"[gloss] skipped unknown: {w}")
        i += 1
    logs.append(f"[gloss] sequence: {glosses}")
    return glosses

def glosses_to_sigml(glosses: List[str]) -> str:
    lines = ["<sigml>"]
    for g in glosses:
        lines.append(f'  <sign gloss="{g}"/>')
    lines.append("</sigml>")
    return "\n".join(lines)

# ---------- Storage for served SiGML ----------
APP_ROOT = os.path.abspath(os.path.dirname(__file__))
SIGML_STORE_DIR = os.path.join(APP_ROOT, "sigml_store")
os.makedirs(SIGML_STORE_DIR, exist_ok=True)

# ---------- Core app function ----------
def generate_sigml_from_text(text: str, progress=gr.Progress(track_tqdm=False)):
    logs: List[str] = []
    text = (text or "").strip()

    if not text:
        return (
            "<div style='color:#b58900'>Please enter some text.</div>",
            "",   # SiGML text
            None, # download file
            "",   # player html
            ""
        )

    # Step 1: normalize + glossing
    progress(0.2, desc="Parsing & glossing…")
    logs.append("[flow] Start text → gloss → SiGML")
    glosses = text_to_glosses(text, logs)
    if not glosses:
        return (
            "<div style='color:#dc2626'>No known glosses found in the input text.</div>",
            "",
            None,
            "",
            "\n".join(logs),
        )

    # Step 2: produce SiGML
    progress(0.55, desc="Building SiGML…")
    sigml_text = glosses_to_sigml(glosses)
    logs.append("[sigml] document built")

    # Step 3: write files (download + served URL)
    progress(0.8, desc="Saving…")
    # a) temp file for download widget
    tmp = tempfile.NamedTemporaryFile(suffix=".sigml", delete=False, mode="w", encoding="utf-8")
    tmp.write(sigml_text)
    tmp_path = tmp.name
    tmp.close()
    logs.append(f"[file] temp (download): {tmp_path}")

    # b) persistent file for player route
    file_id = uuid.uuid4().hex
    store_path = os.path.join(SIGML_STORE_DIR, f"{file_id}.sigml")
    with open(store_path, "w", encoding="utf-8") as f:
        f.write(sigml_text)
    logs.append(f"[file] stored for player: {store_path}")

    # Step 4: inline player (iframe → UEA player, fed by our served URL)
    # Build iframe whose src is set client-side so it uses the current origin.
    # The /sigml/<id>.sigml route below serves the file over HTTPS via Nginx.
    player_html = f"""
    <div class="player-wrap">
      <iframe id="sigml_iframe" width="480" height="480" frameborder="0"
              title="SiGML Player (UEA)"></iframe>
      <div class="hint">If the player doesn't load immediately, check that HTTPS is enabled on this domain.</div>
    </div>
    <script>
    (function() {{
      var sigmlUrl = window.location.origin + "/sigml/{file_id}.sigml";
      var player = document.getElementById("sigml_iframe");
      var src = "https://vh.cmp.uea.ac.uk/player?sigml_url=" + encodeURIComponent(sigmlUrl);
      player.src = src;
    }})();
    </script>
    """.strip()

    progress(1.0, desc="Done")
    # Minimal visual gloss preview
    badges = " ".join(f"<span class='pill'>{g}</span>" for g in glosses)
    preview_html = f"""
    <div class="wrap">
      <div class="title">Gloss sequence</div>
      <div class="badges">{badges}</div>
    </div>
    """

    return preview_html, sigml_text, tmp_path, player_html, "\n".join(logs)

# ---------- Minimalist UI ----------
CUSTOM_CSS = """
.wrap { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; }
.title { font-size: 14px; color: #374151; margin-bottom: 8px; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; }
.pill { font-size: 12px; padding: 6px 10px; border-radius: 999px; background: #111827; color: white; letter-spacing: 0.3px; }
.hint { margin-top: 8px; font-size: 12px; color: #6b7280; }
.player-wrap { margin-top: 8px; }
"""

with gr.Blocks(css=CUSTOM_CSS, title="Text → SiGML (Inline Player)") as demo:
    gr.Markdown("### ✨ Text → Sign Language (SiGML) — Inline Player")

    text_in = gr.Textbox(
        label="Your text",
        placeholder="e.g., Welcome and hello, I am Smile. Thank you",
        lines=3,
    )
    btn = gr.Button("Generate & Play", variant="primary")

    preview = gr.HTML(label="Gloss Preview")
    sigml_out = gr.Textbox(label="SiGML", lines=10)
    download = gr.File(label="Download .sigml")
    player = gr.HTML(label="Player")
    logs = gr.Textbox(label="Debug log", lines=10)

    btn.click(
        generate_sigml_from_text,
        inputs=[text_in],
        outputs=[preview, sigml_out, download, player, logs],
    )

# ---------- Add FastAPI route to serve saved .sigml ----------
app = demo.app  # FastAPI instance underneath Gradio

@app.get("/sigml/{filename:path}")
def serve_sigml(filename: str):
    # only serve .sigml inside our store dir
    safe_name = os.path.basename(filename)
    path = os.path.join(SIGML_STORE_DIR, safe_name)
    if os.path.isfile(path) and path.endswith(".sigml"):
        with open(path, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="application/xml")
    return Response(status_code=404)

# ---------- Launch ----------
if __name__ == "__main__":
    demo.queue()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
        show_api=False,
        inbrowser=False,
        app_kwargs={"docs_url": None, "redoc_url": None},
    )
