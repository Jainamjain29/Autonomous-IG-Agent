"""Video probing utilities using ffprobe / ffmpeg subprocesses."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple, Union

import imageio_ffmpeg


def _find_ffprobe_exe() -> Optional[str]:
    """Locate ffprobe executable if available in PATH or near ffmpeg."""
    exe = shutil.which("ffprobe")
    if exe:
        return exe

    # Check next to imageio_ffmpeg binary
    try:
        ffmpeg_exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        sibling = ffmpeg_exe.parent / "ffprobe.exe"
        if sibling.exists():
            return str(sibling)
        sibling_no_ext = ffmpeg_exe.parent / "ffprobe"
        if sibling_no_ext.exists():
            return str(sibling_no_ext)
    except Exception:
        pass

    return None


def probe_video(video_path: Union[str, Path]) -> Tuple[float, int, int]:
    """Probe an MP4 video file and return (duration_s, width, height).
    
    Uses ffprobe subprocess when available; falls back to ffmpeg -i parsing.
    Raises RuntimeError if probing fails or values cannot be extracted.
    """
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {path}")

    # Attempt ffprobe first
    ffprobe_exe = _find_ffprobe_exe()
    if ffprobe_exe:
        cmd = [
            ffprobe_exe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            width = int(streams[0].get("width", 0)) if streams else 0
            height = int(streams[0].get("height", 0)) if streams else 0
            
            # Duration can be in stream or format
            duration_s = 0.0
            if streams and streams[0].get("duration"):
                duration_s = float(streams[0]["duration"])
            elif data.get("format", {}).get("duration"):
                duration_s = float(data["format"]["duration"])

            if width > 0 and height > 0 and duration_s > 0:
                return (round(duration_s, 2), width, height)
        except Exception:
            # Fall through to ffmpeg probe
            pass

    # Fallback to ffmpeg -i
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        res = subprocess.run([ffmpeg_exe, "-i", str(path)], capture_output=True, text=True)
        stderr = res.stderr or ""
    except Exception as e:
        raise RuntimeError(f"Failed to execute FFmpeg probe on {path}: {e}") from e

    # Parse duration: Duration: 00:00:03.58
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", stderr)
    if not dur_match:
        raise RuntimeError(f"Could not parse duration from video probe: {path}\n{stderr}")
    hours, minutes, seconds = dur_match.groups()
    duration_s = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    # Parse video resolution: Stream #0:0... Video: ... 1080x1920
    res_match = re.search(r"Stream.*?Video:.*?,\s*(\d{2,5})x(\d{2,5})", stderr, re.DOTALL)
    if not res_match:
        raise RuntimeError(f"Could not parse resolution from video probe: {path}\n{stderr}")
    width = int(res_match.group(1))
    height = int(res_match.group(2))

    return (round(duration_s, 2), width, height)
