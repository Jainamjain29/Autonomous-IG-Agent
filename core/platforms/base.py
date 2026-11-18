"""Base interface and data records for social media publishing adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any


@dataclass
class PublishRecord:
    """Record resulting from a publish attempt."""
    platform: str
    status: str  # ok | failed
    platform_post_id: Optional[str] = None
    permalink: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PublishAdapter(ABC):
    """Abstract interface for platform publishing adapters."""

    def __init__(self, platform: str):
        self.platform = platform

    @abstractmethod
    def publish(self, video_path: str, text: str, dry_run: bool = False) -> PublishRecord:
        """Publish a video with text to the target platform.
        
        Args:
            video_path: Local filesystem path to the mp4 video.
            text: Caption / description text.
            dry_run: When True, validate and prepare without finalizing publish.
        """
        raise NotImplementedError
