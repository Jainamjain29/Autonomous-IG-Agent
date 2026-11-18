"""Publishing platform adapters for the Posting Agent."""
from agents.posting.platforms.instagram import InstagramAdapter
from agents.posting.platforms.youtube import YouTubeAdapter

__all__ = ["InstagramAdapter", "YouTubeAdapter"]
