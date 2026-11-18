"""Providers package for Video Generation engines."""
from agents.video_gen.providers.base import (
    Capabilities,
    CharacterModel,
    ClipInfo,
    VideoProvider,
    get_provider,
    list_providers,
    register_provider,
)

# Import engine implementations to register them in the registry
import agents.video_gen.providers.claude_code
import agents.video_gen.providers.flow
import agents.video_gen.providers.placeholder

__all__ = [
    "Capabilities",
    "CharacterModel",
    "ClipInfo",
    "VideoProvider",
    "get_provider",
    "list_providers",
    "register_provider",
]
