"""API Template skeleton for external video generation providers (Luma, Runway, Veo).

This module serves as a reference architecture and skeleton showing how to
implement an asynchronous job-based video API provider.
NOTE: This template is NOT registered in the provider registry and does not execute
network calls.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from agents.video_gen.probe import probe_video
from agents.video_gen.providers.base import Capabilities, CharacterModel, ClipInfo, VideoProvider


class ApiVideoProviderTemplate(VideoProvider):
    """Skeleton implementation for job-based external video generation APIs."""

    def __init__(self, name: str = "api_template"):
        super().__init__(
            name=name,
            capabilities=Capabilities(
                supports_9x16=True,
                supports_reference_image=True,
                native_clip_s=5.0,  # e.g., fixed 5-second generation window
                max_clip_s=10.0,
            ),
        )
        self.api_key: Optional[str] = None
        self.base_url: str = "https://api.example.com/v1"
        self.poll_interval_s: float = 5.0
        self.job_timeout_s: float = 300.0

    def prepare(self, run_ctx: Dict[str, Any], character: CharacterModel) -> None:
        """Called once per run. Verify credentials and stage character reference asset if needed."""
        # 1. Read API credentials from environment or settings
        # self.api_key = os.getenv(f"{self.name.upper()}_API_KEY")
        # if not self.api_key:
        #     raise ValueError(f"Missing API key for {self.name}")

        # 2. If character reference image is local, optionally upload as reusable asset
        # if character.reference_image:
        #     self.character_asset_id = self._upload_asset(character.reference_image)
        pass

    def generate(
        self,
        scene: Dict[str, Any],
        character: CharacterModel,
        out_path: Path,
        target: Optional[Dict[str, Any]] = None,
    ) -> ClipInfo:
        """Generate a video clip: submit job -> poll -> download -> probe -> ClipInfo."""
        scene_id = scene.get("id", 1)
        prompt = f"{character.locked_block}\n\nScene Prompt: {scene.get('visual_prompt', '')}"

        # Step 1: Submit Generation Job
        # job_id = self._submit_job(prompt=prompt, aspect_ratio="9:16")
        job_id = "mock_job_id"

        # Step 2: Poll with backoff and timeout
        # download_url = self._poll_job(job_id=job_id, timeout_s=self.job_timeout_s)
        download_url = "https://example.com/mock.mp4"

        # Step 3: Download to destination file
        # self._download_clip(download_url=download_url, dest_path=out_path)

        # Step 4: Always probe real video file on disk (never trust API response metadata)
        duration_s, width, height = probe_video(out_path)

        return ClipInfo(
            scene_id=scene_id,
            path=str(out_path),
            duration_s=duration_s,
            width=width,
            height=height,
            provider=self.name,
        )

    def _submit_job(self, prompt: str, aspect_ratio: str = "9:16") -> str:
        """Submit generation request to API and return job ID."""
        raise NotImplementedError("Skeleton method: implement API request payload")

    def _poll_job(self, job_id: str, timeout_s: float = 300.0) -> str:
        """Poll job status until 'completed' or timeout. Return video download URL."""
        start_time = time.time()
        while time.time() - start_time < timeout_s:
            # resp = requests.get(f"{self.base_url}/jobs/{job_id}", headers=...)
            # status = resp.json().get("status")
            # if status == "completed":
            #     return resp.json()["video_url"]
            # elif status == "failed":
            #     raise RuntimeError(f"Generation failed: {resp.text}")
            time.sleep(self.poll_interval_s)
        raise TimeoutError(f"Job {job_id} timed out after {timeout_s}s")

    def _download_clip(self, download_url: str, dest_path: Path) -> None:
        """Download binary clip data and write to dest_path."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        # with requests.get(download_url, stream=True) as r:
        #     with open(dest_path, "wb") as f:
        #         shutil.copyfileobj(r.raw, f)
        pass
