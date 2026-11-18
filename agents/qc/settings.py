"""Centralized QC Agent configuration, thresholds, and limits.

All thresholds have a comment referencing source and verification date.
"""
from __future__ import annotations

# Video Format Thresholds
# Target aspect ratio 9:16 (0.5625), checked 18 Nov 2026 (Instagram Reels & YouTube Shorts official 9:16 vertical spec)
TARGET_ASPECT_RATIO = 9.0 / 16.0
# Aspect ratio tolerance ±1% (0.01), checked 18 Nov 2026 (aspect ratio tolerance band [0.556875, 0.568125])
ASPECT_RATIO_TOLERANCE = 0.01

# Minimum resolution 720x1280, checked 18 Nov 2026 (Instagram Reels minimum upload resolution)
MIN_WIDTH = 720
MIN_HEIGHT = 1280

# Standard standard resolution 1080x1920, checked 18 Nov 2026 (Instagram & YouTube Shorts standard vertical resolution)
STANDARD_WIDTH = 1080
STANDARD_HEIGHT = 1920

# YouTube Shorts resolution ceiling 1080p, checked 18 Nov 2026 (YouTube Shorts recommendation: warn if above 1080p)
YT_MAX_WIDTH = 1080
YT_MAX_HEIGHT = 1920

# Allowed video codecs for Instagram: H.264 or HEVC/H.265, checked 18 Nov 2026 (Instagram video upload specifications)
INSTAGRAM_ALLOWED_VIDEO_CODECS = {"h264", "avc1", "hevc", "h265"}

# Allowed frame rate range 23.0 to 60.0 fps, checked 18 Nov 2026 (Instagram supported frame rate range)
MIN_FPS = 23.0
MAX_FPS = 60.0

# Moov atom must precede mdat atom, checked 18 Nov 2026 (Instagram progressive download faststart requirement)
REQUIRE_FASTSTART_INSTAGRAM = True

# Duration Thresholds
# Instagram minimum duration 3.0 s and maximum duration 15.0 min (900 s), checked 18 Nov 2026 (Instagram Reels duration policy)
INSTAGRAM_MIN_DURATION_S = 3.0
INSTAGRAM_MAX_DURATION_S = 900.0  # 15 minutes

# YouTube Shorts maximum duration 180.0 s (3 min), checked 18 Nov 2026 (YouTube Shorts duration limit up to 3 minutes)
YOUTUBE_SHORTS_MAX_DURATION_S = 180.0

# Target duration range 15.0 s to 90.0 s, checked 18 Nov 2026 (Internal creative guideline for optimal viewer retention)
TARGET_MIN_DURATION_S = 15.0
TARGET_MAX_DURATION_S = 90.0

# Maximum allowed duration drift between probe and sync_merge duration_s = 0.5 s, checked 18 Nov 2026 (Audio/video sync drift margin)
MAX_DURATION_DRIFT_S = 0.5

# Audio Thresholds
# Silent volume detection threshold -60.0 dB mean_volume, checked 18 Nov 2026 (ffmpeg volumedetect silence threshold)
SILENT_AUDIO_THRESHOLD_DB = -60.0

# Instagram recommended audio codec AAC, checked 18 Nov 2026 (Instagram audio stream specification)
INSTAGRAM_RECOMMENDED_AUDIO_CODEC = "aac"

# File Size Thresholds
# Instagram maximum file size 300 MB, checked 18 Nov 2026 (Instagram Reels upload size limit: 300 MB = 314,572,800 bytes)
INSTAGRAM_MAX_FILE_SIZE_BYTES = 300 * 1024 * 1024

# Subtitles Thresholds
# Subtitle cue coverage threshold 80% (0.80), checked 18 Nov 2026 (Subtitle readability and speech coverage requirement)
SUBTITLE_MIN_COVERAGE_RATIO = 0.80

# Scene & Narration Coverage Thresholds
# Maximum gap between consecutive scenes 1.0 s, checked 18 Nov 2026 (Scene timing continuity guideline)
MAX_SCENE_GAP_S = 1.0

# Narration vs SRT word coverage thresholds: warn below 0.85, blocker below 0.60, checked 18 Nov 2026 (Speech-to-text alignment thresholds)
NARRATION_COVERAGE_WARN_THRESHOLD = 0.85
NARRATION_COVERAGE_BLOCKER_THRESHOLD = 0.60

# Text Compliance Thresholds
# Instagram maximum caption length 2,200 characters, checked 18 Nov 2026 (Instagram caption character limit)
INSTAGRAM_MAX_CAPTION_LENGTH = 2200

# Instagram maximum hashtags 5, checked 18 Nov 2026 (Instagram search & recommendation algorithm guideline)
INSTAGRAM_MAX_HASHTAGS = 5

# Instagram minimum hashtags 3, checked 18 Nov 2026 (Instagram discoverability guideline)
INSTAGRAM_MIN_HASHTAGS = 3

# Instagram hook length 125 characters, checked 18 Nov 2026 (Instagram feed preview truncation point before '...more')
INSTAGRAM_MAX_HOOK_LENGTH = 125

# YouTube title character limit 100, checked 18 Nov 2026 (YouTube video title limit)
YOUTUBE_MAX_TITLE_LENGTH = 100

# YouTube description character limit 5,000, checked 18 Nov 2026 (YouTube video description limit)
YOUTUBE_MAX_DESCRIPTION_LENGTH = 5000

# AI Critic Configuration
# AI Critic disabled by default, checked 18 Nov 2026 (Advisory AI review feature flag)
ENABLE_AI_CRITIC = False

# AI Critic model default gemini-2.5-flash, checked 18 Nov 2026 (QC Agent LLM configuration)
CRITIC_MODEL = "gemini-2.5-flash"
