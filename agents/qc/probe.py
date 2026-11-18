"""Media probing utilities wrapping ffprobe and ffmpeg."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("qc.probe")


@dataclass
class ProbeData:
    """Structured media probe data extracted from a video file."""
    file_exists: bool = False
    readable: bool = False
    file_size_bytes: int = 0
    width: int = 0
    height: int = 0
    aspect_ratio: float = 0.0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    has_audio: bool = False
    mean_volume_db: Optional[float] = None
    duration_s: float = 0.0
    is_faststart: bool = False
    error: Optional[str] = None
    raw_streams: list[Dict[str, Any]] = field(default_factory=list)


def find_ffmpeg_executable() -> Optional[str]:
    """Find ffmpeg binary via PATH or imageio-ffmpeg."""
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


def find_ffprobe_executable() -> Optional[str]:
    """Find ffprobe binary via PATH or adjacent to ffmpeg."""
    found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if found:
        return found
    ffmpeg_exe = find_ffmpeg_executable()
    if ffmpeg_exe:
        adjacent = os.path.join(os.path.dirname(ffmpeg_exe), "ffprobe.exe")
        if os.path.exists(adjacent):
            return adjacent
        adjacent_nix = os.path.join(os.path.dirname(ffmpeg_exe), "ffprobe")
        if os.path.exists(adjacent_nix):
            return adjacent_nix
    return None


def check_mp4_faststart(video_path: str | Path) -> bool:
    """Check if MP4 file has moov atom before mdat atom (faststart progressive download)."""
    p = Path(video_path)
    if not p.is_file():
        return False

    try:
        with open(p, "rb") as f:
            moov_offset = None
            mdat_offset = None
            current_offset = 0

            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                size, atom_type_raw = struct.unpack(">I4s", header)
                atom_type = atom_type_raw.decode("latin1", errors="replace")

                if atom_type == "moov" and moov_offset is None:
                    moov_offset = current_offset
                elif atom_type == "mdat" and mdat_offset is None:
                    mdat_offset = current_offset

                if moov_offset is not None and mdat_offset is not None:
                    break

                if size == 1:
                    ext_size_bytes = f.read(8)
                    if len(ext_size_bytes) < 8:
                        break
                    large_size = struct.unpack(">Q", ext_size_bytes)[0]
                    f.seek(large_size - 16, os.SEEK_CUR)
                    current_offset += large_size
                elif size == 0:
                    break
                else:
                    f.seek(size - 8, os.SEEK_CUR)
                    current_offset += size

            if moov_offset is not None and mdat_offset is not None:
                return moov_offset < mdat_offset
            return False
    except Exception as e:
        logger.debug(f"Faststart check error: {e}")
        return False


def _parse_timecode(tc: str) -> float:
    """Parse HH:MM:SS.ss timecode into seconds."""
    parts = tc.split(":")
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    return float(tc)


def probe_with_ffmpeg_cli(video_path: Path, ffmpeg_exe: str, probe_data: ProbeData) -> ProbeData:
    """Probe video attributes using ffmpeg -i."""
    try:
        res = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-i", str(video_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = res.stderr

        # Check for duration
        dur_match = re.search(r"Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)", output)
        if dur_match:
            probe_data.duration_s = _parse_timecode(dur_match.group(1))

        # Check video stream
        v_match = re.search(
            r"Stream\s+#\d+:\d+.*?Video:\s*([a-zA-Z0-9_-]+).*?,\s*(\d{2,5})x(\d{2,5})",
            output,
        )
        if v_match:
            probe_data.video_codec = v_match.group(1).lower()
            probe_data.width = int(v_match.group(2))
            probe_data.height = int(v_match.group(3))
            if probe_data.height > 0:
                probe_data.aspect_ratio = probe_data.width / probe_data.height

        # Check fps
        fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", output)
        if fps_match:
            probe_data.fps = float(fps_match.group(1))
        else:
            tbr_match = re.search(r"(\d+(?:\.\d+)?)\s*tbr", output)
            if tbr_match:
                probe_data.fps = float(tbr_match.group(1))

        # Check audio stream
        a_match = re.search(r"Stream\s+#\d+:\d+.*?Audio:\s*([a-zA-Z0-9_-]+)", output)
        if a_match:
            probe_data.has_audio = True
            probe_data.audio_codec = a_match.group(1).lower()

        # If has audio, run volumedetect
        if probe_data.has_audio:
            vol_res = subprocess.run(
                [ffmpeg_exe, "-hide_banner", "-i", str(video_path), "-af", "volumedetect", "-f", "null", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            vol_output = vol_res.stderr
            mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", vol_output)
            if mean_match:
                probe_data.mean_volume_db = float(mean_match.group(1))

        probe_data.readable = probe_data.width > 0 and probe_data.height > 0
    except Exception as e:
        probe_data.readable = False
        probe_data.error = str(e)

    return probe_data


def probe_with_ffprobe_cli(video_path: Path, ffprobe_exe: str, probe_data: ProbeData) -> ProbeData:
    """Probe video attributes using ffprobe json output."""
    try:
        cmd = [
            ffprobe_exe,
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            str(video_path),
        ]
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode != 0:
            probe_data.readable = False
            probe_data.error = res.stderr.strip()
            return probe_data

        data = json.loads(res.stdout)
        probe_data.raw_streams = data.get("streams", [])
        format_info = data.get("format", {})

        if "duration" in format_info:
            probe_data.duration_s = float(format_info["duration"])

        for stream in probe_data.raw_streams:
            stype = stream.get("codec_type")
            if stype == "video" and not probe_data.video_codec:
                probe_data.video_codec = stream.get("codec_name", "").lower()
                probe_data.width = int(stream.get("width", 0))
                probe_data.height = int(stream.get("height", 0))
                if probe_data.height > 0:
                    probe_data.aspect_ratio = probe_data.width / probe_data.height

                # Extract frame rate
                r_frame = stream.get("r_frame_rate", "")
                if "/" in r_frame:
                    num, den = r_frame.split("/")
                    if float(den) > 0:
                        probe_data.fps = float(num) / float(den)
                elif r_frame:
                    probe_data.fps = float(r_frame)

            elif stype == "audio" and not probe_data.has_audio:
                probe_data.has_audio = True
                probe_data.audio_codec = stream.get("codec_name", "").lower()

        # Audio volume detect via ffmpeg
        if probe_data.has_audio:
            ffmpeg_exe = find_ffmpeg_executable()
            if ffmpeg_exe:
                vol_res = subprocess.run(
                    [ffmpeg_exe, "-hide_banner", "-i", str(video_path), "-af", "volumedetect", "-f", "null", "-"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", vol_res.stderr)
                if mean_match:
                    probe_data.mean_volume_db = float(mean_match.group(1))

        probe_data.readable = probe_data.width > 0 and probe_data.height > 0
    except Exception as e:
        probe_data.readable = False
        probe_data.error = str(e)

    return probe_data


def probe_video(video_path: str | Path) -> ProbeData:
    """Probe all metadata for a video file, with ffprobe and ffmpeg fallbacks."""
    p = Path(video_path)
    probe_data = ProbeData()

    if not p.is_file():
        probe_data.file_exists = False
        probe_data.readable = False
        probe_data.error = f"Video file not found: {p}"
        return probe_data

    probe_data.file_exists = True
    probe_data.file_size_bytes = p.stat().st_size
    probe_data.is_faststart = check_mp4_faststart(p)

    ffprobe_exe = find_ffprobe_executable()
    if ffprobe_exe:
        return probe_with_ffprobe_cli(p, ffprobe_exe, probe_data)

    ffmpeg_exe = find_ffmpeg_executable()
    if ffmpeg_exe:
        return probe_with_ffmpeg_cli(p, ffmpeg_exe, probe_data)

    probe_data.readable = False
    probe_data.error = "Neither ffprobe nor ffmpeg executable could be located."
    return probe_data
