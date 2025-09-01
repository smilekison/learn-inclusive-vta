import os
from uuid import uuid4
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import FileResponse

# -------------------------------
# Setup persistent SiGML storage
# -------------------------------
SIGML_DIR = "./sigml"
os.makedirs(SIGML_DIR, exist_ok=True)

# -------------------------------
# Helper: build SiGML from text
# (very simple placeholder logic)
# -------------------------------
def text_to_sigml(text: str) -> str:
    words = text.strip().split()
    sigml_parts = []
    for word in words:
        sigml_parts.append(
            f'<hamnosys_sign gloss="{word.upper()}">🤟 {word}</hamnosys_sign>'
        )
    return f"<sigml>{''.join(sigml_parts)}</sigml>"

# -------------------------------
# Main function for Gradio
# -------------------------------
def generate_sigml(text: str):
    if not text.strip():
        return "⚠️ Please enter some text", None, None

    # Generate sigml content
    sigml_content = text_to_sigml(text)

    # Save file persistently
    filename = f"{uuid4().hex}.sigml"
    sigml_path = os.path.join(SIGML_DIR, filename)
    with open(sigml_path, "w") as f:
        f.write(sigml_content)

    # Public URL (served by FastAPI + nginx)
    public_url = f"https://learn.learninclusive.com/sigml/{filename}"

    # Player iframe
    player_url = f"/player/?sigml_url=https://learn.learninclusive.com/sigml/{filename}"
    player_html = f'<iframe src="{player_url}" width="640" height="480" frameborder="0"></iframe>'


#    player_url = f"https://vh.cmp.uea.ac.uk/player/?sigml_url=https://learn.learninclusive.com/sigml/{filename}"
#    iframe_html = f'<iframe src="{player_url}" width="600" height="400"></iframe>'

    return sigml_content, public_url
# , iframe_html


# -------------------------------
# Build Gradio UI
# -------------------------------
with gr.Blocks(title="Text → SiGML") as demo:
    gr.Markdown("## 🖐️ Text to Sign Language (SiGML)")

    with gr.Row():
        with gr.Column():
            text_input = gr.Textbox(
                label="Enter Text", placeholder="Type something..."
            )
            run_btn = gr.Button("Generate Sign Language")

        with gr.Column():
            # Use Textbox instead of Code for compatibility
            sigml_output = gr.Textbox(
                label="Generated SiGML",
                lines=10,
                max_lines=20,
                interactive=False
            )
            file_url = gr.Textbox(label="Public URL", interactive=False)
            sign_player = gr.HTML(label="Sign Language Player")

    # ✅ Correct indentation (aligned with gr.Row, not inside it)
    run_btn.click(
        generate_sigml,
        inputs=[text_input],
        outputs=[sigml_output, file_url, sign_player],
    )



# -------------------------------
# Mount Gradio into FastAPI
# -------------------------------
app = FastAPI()
app = gr.mount_gradio_app(app, demo, path="/")

@app.get("/sigml/{filename}")
async def serve_sigml(filename: str):
    path = os.path.join(SIGML_DIR, filename)
    if os.path.exists(path):
        return FileResponse(path, media_type="application/xml")
    return Response(status_code=404)

# -------------------------------
# Launch (for systemd/nginx)
# -------------------------------
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
