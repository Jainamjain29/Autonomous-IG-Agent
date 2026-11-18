"""YouTube Shorts publishing adapter stub conforming to PublishAdapter."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import database as db
from core.platforms.base import PublishAdapter, PublishRecord

DEFAULT_PRIVACY = "private"


class YouTubeAdapter(PublishAdapter):
    """Publishes videos to YouTube Shorts.
    
    Checks for Google OAuth credentials before attempting network operations.
    If not configured, returns status 'skipped' with 'not configured: Google OAuth not set up'.
    """

    def __init__(self):
        super().__init__(platform="youtube")

    def _is_oauth_configured(self) -> bool:
        """Check if Google/YouTube OAuth credentials are set in settings or environment."""
        oauth_keys = [
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_CLIENT_ID",
            "YOUTUBE_CLIENT_SECRET",
            "YOUTUBE_CLIENT_ID",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ]
        for key in oauth_keys:
            val = db.get_setting(key) or os.environ.get(key)
            if val:
                return True
        return False

    def publish(
        self,
        video_path: str,
        text: str,
        dry_run: bool = False,
        title: Optional[str] = None,
        description: Optional[str] = None,
        privacy: Optional[str] = None,
        **kwargs: Any,
    ) -> PublishRecord:
        """Publish video to YouTube Shorts.
        
        Args:
            video_path: Path to the video file.
            text: Fallback caption/description text.
            dry_run: Validation only flag.
            title: YouTube video title.
            description: YouTube video description.
            privacy: 'private', 'unlisted', or 'public'. Defaults to 'private'.
        """
        video_file = Path(video_path)
        if not video_file.exists() or video_file.stat().st_size == 0:
            return PublishRecord(
                platform="youtube",
                status="failed",
                error=f"Video file missing or empty: {video_path}",
            )

        video_title = title or "Shorts Video"
        video_desc = description if description is not None else text
        privacy_status = privacy or DEFAULT_PRIVACY

        # Config check
        if not self._is_oauth_configured():
            return PublishRecord(
                platform="youtube",
                status="skipped",
                platform_post_id=None,
                permalink=None,
                error="not configured: Google OAuth not set up",
            )

        if dry_run:
            return PublishRecord(
                platform="youtube",
                status="dry_run",
                platform_post_id=f"dry_run: {video_title}",
                permalink=f"dry_run: {video_desc}",
                error=None,
            )

        # Upload structure placeholder ready for future Google API client drop-in
        return self._execute_upload(
            file_path=str(video_file),
            title=video_title,
            description=video_desc,
            privacy=privacy_status,
        )

    def _execute_upload(
        self, file_path: str, title: str, description: str, privacy: str
    ) -> PublishRecord:
        """Placeholder for Google API Client upload flow (to be populated in future prompts)."""
        return PublishRecord(
            platform="youtube",
            status="skipped",
            error="not configured: Google OAuth upload client not implemented",
        )
