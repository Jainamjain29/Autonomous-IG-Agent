"""Placeholder video generation engine for dry-runs and automated unit tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from core.testing import make_tiny_mp4
from agents.video_gen.defaults import (
    DEFAULT_CLIP_DURATION_S,
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


class PlaceholderProvider(VideoProvider):
    """Engine that generates valid synthetic MP4 clips without network or LLM calls."""

    def __init__(self):
        super().__init__(
            name="placeholder",
            capabilities=Capabilities(
                supports_9x16=True,
                supports_reference_image=True,
                native_clip_s=None,
                max_clip_s=MAX_CLIP_DURATION_S,
            ),
        )

    def prepare(self, run_ctx: Dict[str, Any], character: CharacterModel) -> None:
        """No initialization necessary for placeholder."""
        pass

    def generate(
        self,
        scene: Dict[str, Any],
        character: CharacterModel,
        out_path: Path,
        target: Optional[Dict[str, Any]] = None,
    ) -> ClipInfo:
        """Generate a valid MP4 file at requested dimensions and duration."""
        target = target or {}
        scene_id = int(scene.get("id", 1))

        # Clamp duration
        raw_duration = float(target.get("duration_s", scene.get("duration_hint_s", DEFAULT_CLIP_DURATION_S)))
        duration_s = max(MIN_CLIP_DURATION_S, min(MAX_CLIP_DURATION_S, raw_duration))

        width = int(target.get("width", DEFAULT_WIDTH))
        height = int(target.get("height", DEFAULT_HEIGHT))

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Generate synthetic clip using core.testing
        make_tiny_mp4(out_path, w=width, h=height, seconds=duration_s)

        # Probe file for real values
        probed_dur, probed_w, probed_h = probe_video(out_path)

        return ClipInfo(
            scene_id=scene_id,
            path=str(out_path),
            duration_s=probed_dur,
            width=probed_w,
            height=probed_h,
            provider=self.name,
        )


# Register engine
register_provider("placeholder", PlaceholderProvider)
