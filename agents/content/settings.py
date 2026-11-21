"""Settings and platform constraints for the Content Agent."""
from __future__ import annotations

# Allowed execution modes defined by core contracts (core/contracts.py)
ALLOWED_MODES: set = {"generate", "script", "character", "video"}

# Scene & Timing Constraints
TARGET_LENGTH_MIN_S: float = 30.0
TARGET_LENGTH_MAX_S: float = 60.0
TARGET_LENGTH_DEFAULT_S: float = 45.0

SCENE_COUNT_MIN: int = 4
SCENE_COUNT_MAX: int = 8

SCENE_DURATION_MIN_S: float = 3.0
SCENE_DURATION_MAX_S: float = 8.0

# Speech timing constant (2.5 words per second + 0.5s buffer)
SPEECH_WORDS_PER_SECOND: float = 2.5
SPEECH_BUFFER_S: float = 0.5
MAX_WORDS_PER_SCENE: int = 20

# Language
DEFAULT_LANGUAGE: str = "English"

# Instagram Constraints
IG_CAPTION_MAX_CHARS: int = 2200
IG_MAX_HASHTAGS: int = 5

# YouTube Constraints
YT_TITLE_MAX_CHARS: int = 100
YT_DESCRIPTION_MAX_CHARS: int = 5000

# Brand Identity & Suffix
BRAND_NAME: str = "StreamOvate"
BRAND_HANDLE: str = "@streamovate"
VISUAL_PROMPT_STYLE_SUFFIX: str = "Vertical 9:16 frame, subject centered, no on-screen text."

# Brand Voice for StreamOvate
DEFAULT_BRAND_VOICE: str = (
    f"Clear, friendly tech explainers on the latest products "
    f"(what it is, how to use it, why it matters) for {BRAND_NAME} ({BRAND_HANDLE})."
)
