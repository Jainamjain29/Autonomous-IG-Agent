"""Video format, resolution, codec, duration, audio, and file size checks."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agents.qc.probe import ProbeData
from agents.qc import settings


def check_video_readable(probe: ProbeData) -> Dict[str, Any]:
    """B: video file exists and ffprobe/ffmpeg can read it."""
    if not probe.file_exists:
        return {
            "name": "video_file_readable",
            "passed": False,
            "severity": "blocker",
            "detail": probe.error or "Video file does not exist",
        }
    if not probe.readable:
        return {
            "name": "video_file_readable",
            "passed": False,
            "severity": "blocker",
            "detail": probe.error or "Video file could not be read or parsed",
        }
    return {
        "name": "video_file_readable",
        "passed": True,
        "severity": "blocker",
        "detail": f"Video file is readable ({probe.width}x{probe.height}, {probe.duration_s:.2f}s)",
    }


def check_aspect_ratio(probe: ProbeData) -> Dict[str, Any]:
    """B: aspect ratio 9:16 (±1% tolerance)."""
    if probe.height <= 0:
        return {
            "name": "video_aspect_ratio",
            "passed": False,
            "severity": "blocker",
            "detail": "Invalid video dimensions (height <= 0)",
        }

    ratio = probe.aspect_ratio
    target = settings.TARGET_ASPECT_RATIO
    tol = settings.ASPECT_RATIO_TOLERANCE
    min_ratio = target * (1.0 - tol)
    max_ratio = target * (1.0 + tol)

    if min_ratio <= ratio <= max_ratio:
        return {
            "name": "video_aspect_ratio",
            "passed": True,
            "severity": "blocker",
            "detail": f"{probe.width}x{probe.height} (ratio {ratio:.4f}) within 9:16 ±1% tolerance",
        }
    return {
        "name": "video_aspect_ratio",
        "passed": False,
        "severity": "blocker",
        "detail": f"Aspect ratio {ratio:.4f} ({probe.width}x{probe.height}) outside 9:16 ±1% range [{min_ratio:.4f}, {max_ratio:.4f}]",
    }


def check_resolution(probe: ProbeData, platforms: List[str]) -> List[Dict[str, Any]]:
    """Resolution checks: minimum 720x1280 (B), standard 1080x1920 (W), YouTube above 1080p (W)."""
    checks = []

    # Blocker: minimum 720x1280
    if probe.width < settings.MIN_WIDTH or probe.height < settings.MIN_HEIGHT:
        checks.append({
            "name": "video_resolution_minimum",
            "passed": False,
            "severity": "blocker",
            "detail": f"Resolution {probe.width}x{probe.height} below minimum {settings.MIN_WIDTH}x{settings.MIN_HEIGHT}",
        })
    else:
        checks.append({
            "name": "video_resolution_minimum",
            "passed": True,
            "severity": "blocker",
            "detail": f"Resolution {probe.width}x{probe.height} meets minimum {settings.MIN_WIDTH}x{settings.MIN_HEIGHT}",
        })

    # Warn: standard 1080x1920
    is_standard = probe.width == settings.STANDARD_WIDTH and probe.height == settings.STANDARD_HEIGHT
    if is_standard:
        checks.append({
            "name": "video_resolution_standard",
            "passed": True,
            "severity": "warn",
            "detail": f"Resolution is standard {settings.STANDARD_WIDTH}x{settings.STANDARD_HEIGHT}",
        })
    else:
        checks.append({
            "name": "video_resolution_standard",
            "passed": False,
            "severity": "warn",
            "detail": f"Resolution is {probe.width}x{probe.height}, not standard {settings.STANDARD_WIDTH}x{settings.STANDARD_HEIGHT}",
        })

    # YouTube: Warn if above 1080p
    if "youtube" in platforms:
        is_above_1080p = probe.width > settings.YT_MAX_WIDTH or probe.height > settings.YT_MAX_HEIGHT
        if is_above_1080p:
            checks.append({
                "name": "video_resolution_youtube_1080p",
                "passed": False,
                "severity": "warn",
                "detail": f"Resolution {probe.width}x{probe.height} exceeds recommended 1080p for Shorts",
            })
        else:
            checks.append({
                "name": "video_resolution_youtube_1080p",
                "passed": True,
                "severity": "warn",
                "detail": f"Resolution {probe.width}x{probe.height} within 1080p limit for Shorts",
            })

    return checks


def check_codecs_and_container(probe: ProbeData, platforms: List[str]) -> List[Dict[str, Any]]:
    """Instagram video codec H.264/HEVC (B), fps 23-60 (B), faststart moov atom (B)."""
    checks = []

    if "instagram" in platforms:
        # Codec check
        codec = probe.video_codec.lower()
        if codec in settings.INSTAGRAM_ALLOWED_VIDEO_CODECS:
            checks.append({
                "name": "video_codec_instagram",
                "passed": True,
                "severity": "blocker",
                "detail": f"Video codec '{probe.video_codec}' supported (H.264/HEVC)",
            })
        else:
            checks.append({
                "name": "video_codec_instagram",
                "passed": False,
                "severity": "blocker",
                "detail": f"Video codec '{probe.video_codec}' not supported on Instagram (must be H.264 or HEVC)",
            })

        # Frame rate check (tolerance for 23.976 fps)
        fps = probe.fps
        if 22.9 <= fps <= 60.5:
            checks.append({
                "name": "video_fps_instagram",
                "passed": True,
                "severity": "blocker",
                "detail": f"Frame rate {fps:.2f} fps within supported 23–60 fps range",
            })
        else:
            checks.append({
                "name": "video_fps_instagram",
                "passed": False,
                "severity": "blocker",
                "detail": f"Frame rate {fps:.2f} fps outside supported 23–60 fps range",
            })

        # Faststart check
        if probe.is_faststart:
            checks.append({
                "name": "video_faststart_instagram",
                "passed": True,
                "severity": "blocker",
                "detail": "Moov atom at front of file (faststart verified)",
            })
        else:
            checks.append({
                "name": "video_faststart_instagram",
                "passed": False,
                "severity": "blocker",
                "detail": "Moov atom not at front of file (missing faststart)",
            })

    return checks


def check_duration(
    probe: ProbeData,
    platforms: List[str],
    sync_merge_duration: Optional[float] = None,
    is_existing_video: bool = False,
) -> List[Dict[str, Any]]:
    """Duration checks: Instagram 3s-15m (B), YouTube 180s (B), target 15-90s (W), drift 0.5s (W)."""
    checks = []
    dur = probe.duration_s

    if "instagram" in platforms:
        if settings.INSTAGRAM_MIN_DURATION_S <= dur <= settings.INSTAGRAM_MAX_DURATION_S:
            checks.append({
                "name": "duration_instagram",
                "passed": True,
                "severity": "blocker",
                "detail": f"Duration {dur:.2f}s within Instagram limits (3s–15m)",
            })
        else:
            checks.append({
                "name": "duration_instagram",
                "passed": False,
                "severity": "blocker",
                "detail": f"Duration {dur:.2f}s outside Instagram limits (3s–15m)",
            })

    if "youtube" in platforms:
        if dur <= settings.YOUTUBE_SHORTS_MAX_DURATION_S:
            checks.append({
                "name": "duration_youtube",
                "passed": True,
                "severity": "blocker",
                "detail": f"Duration {dur:.2f}s within YouTube Shorts limit (<= 180s)",
            })
        else:
            checks.append({
                "name": "duration_youtube",
                "passed": False,
                "severity": "blocker",
                "detail": f"Duration {dur:.2f}s exceeds YouTube Shorts limit (max 180s)",
            })

    # Target range warning: 15–90s
    if settings.TARGET_MIN_DURATION_S <= dur <= settings.TARGET_MAX_DURATION_S:
        checks.append({
            "name": "duration_target_range",
            "passed": True,
            "severity": "warn",
            "detail": f"Duration {dur:.2f}s within target range (15–90s)",
        })
    else:
        checks.append({
            "name": "duration_target_range",
            "passed": False,
            "severity": "warn",
            "detail": f"Duration {dur:.2f}s outside target range (15–90s)",
        })

    # Sync merge drift check (warn if diff > 0.5s)
    if is_existing_video or sync_merge_duration is None:
        checks.append({
            "name": "duration_sync_merge_drift",
            "passed": True,
            "severity": "warn",
            "detail": "skipped – existing video mode",
        })
    else:
        drift = abs(dur - sync_merge_duration)
        if drift <= settings.MAX_DURATION_DRIFT_S:
            checks.append({
                "name": "duration_sync_merge_drift",
                "passed": True,
                "severity": "warn",
                "detail": f"Duration drift {drift:.2f}s <= {settings.MAX_DURATION_DRIFT_S}s limit",
            })
        else:
            checks.append({
                "name": "duration_sync_merge_drift",
                "passed": False,
                "severity": "warn",
                "detail": f"Duration drift {drift:.2f}s exceeds {settings.MAX_DURATION_DRIFT_S}s limit (probe={dur:.2f}s, plan={sync_merge_duration:.2f}s)",
            })

    return checks


def check_audio(probe: ProbeData, platforms: List[str]) -> List[Dict[str, Any]]:
    """Audio checks: stream present (B), not silent (B), AAC codec on Instagram (W)."""
    checks = []

    # Blocker: no audio stream
    if not probe.has_audio:
        checks.append({
            "name": "audio_stream_present",
            "passed": False,
            "severity": "blocker",
            "detail": "No audio stream present in media file",
        })
        checks.append({
            "name": "audio_volume_level",
            "passed": False,
            "severity": "blocker",
            "detail": "Audio track missing; unable to verify volume level",
        })
        return checks

    checks.append({
        "name": "audio_stream_present",
        "passed": True,
        "severity": "blocker",
        "detail": f"Audio stream present (codec '{probe.audio_codec}')",
    })

    # Blocker: audio is silent (mean_volume <= -60 dB)
    if probe.mean_volume_db is None:
        checks.append({
            "name": "audio_volume_level",
            "passed": False,
            "severity": "blocker",
            "detail": "Could not determine audio volume level via volumedetect",
        })
    elif probe.mean_volume_db <= settings.SILENT_AUDIO_THRESHOLD_DB:
        checks.append({
            "name": "audio_volume_level",
            "passed": False,
            "severity": "blocker",
            "detail": f"Audio is silent (mean_volume {probe.mean_volume_db:.1f} dB <= {settings.SILENT_AUDIO_THRESHOLD_DB} dB)",
        })
    else:
        checks.append({
            "name": "audio_volume_level",
            "passed": True,
            "severity": "blocker",
            "detail": f"Audio volume verified (mean_volume {probe.mean_volume_db:.1f} dB)",
        })

    # Instagram: audio codec AAC (W)
    if "instagram" in platforms:
        if probe.audio_codec.lower() == settings.INSTAGRAM_RECOMMENDED_AUDIO_CODEC:
            checks.append({
                "name": "audio_codec_instagram",
                "passed": True,
                "severity": "warn",
                "detail": f"Audio codec is {settings.INSTAGRAM_RECOMMENDED_AUDIO_CODEC.upper()}",
            })
        else:
            checks.append({
                "name": "audio_codec_instagram",
                "passed": False,
                "severity": "warn",
                "detail": f"Audio codec is '{probe.audio_codec}', recommended is AAC",
            })

    return checks


def check_file_size(probe: ProbeData, platforms: List[str]) -> List[Dict[str, Any]]:
    """B: Instagram file size over 300 MB."""
    checks = []
    if "instagram" in platforms:
        size = probe.file_size_bytes
        limit = settings.INSTAGRAM_MAX_FILE_SIZE_BYTES
        if size <= limit:
            mb = size / (1024 * 1024)
            checks.append({
                "name": "file_size_instagram",
                "passed": True,
                "severity": "blocker",
                "detail": f"File size {mb:.1f} MB within Instagram 300 MB limit",
            })
        else:
            mb = size / (1024 * 1024)
            checks.append({
                "name": "file_size_instagram",
                "passed": False,
                "severity": "blocker",
                "detail": f"File size {mb:.1f} MB exceeds Instagram 300 MB limit",
            })
    return checks
