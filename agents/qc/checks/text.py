"""Platform text and hashtag compliance checks."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set

from agents.qc import settings


def extract_deduped_hashtags(caption: str, hashtags_list: List[str]) -> List[str]:
    """Extract and deduplicate hashtags from caption text and plan hashtags list."""
    seen_lower: Set[str] = set()
    deduped: List[str] = []

    # 1. From explicit hashtags list
    for tag in hashtags_list or []:
        cleaned = tag.strip()
        if not cleaned:
            continue
        if not cleaned.startswith("#"):
            cleaned = f"#{cleaned}"
        low = cleaned.lower()
        if low not in seen_lower:
            seen_lower.add(low)
            deduped.append(cleaned)

    # 2. From caption
    caption_tags = re.findall(r"#[a-zA-Z0-9_]+", caption or "")
    for tag in caption_tags:
        cleaned = tag.strip()
        low = cleaned.lower()
        if low not in seen_lower:
            seen_lower.add(low)
            deduped.append(cleaned)

    return deduped


def check_text_compliance(plan: Dict[str, Any], platforms: List[str]) -> List[Dict[str, Any]]:
    """Verify platform metadata, caption lengths, hashtags, and hook lines."""
    checks: List[Dict[str, Any]] = []

    # Extract platform specific fields with fallback to top-level fields
    platform_text = plan.get("platform_text", {})
    ig_data = platform_text.get("instagram", {})
    yt_data = platform_text.get("youtube", {})

    ig_caption = ig_data.get("caption") if isinstance(ig_data, dict) and "caption" in ig_data else plan.get("caption", "")
    yt_title = yt_data.get("title") if isinstance(yt_data, dict) and "title" in yt_data else plan.get("title", "")
    yt_description = yt_data.get("description") if isinstance(yt_data, dict) and "description" in yt_data else plan.get("caption", "")

    raw_hashtags = plan.get("hashtags", [])

    # Check 1: Non-empty required fields per platform (Blocker)
    for p in platforms:
        if p == "instagram":
            if ig_caption and ig_caption.strip():
                checks.append({
                    "name": "text_instagram_caption_present",
                    "passed": True,
                    "severity": "blocker",
                    "detail": f"Instagram caption present ({len(ig_caption)} characters)",
                })
            else:
                checks.append({
                    "name": "text_instagram_caption_present",
                    "passed": False,
                    "severity": "blocker",
                    "detail": "Instagram caption is empty or missing",
                })
        elif p == "youtube":
            if yt_title and yt_title.strip():
                checks.append({
                    "name": "text_youtube_title_present",
                    "passed": True,
                    "severity": "blocker",
                    "detail": f"YouTube title present ({len(yt_title)} characters)",
                })
            else:
                checks.append({
                    "name": "text_youtube_title_present",
                    "passed": False,
                    "severity": "blocker",
                    "detail": "YouTube title is empty or missing",
                })

    # Instagram specific text rules
    if "instagram" in platforms:
        # B: caption over 2,200 characters
        cap_len = len(ig_caption or "")
        if cap_len <= settings.INSTAGRAM_MAX_CAPTION_LENGTH:
            checks.append({
                "name": "text_instagram_caption_length",
                "passed": True,
                "severity": "blocker",
                "detail": f"Caption length {cap_len} <= {settings.INSTAGRAM_MAX_CAPTION_LENGTH} characters",
            })
        else:
            checks.append({
                "name": "text_instagram_caption_length",
                "passed": False,
                "severity": "blocker",
                "detail": f"Caption length {cap_len} exceeds {settings.INSTAGRAM_MAX_CAPTION_LENGTH} characters",
            })

        # Hashtags counting (deduped)
        deduped_tags = extract_deduped_hashtags(ig_caption or "", raw_hashtags)
        tag_count = len(deduped_tags)

        # B: more than 5 hashtags
        if tag_count <= settings.INSTAGRAM_MAX_HASHTAGS:
            checks.append({
                "name": "text_instagram_hashtags_max",
                "passed": True,
                "severity": "blocker",
                "detail": f"Hashtag count {tag_count} <= {settings.INSTAGRAM_MAX_HASHTAGS} limit",
            })
        else:
            checks.append({
                "name": "text_instagram_hashtags_max",
                "passed": False,
                "severity": "blocker",
                "detail": f"Hashtag count {tag_count} exceeds {settings.INSTAGRAM_MAX_HASHTAGS} limit ({deduped_tags})",
            })

        # W: fewer than 3 hashtags
        if tag_count >= settings.INSTAGRAM_MIN_HASHTAGS:
            checks.append({
                "name": "text_instagram_hashtags_min",
                "passed": True,
                "severity": "warn",
                "detail": f"Hashtag count {tag_count} >= {settings.INSTAGRAM_MIN_HASHTAGS} recommended minimum",
            })
        else:
            checks.append({
                "name": "text_instagram_hashtags_min",
                "passed": False,
                "severity": "warn",
                "detail": f"Hashtag count {tag_count} below {settings.INSTAGRAM_MIN_HASHTAGS} recommended minimum",
            })

        # W: first line (hook) over 125 characters
        first_line = (ig_caption or "").split("\n")[0].strip() if ig_caption else ""
        hook_len = len(first_line)
        if hook_len <= settings.INSTAGRAM_MAX_HOOK_LENGTH:
            checks.append({
                "name": "text_instagram_hook_length",
                "passed": True,
                "severity": "warn",
                "detail": f"First line hook length {hook_len} <= {settings.INSTAGRAM_MAX_HOOK_LENGTH} characters",
            })
        else:
            checks.append({
                "name": "text_instagram_hook_length",
                "passed": False,
                "severity": "warn",
                "detail": f"First line hook length {hook_len} exceeds {settings.INSTAGRAM_MAX_HOOK_LENGTH} characters",
            })

    # YouTube specific text rules
    if "youtube" in platforms:
        # B: title over 100 characters
        title_len = len(yt_title or "")
        if 0 < title_len <= settings.YOUTUBE_MAX_TITLE_LENGTH:
            checks.append({
                "name": "text_youtube_title_length",
                "passed": True,
                "severity": "blocker",
                "detail": f"YouTube title length {title_len} <= {settings.YOUTUBE_MAX_TITLE_LENGTH} characters",
            })
        elif title_len > settings.YOUTUBE_MAX_TITLE_LENGTH:
            checks.append({
                "name": "text_youtube_title_length",
                "passed": False,
                "severity": "blocker",
                "detail": f"YouTube title length {title_len} exceeds {settings.YOUTUBE_MAX_TITLE_LENGTH} characters",
            })

        # B: description over 5,000 characters
        desc_len = len(yt_description or "")
        if desc_len <= settings.YOUTUBE_MAX_DESCRIPTION_LENGTH:
            checks.append({
                "name": "text_youtube_description_length",
                "passed": True,
                "severity": "blocker",
                "detail": f"YouTube description length {desc_len} <= {settings.YOUTUBE_MAX_DESCRIPTION_LENGTH} characters",
            })
        else:
            checks.append({
                "name": "text_youtube_description_length",
                "passed": False,
                "severity": "blocker",
                "detail": f"YouTube description length {desc_len} exceeds {settings.YOUTUBE_MAX_DESCRIPTION_LENGTH} characters",
            })

    return checks
