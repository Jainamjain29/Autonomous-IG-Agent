"""Subtitles, scene timings, and narration word coverage checks."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.qc import settings


@dataclass
class SubtitleCue:
    start_s: float
    end_s: float
    text: str


def parse_srt(srt_content: str) -> List[SubtitleCue]:
    """Parse SRT string into list of SubtitleCue objects."""
    cues: List[SubtitleCue] = []
    blocks = re.split(r"\n\s*\n", srt_content.strip())

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        time_line_idx = -1
        for idx, line in enumerate(lines):
            if "-->" in line:
                time_line_idx = idx
                break

        if time_line_idx == -1:
            continue

        time_parts = lines[time_line_idx].split("-->")
        if len(time_parts) != 2:
            continue

        def parse_timestamp(ts: str) -> float:
            ts = ts.strip().replace(",", ".")
            parts = ts.split(":")
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            return float(ts)

        try:
            start_s = parse_timestamp(time_parts[0])
            end_s = parse_timestamp(time_parts[1])
            text = " ".join(lines[time_line_idx + 1:])
            cues.append(SubtitleCue(start_s=start_s, end_s=end_s, text=text))
        except Exception:
            continue

    return cues


def check_subtitles(
    srt_path: Optional[str | Path],
    video_duration: float,
    is_existing_video: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """Check SRT file validity (B or W in existing-video) and cue coverage (W).
    
    Returns (checks_list, combined_srt_text).
    """
    checks = []
    combined_srt_text = ""
    severity = "warn" if is_existing_video else "blocker"

    if not srt_path:
        checks.append({
            "name": "subtitles_validity",
            "passed": False,
            "severity": severity,
            "detail": "SRT path missing or not provided" + (" (warn only in existing video mode)" if is_existing_video else ""),
        })
        checks.append({
            "name": "subtitles_cue_coverage",
            "passed": False,
            "severity": "warn",
            "detail": "Cannot calculate subtitle coverage (SRT file missing)",
        })
        return checks, combined_srt_text

    p = Path(srt_path)
    if not p.is_file():
        checks.append({
            "name": "subtitles_validity",
            "passed": False,
            "severity": severity,
            "detail": f"SRT file not found: {p}" + (" (warn only in existing video mode)" if is_existing_video else ""),
        })
        checks.append({
            "name": "subtitles_cue_coverage",
            "passed": False,
            "severity": "warn",
            "detail": "Cannot calculate subtitle coverage (SRT file missing)",
        })
        return checks, combined_srt_text

    try:
        content = p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        checks.append({
            "name": "subtitles_validity",
            "passed": False,
            "severity": severity,
            "detail": f"Could not read SRT file: {e}",
        })
        checks.append({
            "name": "subtitles_cue_coverage",
            "passed": False,
            "severity": "warn",
            "detail": "Cannot calculate subtitle coverage (SRT unreadable)",
        })
        return checks, combined_srt_text

    if not content:
        checks.append({
            "name": "subtitles_validity",
            "passed": False,
            "severity": severity,
            "detail": "SRT file is empty",
        })
        checks.append({
            "name": "subtitles_cue_coverage",
            "passed": False,
            "severity": "warn",
            "detail": "Subtitle coverage 0.0% (empty file)",
        })
        return checks, combined_srt_text

    cues = parse_srt(content)
    if not cues:
        checks.append({
            "name": "subtitles_validity",
            "passed": False,
            "severity": severity,
            "detail": "SRT file is unparseable or contains no valid timestamp cues",
        })
        checks.append({
            "name": "subtitles_cue_coverage",
            "passed": False,
            "severity": "warn",
            "detail": "Subtitle coverage 0.0% (unparseable cues)",
        })
        return checks, combined_srt_text

    checks.append({
        "name": "subtitles_validity",
        "passed": True,
        "severity": severity,
        "detail": f"SRT file valid ({len(cues)} cues parsed)",
    })

    combined_srt_text = " ".join(c.text for c in cues)

    # Calculate subtitle coverage
    # Merge overlapping intervals for total speech coverage
    sorted_intervals = sorted([(c.start_s, c.end_s) for c in cues], key=lambda x: x[0])
    merged = []
    for start, end in sorted_intervals:
        if not merged:
            merged.append([start, end])
        else:
            prev = merged[-1]
            if start <= prev[1]:
                prev[1] = max(prev[1], end)
            else:
                merged.append([start, end])

    total_covered_s = sum(end - start for start, end in merged)
    dur = max(video_duration, 0.1)
    coverage_ratio = min(1.0, total_covered_s / dur)

    if coverage_ratio >= settings.SUBTITLE_MIN_COVERAGE_RATIO:
        checks.append({
            "name": "subtitles_cue_coverage",
            "passed": True,
            "severity": "warn",
            "detail": f"Subtitle cues cover {coverage_ratio * 100:.1f}% of duration (>= 80% threshold)",
        })
    else:
        checks.append({
            "name": "subtitles_cue_coverage",
            "passed": False,
            "severity": "warn",
            "detail": f"Subtitle cues cover {coverage_ratio * 100:.1f}% of duration (< 80% threshold)",
        })

    return checks, combined_srt_text


def check_scene_timings(
    plan_scenes: List[Dict[str, Any]],
    scene_timings: List[Dict[str, Any]],
    video_duration: float,
    is_existing_video: bool = False,
) -> List[Dict[str, Any]]:
    """Scene coverage checks: every scene_id present in bounds (B), no overlaps or gaps > 1.0s (W)."""
    checks = []

    if is_existing_video:
        checks.append({
            "name": "scene_timings_validity",
            "passed": True,
            "severity": "blocker",
            "detail": "skipped – existing video mode",
        })
        checks.append({
            "name": "scene_timings_continuity",
            "passed": True,
            "severity": "warn",
            "detail": "skipped – existing video mode",
        })
        return checks

    # Blocker check: plan scenes must exist and have timings within [0, duration]
    timing_map = {}
    for st in scene_timings:
        sid = st.get("scene_id")
        if sid is not None:
            timing_map[sid] = st

    missing_scenes = []
    out_of_bounds = []

    for scene in plan_scenes:
        sid = scene.get("id") if "id" in scene else scene.get("scene_id")
        if sid not in timing_map:
            missing_scenes.append(sid)
            continue
        timing = timing_map[sid]
        start_s = timing.get("start_s", -1.0)
        end_s = timing.get("end_s", -1.0)
        if start_s < 0.0 or end_s > (video_duration + 0.5) or start_s >= end_s:
            out_of_bounds.append((sid, start_s, end_s))

    if missing_scenes or out_of_bounds:
        errs = []
        if missing_scenes:
            errs.append(f"scenes missing from scene_timings: {missing_scenes}")
        if out_of_bounds:
            errs.append(f"scenes out of bounds: {out_of_bounds}")
        checks.append({
            "name": "scene_timings_validity",
            "passed": False,
            "severity": "blocker",
            "detail": "; ".join(errs),
        })
    else:
        checks.append({
            "name": "scene_timings_validity",
            "passed": True,
            "severity": "blocker",
            "detail": f"All {len(plan_scenes)} scenes have valid timings within 0..{video_duration:.2f}s",
        })

    # Warn check: continuity (overlapping timings or gaps over 1.0 s)
    if not scene_timings:
        checks.append({
            "name": "scene_timings_continuity",
            "passed": False,
            "severity": "warn",
            "detail": "No scene timings available to check continuity",
        })
        return checks

    sorted_timings = sorted(scene_timings, key=lambda x: x.get("start_s", 0.0))
    has_overlap = False
    has_gap = False
    issues = []

    for i in range(len(sorted_timings) - 1):
        curr_end = sorted_timings[i].get("end_s", 0.0)
        next_start = sorted_timings[i + 1].get("start_s", 0.0)

        if next_start < (curr_end - 0.05):
            has_overlap = True
            issues.append(f"overlap between scene {sorted_timings[i].get('scene_id')} and {sorted_timings[i+1].get('scene_id')} ({curr_end:.2f}s > {next_start:.2f}s)")
        elif (next_start - curr_end) > settings.MAX_SCENE_GAP_S:
            has_gap = True
            gap_s = next_start - curr_end
            issues.append(f"gap {gap_s:.2f}s > 1.0s between scene {sorted_timings[i].get('scene_id')} and {sorted_timings[i+1].get('scene_id')}")

    if has_overlap or has_gap:
        checks.append({
            "name": "scene_timings_continuity",
            "passed": False,
            "severity": "warn",
            "detail": "; ".join(issues),
        })
    else:
        checks.append({
            "name": "scene_timings_continuity",
            "passed": True,
            "severity": "warn",
            "detail": "Scene timings continuous without overlaps or gaps > 1.0s",
        })

    return checks


def normalize_words(text: str) -> List[str]:
    """Extract lowercase alphanumeric words from text."""
    return re.findall(r"[a-z0-9]+", text.lower())


def check_narration_coverage(
    plan_text_or_scenes: Any,
    srt_text: str,
    is_existing_video: bool = False,
) -> Dict[str, Any]:
    """Narration vs SRT word coverage check: W below 0.85, B below 0.60."""
    if is_existing_video:
        return {
            "name": "narration_word_coverage",
            "passed": True,
            "severity": "blocker",
            "detail": "skipped – existing video mode",
        }

    # Extract all narration words from plan
    narration_parts = []
    if isinstance(plan_text_or_scenes, list):
        for scene in plan_text_or_scenes:
            if isinstance(scene, dict):
                narration_parts.append(scene.get("narration", ""))
    elif isinstance(plan_text_or_scenes, str):
        narration_parts.append(plan_text_or_scenes)

    plan_narration = " ".join(narration_parts)
    plan_words = normalize_words(plan_narration)

    if not plan_words:
        return {
            "name": "narration_word_coverage",
            "passed": True,
            "severity": "blocker",
            "detail": "No narration text in plan to verify",
        }

    srt_word_set = set(normalize_words(srt_text))
    matched_count = sum(1 for word in plan_words if word in srt_word_set)
    coverage = matched_count / len(plan_words)

    if coverage >= settings.NARRATION_COVERAGE_WARN_THRESHOLD:
        return {
            "name": "narration_word_coverage",
            "passed": True,
            "severity": "blocker",
            "detail": f"Narration word coverage {coverage * 100:.1f}% >= 85%",
        }
    elif coverage >= settings.NARRATION_COVERAGE_BLOCKER_THRESHOLD:
        return {
            "name": "narration_word_coverage",
            "passed": False,
            "severity": "warn",
            "detail": f"Narration word coverage {coverage * 100:.1f}% below warning threshold 85% (>= 60%)",
        }
    else:
        return {
            "name": "narration_word_coverage",
            "passed": False,
            "severity": "blocker",
            "detail": f"Narration word coverage {coverage * 100:.1f}% below blocker threshold 60%",
        }
