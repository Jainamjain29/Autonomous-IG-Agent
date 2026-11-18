"""Default constants and settings for Video Generation Agent."""

DEFAULT_PROVIDER = "claude_code"
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30

MIN_CLIP_DURATION_S = 2.0
MAX_CLIP_DURATION_S = 15.0
DEFAULT_CLIP_DURATION_S = 5.0

MAX_SCENE_RETRIES = 2  # Total attempts = 1 initial + 2 retries = 3
