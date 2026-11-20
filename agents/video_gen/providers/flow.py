"""Google Flow video generation provider automating VideoFX via Playwright."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from agents.video_gen.probe import probe_video
from agents.video_gen.providers.base import (
    Capabilities,
    CharacterModel,
    ClipInfo,
    VideoProvider,
    register_provider,
)
from agents.video_gen.providers import flow_selectors as sel


def get_flow_profile_dir() -> Path:
    """Return persistent browser profile directory located safely outside the repo."""
    override = os.environ.get("FLOW_PROFILE_DIR")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(local_app_data) / "ig_agent" / "flow_profile"


def is_flow_profile_initialized(profile_dir: Path) -> bool:
    """Check if profile directory exists and has been populated."""
    if not profile_dir.is_dir():
        return False
    # Check for basic Chromium profile items
    contents = list(profile_dir.iterdir())
    return len(contents) > 0


def run_login_helper(profile_dir: Optional[Path] = None) -> None:
    """Open headed Chromium browser session for manual Google login."""
    pdir = profile_dir or get_flow_profile_dir()
    pdir.mkdir(parents=True, exist_ok=True)

    print(f"\n🚀 Opening Google Flow login session using profile:\n   {pdir}")
    print("Please log in to your Google Account in the browser window.")
    print("When finished, simply close the browser window.\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(pdir),
            headless=False,
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()
        page.goto(sel.FLOW_URL)

        try:
            while len(context.pages) > 0:
                time.sleep(1)
        except Exception:
            pass

    print("✅ Session saved successfully.")


class FlowProvider(VideoProvider):
    """VideoProvider implementing Google Flow via Playwright browser automation."""

    def __init__(self):
        super().__init__(
            name="flow",
            capabilities=Capabilities(
                supports_9x16=True,
                supports_reference_image=True,
                native_clip_s=5.0,  # Flow outputs native clips (typically ~5s)
                max_clip_s=15.0,
            ),
        )
        self.profile_dir: Path = get_flow_profile_dir()

    def prepare(self, run_ctx: Dict[str, Any], character: CharacterModel) -> None:
        """Verify user authentication profile before proceeding."""
        if not is_flow_profile_initialized(self.profile_dir):
            raise RuntimeError(
                f"Google Flow profile not found or unauthenticated at '{self.profile_dir}'. "
                "Please run the login helper first: python -m agents.video_gen.providers.flow --login"
            )

    def generate(
        self,
        scene: Dict[str, Any],
        character: CharacterModel,
        out_path: Path,
        target: Optional[Dict[str, Any]] = None,
    ) -> ClipInfo:
        """Automate Google Flow to generate and download a scene clip."""
        if not is_flow_profile_initialized(self.profile_dir):
            raise RuntimeError(
                f"Google Flow profile not authenticated at '{self.profile_dir}'. "
                "Please run the login helper: python -m agents.video_gen.providers.flow --login"
            )

        scene_id = scene.get("id", 1)
        prompt_text = self._build_prompt(scene=scene, character=character)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        self._execute_playwright_flow(
            prompt_text=prompt_text,
            reference_image=character.reference_image,
            out_path=out_path,
        )

        duration_s, width, height = probe_video(out_path)

        return ClipInfo(
            scene_id=scene_id,
            path=str(out_path),
            duration_s=duration_s,
            width=width,
            height=height,
            provider=self.name,
        )

    def _build_prompt(self, scene: Dict[str, Any], character: CharacterModel) -> str:
        """Construct full prompt containing locked character specification and scene visual prompt."""
        visual = scene.get("visual_prompt", "").strip()
        if character.locked_block:
            return f"{character.locked_block}\n\nScene Prompt: {visual}"
        return visual

    def _execute_playwright_flow(
        self,
        prompt_text: str,
        reference_image: Optional[str],
        out_path: Path,
    ) -> None:
        """Execute Playwright browser automation sequence against Google Flow."""
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()

            try:
                # 1. Navigate
                page.goto(sel.FLOW_URL, timeout=60000)
                page.wait_for_load_state("networkidle")

                # 2. Check for login wall
                if not page.is_visible(sel.PROMPT_TEXTAREA):
                    raise RuntimeError(
                        "Google Flow prompt area not accessible. User may be signed out. "
                        "Please run the login helper: python -m agents.video_gen.providers.flow --login"
                    )

                # 3. Optional ingredient upload
                if reference_image and Path(reference_image).is_file():
                    if page.is_visible(sel.INGREDIENT_FILE_INPUT):
                        try:
                            page.set_input_files(sel.INGREDIENT_FILE_INPUT, str(Path(reference_image).resolve()))
                            page.wait_for_timeout(1000)
                        except Exception as e:
                            print(f"[flow] Warning: could not upload reference image: {e}")

                # 4. Aspect ratio selection (try 9:16)
                if page.is_visible(sel.ASPECT_RATIO_SELECTOR):
                    try:
                        page.click(sel.ASPECT_RATIO_SELECTOR)
                        if page.is_visible(sel.ASPECT_RATIO_9_16_OPTION):
                            page.click(sel.ASPECT_RATIO_9_16_OPTION)
                    except Exception:
                        pass

                # 5. Input prompt
                page.wait_for_selector(sel.PROMPT_TEXTAREA, timeout=15000)
                page.fill(sel.PROMPT_TEXTAREA, prompt_text)

                # 6. Click generate
                page.click(sel.GENERATE_BUTTON)

                # 7. Wait for generation and download
                page.wait_for_selector(sel.DOWNLOAD_BUTTON, timeout=180000)
                with page.expect_download(timeout=60000) as download_info:
                    page.click(sel.DOWNLOAD_BUTTON)

                download = download_info.value
                download.save_as(str(out_path))

            except PlaywrightTimeoutError as te:
                raise TimeoutError(f"Google Flow automation timed out: {te}") from te
            finally:
                context.close()


# Register provider
register_provider("flow", FlowProvider)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google Flow Browser Profile Login Helper")
    parser.add_argument("--login", action="store_true", help="Launch headed browser to authenticate Google session")
    args = parser.parse_args()

    if args.login:
        run_login_helper()
    else:
        parser.print_help()
        sys.exit(1)
