"""Default constants and settings for Video Generation Agent."""

DEFAULT_PROVIDER = "claude_code"
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30

MIN_CLIP_DURATION_S = 2.0
MAX_CLIP_DURATION_S = 15.0
DEFAULT_CLIP_DURATION_S = 5.0

# Per-engine retry budget:
# claude_code retries once internally with error feedback, so agent gives 1 extra attempt.
# flow and placeholder get 2 retries.
ENGINE_RETRIES = {
    "claude_code": 1,
    "flow": 2,
    "placeholder": 2,
}
MAX_SCENE_RETRIES = 2  # Fallback default

# Watchdog timeout per scene in seconds (wall-clock limit)
RENDER_TIMEOUT_S = 300
