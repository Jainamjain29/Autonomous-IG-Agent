"""Settings and platform constraints for the Content Agent."""
from __future__ import annotations

# Scene & Timing Constraints
TARGET_LENGTH_MIN_S: float = 30.0
TARGET_LENGTH_MAX_S: float = 60.0
TARGET_LENGTH_DEFAULT_S: float = 45.0

SCENE_COUNT_MIN: int = 4
SCENE_COUNT_MAX: int = 8

SCENE_DURATION_MIN_S: float = 3.0
SCENE_DURATION_MAX_S: float = 8.0

# Language
DEFAULT_LANGUAGE: str = "English"

# Instagram Constraints
IG_CAPTION_MAX_CHARS: int = 2200
IG_MAX_HASHTAGS: int = 5

# YouTube Constraints
YT_TITLE_MAX_CHARS: int = 100
YT_DESCRIPTION_MAX_CHARS: int = 5000

# Brand Voice for @streamovate
DEFAULT_BRAND_VOICE: str = (
    "Clear, friendly tech explainers on the latest products "
    "(what it is, how to use it, why it matters)."
)
