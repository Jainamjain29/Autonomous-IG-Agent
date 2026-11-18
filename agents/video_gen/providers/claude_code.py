"""Claude Code animation engine: AI-coded HTML/CSS/SVG animations rendered via Playwright."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

from core.llm import complete
from agents.video_gen.defaults import (
    DEFAULT_CLIP_DURATION_S,
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MAX_CLIP_DURATION_S,
    MIN_CLIP_DURATION_S,
)
from agents.video_gen.probe import probe_video
from agents.video_gen.providers.base import (
    Capabilities,
    CharacterModel,
    ClipInfo,
    VideoProvider,
    register_provider,
)


DESIGN_KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "palette": {
            "type": "object",
            "properties": {
                "primary": {"type": "string"},
                "secondary": {"type": "string"},
                "accent": {"type": "string"},
                "background": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["primary", "background", "text"],
        },
        "fonts": {
            "type": "object",
            "properties": {
                "heading": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["heading", "body"],
        },
        "character_svg": {"type": "string"},
    },
    "required": ["palette", "fonts", "character_svg"],
}

SCENE_HTML_SCHEMA = {
    "type": "object",
    "properties": {
        "html": {"type": "string"},
    },
    "required": ["html"],
}


class ClaudeCodeProvider(VideoProvider):
    """Generates animations via AI-coded HTML/CSS/SVG and renders deterministically using Playwright."""

    def __init__(self):
        super().__init__(
            name="claude_code",
            capabilities=Capabilities(
                supports_9x16=True,
                supports_reference_image=True,
                native_clip_s=None,
                max_clip_s=MAX_CLIP_DURATION_S,
            ),
        )
        self.design_kit: Optional[Dict[str, Any]] = None
        self.character_svg: str = ""
        self.agent_dir: Optional[Path] = None

    def prepare(self, run_ctx: Dict[str, Any], character: CharacterModel) -> None:
        """Call LLM once to create design kit and SVG character; save to disk and cache."""
        run_dir = Path(run_ctx["run_dir"])
        self.agent_dir = Path(run_ctx.get("agent_dir") or (run_dir / "video_gen"))
        self.agent_dir.mkdir(parents=True, exist_ok=True)

        # Check if already prepared (e.g. on resume)
        kit_file = self.agent_dir / "design_kit.json"
        svg_file = self.agent_dir / "character.svg"
        if kit_file.exists() and svg_file.exists():
            try:
                with open(kit_file, "r", encoding="utf-8") as f:
                    self.design_kit = json.load(f)
                with open(svg_file, "r", encoding="utf-8") as f:
                    self.character_svg = f.read()
                if self.design_kit and self.character_svg:
                    return
            except Exception:
                pass

        # Check for reference image data URI
        ref_image_uri = ""
        if character.reference_image and Path(character.reference_image).is_file():
            try:
                ext = Path(character.reference_image).suffix.lower().lstrip(".")
                mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                with open(character.reference_image, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")
                ref_image_uri = f"data:{mime};base64,{b64_data}"
            except Exception:
                ref_image_uri = ""

        system_prompt = (
            "You are an expert creative director and SVG motion designer. "
            "Produce a cohesive design kit for a 9:16 vertical short-form video series. "
            "FONTS MUST BE SYSTEM/WEB-SAFE ONLY (e.g. 'Impact, Arial Black, sans-serif' or 'system-ui, -apple-system, sans-serif'). "
            "NEVER request external web fonts or CDN resources. "
            "Generate a complete, self-contained inline SVG for the character that captures their description. "
            "If a reference image data URI is provided, incorporate or stylize it into the SVG."
        )

        user_content = (
            f"{character.locked_block}\n\n"
            f"Reference Image Data URI: {ref_image_uri or 'None'}\n\n"
            "Generate the design kit JSON containing palette, fonts, and inline character_svg."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        resp = complete(
            agent="video_gen",
            messages=messages,
            json_schema=DESIGN_KIT_SCHEMA,
        )

        kit_data = resp.data if isinstance(resp.data, dict) else json.loads(resp.text)
        self.design_kit = kit_data
        self.character_svg = kit_data.get("character_svg", "")

        # Write to video_gen/design_kit.json and video_gen/character.svg
        with open(kit_file, "w", encoding="utf-8") as f:
            json.dump(kit_data, f, indent=2, ensure_ascii=False)
        with open(svg_file, "w", encoding="utf-8") as f:
            f.write(self.character_svg)

    def generate(
        self,
        scene: Dict[str, Any],
        character: CharacterModel,
        out_path: Path,
        target: Optional[Dict[str, Any]] = None,
    ) -> ClipInfo:
        """Generate AI-coded HTML animation and render frame-by-frame to MP4."""
        target = target or {}
        scene_id = int(scene.get("id", 1))

        # Clamp duration to 2.0 - 15.0 s
        raw_dur = float(target.get("duration_s", scene.get("duration_hint_s", DEFAULT_CLIP_DURATION_S)))
        duration_s = max(MIN_CLIP_DURATION_S, min(MAX_CLIP_DURATION_S, raw_dur))

        fps = int(target.get("fps", DEFAULT_FPS))
        width = int(target.get("width", DEFAULT_WIDTH))
        height = int(target.get("height", DEFAULT_HEIGHT))

        scenes_dir = out_path.parent.parent / "scenes" if out_path.parent.name == "clips" else out_path.parent / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        html_file = scenes_dir / f"{scene_id}.html"

        # Generate HTML with retry feedback
        html_code = self._generate_html_with_retry(
            scene=scene,
            character=character,
            duration_s=duration_s,
            width=width,
            height=height,
            html_file=html_file,
            out_path=out_path,
            fps=fps,
        )

        # Probe the rendered MP4
        probed_dur, probed_w, probed_h = probe_video(out_path)

        return ClipInfo(
            scene_id=scene_id,
            path=str(out_path),
            duration_s=probed_dur,
            width=probed_w,
            height=probed_h,
            provider=self.name,
        )

    def _generate_html_with_retry(
        self,
        scene: Dict[str, Any],
        character: CharacterModel,
        duration_s: float,
        width: int,
        height: int,
        html_file: Path,
        out_path: Path,
        fps: int,
    ) -> str:
        """Invoke LLM for HTML animation code; test render; retry once on error."""
        scene_id = scene.get("id", 1)
        system_prompt = (
            "You are an expert creative technologist who codes standalone, deterministic HTML5/CSS/SVG animations. "
            "You write self-contained HTML documents at 1080x1920 (vertical 9:16) that animate smoothly. "
            "RENDER CONTRACT REQUIREMENTS:\n"
            "1. The page MUST define `window.renderFrame(t_seconds)` where t_seconds is current playback time.\n"
            "2. When renderFrame(t) is called, the visual state of the document MUST be updated to correspond to time t.\n"
            "3. Web Animations API (element.animate) or CSS transitions/transforms controlled via renderFrame or currentTime are fully supported.\n"
            "4. NO EXTERNAL FETCHES OR NETWORKING: no Google Fonts, no external CDN JS/CSS, no external images.\n"
            "5. Use the provided design kit palette, web-safe fonts, and inline character SVG.\n"
            "6. Dimensions: exact 1080x1920 viewport (width: 1080px; height: 1920px; overflow: hidden; margin: 0; padding: 0;).\n"
            "Return JSON format matching schema: { 'html': '<!DOCTYPE html>...' }"
        )

        palette_str = json.dumps(self.design_kit.get("palette", {}) if self.design_kit else {})
        fonts_str = json.dumps(self.design_kit.get("fonts", {}) if self.design_kit else {})

        user_content = (
            f"Scene ID: {scene_id}\n"
            f"Visual Prompt: {scene.get('visual_prompt', '')}\n"
            f"Narration Context (no audio voiceover needed, visuals only): {scene.get('narration', '')}\n"
            f"Target Duration: {duration_s} seconds\n"
            f"Design Kit Palette: {palette_str}\n"
            f"Design Kit Fonts: {fonts_str}\n"
            f"Character SVG: {self.character_svg}\n\n"
            "Write the complete self-contained HTML document."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # Attempt 1
        resp = complete(
            agent="video_gen",
            messages=messages,
            json_schema=SCENE_HTML_SCHEMA,
        )
        html_data = resp.data if isinstance(resp.data, dict) else json.loads(resp.text)
        html_content = html_data.get("html", "")

        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)

        # Attempt render
        try:
            self._render_html_to_mp4(
                html_path=html_file,
                out_path=out_path,
                duration_s=duration_s,
                fps=fps,
                width=width,
                height=height,
            )
            return html_content
        except Exception as first_err:
            # Feedback error and retry once
            err_msg = str(first_err)
            retry_messages = list(messages)
            retry_messages.append({"role": "assistant", "content": json.dumps({"html": html_content})})
            retry_messages.append({
                "role": "user",
                "content": (
                    f"The previous HTML failed to render properly with error:\n{err_msg}\n\n"
                    "Please fix the code. Ensure window.renderFrame(t_seconds) is defined as a global function on window, "
                    "no external URLs are referenced, and syntax is valid. Return the corrected HTML in { 'html': '...' }."
                ),
            })

            resp2 = complete(
                agent="video_gen",
                messages=retry_messages,
                json_schema=SCENE_HTML_SCHEMA,
            )
            html_data2 = resp2.data if isinstance(resp2.data, dict) else json.loads(resp2.text)
            html_content2 = html_data2.get("html", "")

            with open(html_file, "w", encoding="utf-8") as f:
                f.write(html_content2)

            try:
                self._render_html_to_mp4(
                    html_path=html_file,
                    out_path=out_path,
                    duration_s=duration_s,
                    fps=fps,
                    width=width,
                    height=height,
                )
                return html_content2
            except Exception as second_err:
                raise RuntimeError(
                    f"Scene {scene_id} HTML animation failed rendering after retry: {second_err}"
                ) from second_err

    def _render_html_to_mp4(
        self,
        html_path: Path,
        out_path: Path,
        duration_s: float,
        fps: int,
        width: int,
        height: int,
    ) -> None:
        """Render HTML frame-by-frame via Playwright and pipe PNG bytes to FFmpeg."""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        total_frames = max(1, int(round(duration_s * fps)))
        stderr_log_path = out_path.with_suffix(".ffmpeg.log")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ffmpeg_proc: Optional[subprocess.Popen] = None
            try:
                page = browser.new_page(viewport={"width": width, "height": height})

                page_errors: List[str] = []
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                def on_console(msg):
                    if msg.type == "error":
                        text = msg.text
                        # Ignore expected errors resulting from aborted external network requests
                        if "net::ERR_" in text or "Failed to load resource" in text:
                            return
                        page_errors.append(text)

                page.on("console", on_console)

                # Sandbox: block all non-file/data requests
                def handle_route(route):
                    url = route.request.url.lower()
                    if url.startswith("file:") or url.startswith("data:") or url.startswith("about:"):
                        route.continue_()
                    else:
                        route.abort()

                page.route("**/*", handle_route)

                # Load HTML file
                file_url = html_path.resolve().as_uri()
                page.goto(file_url, wait_until="load", timeout=15000)

                # Verify renderFrame contract BEFORE spawning FFmpeg
                has_render_frame = page.evaluate("typeof window.renderFrame === 'function'")
                if not has_render_frame:
                    raise ValueError(
                        f"Render contract violated: window.renderFrame(t_seconds) is not defined in {html_path.name}"
                    )

                if page_errors:
                    raise RuntimeError(f"JavaScript errors during initial load: {'; '.join(page_errors)}")

                # Spawn FFmpeg encoder process with stderr directed to file (avoids OS pipe deadlock)
                ffmpeg_cmd = [
                    ffmpeg_exe,
                    "-y",
                    "-f", "image2pipe",
                    "-vcodec", "png",
                    "-r", str(fps),
                    "-i", "-",
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-preset", "veryfast",
                    "-s", f"{width}x{height}",
                    str(out_path),
                ]

                with open(stderr_log_path, "w", encoding="utf-8") as stderr_f:
                    ffmpeg_proc = subprocess.Popen(
                        ffmpeg_cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_f,
                    )

                    # Render each frame deterministically
                    for frame_idx in range(total_frames):
                        t_seconds = frame_idx / float(fps)
                        page.evaluate(
                            """(t) => {
                                if (typeof window.renderFrame === 'function') {
                                    window.renderFrame(t);
                                }
                                if (document.getAnimations) {
                                    const anims = document.getAnimations();
                                    for (let i = 0; i < anims.length; i++) {
                                        anims[i].pause();
                                        anims[i].currentTime = t * 1000;
                                    }
                                }
                            }""",
                            t_seconds,
                        )

                        if page_errors:
                            raise RuntimeError(
                                f"JavaScript error at t={t_seconds}s: {'; '.join(page_errors)}"
                            )

                        png_bytes = page.screenshot(type="png")
                        ffmpeg_proc.stdin.write(png_bytes)

                    ffmpeg_proc.stdin.close()
                    ffmpeg_proc.wait()

                if ffmpeg_proc.returncode != 0:
                    err_output = ""
                    if stderr_log_path.exists():
                        err_output = stderr_log_path.read_text(encoding="utf-8", errors="replace")
                    raise RuntimeError(f"FFmpeg render failed (code {ffmpeg_proc.returncode}): {err_output}")

                # Clean up stderr log file on success
                if stderr_log_path.exists():
                    try:
                        stderr_log_path.unlink()
                    except Exception:
                        pass

            finally:
                if ffmpeg_proc and ffmpeg_proc.stdin and not ffmpeg_proc.stdin.closed:
                    try:
                        ffmpeg_proc.stdin.close()
                    except Exception:
                        pass
                if ffmpeg_proc and ffmpeg_proc.poll() is None:
                    try:
                        ffmpeg_proc.kill()
                    except Exception:
                        pass
                browser.close()


# Register provider
register_provider("claude_code", ClaudeCodeProvider)
