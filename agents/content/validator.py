"""Validation and sanitization rules for Content Agent outputs."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.content.settings import (
    BRAND_HANDLE,
    BRAND_NAME,
    IG_CAPTION_MAX_CHARS,
    IG_MAX_HASHTAGS,
    MAX_WORDS_PER_SCENE,
    SCENE_COUNT_MAX,
    SCENE_COUNT_MIN,
    SCENE_DURATION_MAX_S,
    SCENE_DURATION_MIN_S,
    SPEECH_BUFFER_S,
    SPEECH_WORDS_PER_SECOND,
    TARGET_LENGTH_MAX_S,
    TARGET_LENGTH_MIN_S,
    VISUAL_PROMPT_STYLE_SUFFIX,
    YT_DESCRIPTION_MAX_CHARS,
    YT_TITLE_MAX_CHARS,
)


def compute_scene_duration(narration: str) -> float:
    """Compute scene duration_hint_s from narration words: words / 2.5 + 0.5s, rounded up to 0.5s, clamped 3–8s."""
    words = len(re.findall(r"\S+", narration)) if isinstance(narration, str) else 0
    if words == 0:
        return SCENE_DURATION_MIN_S
    raw_s = (words / SPEECH_WORDS_PER_SECOND) + SPEECH_BUFFER_S
    rounded_s = math.ceil(raw_s * 2.0) / 2.0
    return max(SCENE_DURATION_MIN_S, min(SCENE_DURATION_MAX_S, rounded_s))


def narration_needs_more_than_8s(narration: str) -> bool:
    """Check if narration requires more than 8 seconds of speech time."""
    words = len(re.findall(r"\S+", narration)) if isinstance(narration, str) else 0
    raw_s = (words / SPEECH_WORDS_PER_SECOND) + SPEECH_BUFFER_S
    return raw_s > SCENE_DURATION_MAX_S


def correct_brand_name(text: str, brand_name: str = BRAND_NAME) -> str:
    """Correct case-insensitive 'streamovate' to BRAND_NAME except inside @handle and #hashtags."""
    if not isinstance(text, str):
        return ""
    # Negative lookbehind ensures we do not replace @streamovate or #streamovate or mid-word letters
    return re.sub(r"(?<![@#\w])streamovate\b", brand_name, text, flags=re.IGNORECASE)


def ensure_visual_prompt_style(visual_prompt: str, suffix: str = VISUAL_PROMPT_STYLE_SUFFIX) -> str:
    """Append the standardized style suffix to visual_prompt without duplicating."""
    vp = visual_prompt.strip() if isinstance(visual_prompt, str) else ""
    if not vp:
        return suffix
    norm_suffix = suffix.rstrip(".").lower()
    if norm_suffix in vp.lower():
        return vp
    if not vp.endswith("."):
        vp += "."
    return f"{vp} {suffix}"


def split_narration_at_boundary(text: str) -> List[str]:
    """Split narration into sub-segments that each require <= 8s at sentence or clause boundaries."""
    if not narration_needs_more_than_8s(text):
        return [text.strip()]

    # Try sentence boundaries first (. ! ?)
    m = list(re.finditer(r"(?<=[.!?])\s+", text))
    best_split = None
    min_dist = float("inf")
    mid_idx = len(text) // 2

    for match in m:
        idx = match.start()
        p1 = text[:idx].strip()
        p2 = text[match.end():].strip()
        if p1 and p2:
            dist = abs(idx - mid_idx)
            if dist < min_dist:
                min_dist = dist
                best_split = (p1, p2)

    if not best_split:
        # Try clause boundaries (, ; - --)
        m = list(re.finditer(r"(?<=[,;])\s+|(?<=\s)[—–-]\s+", text))
        for match in m:
            idx = match.start()
            p1 = text[:idx].strip()
            p2 = text[match.end():].strip()
            if p1 and p2:
                dist = abs(idx - mid_idx)
                if dist < min_dist:
                    min_dist = dist
                    best_split = (p1, p2)

    if not best_split:
        # Fallback: split at whitespace near midpoint
        word_list = text.split()
        mid_w = len(word_list) // 2
        p1 = " ".join(word_list[:mid_w])
        p2 = " ".join(word_list[mid_w:])
        best_split = (p1, p2)

    p1, p2 = best_split
    res: List[str] = []
    for p in (p1, p2):
        res.extend(split_narration_at_boundary(p))
    return res


def split_long_script_scenes(scenes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """In script mode, split any scene whose narration needs > 8s so words stay verbatim."""
    new_scenes: List[Dict[str, Any]] = []
    for s in scenes:
        narration = s.get("narration", "")
        if narration_needs_more_than_8s(narration):
            sub_parts = split_narration_at_boundary(narration)
            for part in sub_parts:
                new_scene = dict(s)
                new_scene["narration"] = part
                new_scene["duration_hint_s"] = compute_scene_duration(part)
                new_scenes.append(new_scene)
        else:
            s_copy = dict(s)
            s_copy["duration_hint_s"] = compute_scene_duration(narration)
            new_scenes.append(s_copy)

    # Renumber IDs
    for idx, sc in enumerate(new_scenes, start=1):
        sc["id"] = idx
    return new_scenes


def extract_and_strip_hashtags(text: str) -> Tuple[str, List[str]]:
    """Extract all inline #tags from text and return (clean_text, extracted_tags)."""
    if not isinstance(text, str):
        return "", []

    found_tags = re.findall(r"#\w+", text)
    clean_text = re.sub(r"#\w+", "", text)
    clean_text = re.sub(r"[ \t]+", " ", clean_text)
    clean_text = re.sub(r" \n", "\n", clean_text)
    clean_text = re.sub(r"\n\s*\n+", "\n\n", clean_text).strip()
    return clean_text, found_tags


def clean_hashtags(raw_tags: List[str], max_count: int = IG_MAX_HASHTAGS) -> List[str]:
    """Clean, deduplicate case-insensitively, cap, and format hashtags with '#'."""
    cleaned: List[str] = []
    seen: set = set()

    for item in raw_tags:
        if not isinstance(item, str):
            continue
        tag = item.strip().lstrip("#")
        tag = re.sub(r"[^\w]", "", tag)
        if not tag:
            continue
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(f"#{tag}")
            if len(cleaned) >= max_count:
                break

    return cleaned


def sanitize_youtube_text(text: str) -> str:
    """Remove '<' and '>' characters from YouTube text."""
    if not isinstance(text, str):
        return ""
    return text.replace("<", "").replace(">", "").strip()


def check_verbatim_script(scenes: List[Dict[str, Any]], expected_script: str) -> Tuple[bool, str]:
    """Check that the joined narration of all scenes equals the user's script word-for-word."""
    joined_narration = " ".join(str(s.get("narration", "")) for s in scenes)
    norm_joined = " ".join(joined_narration.split())
    norm_expected = " ".join(expected_script.split())

    if norm_joined == norm_expected:
        return True, ""

    return (
        False,
        f"Script mismatch.\nExpected ({len(norm_expected.split())} words):\n{norm_expected}\n"
        f"Got ({len(norm_joined.split())} words):\n{norm_joined}",
    )


def ensure_character_in_visual_prompts(scenes: List[Dict[str, Any]], character_desc: str) -> None:
    """Guarantee character description is in every scene's visual_prompt without duplicate text."""
    desc_clean = character_desc.strip()
    desc_lower = desc_clean.lower()
    prefix = f"Character: {desc_clean}. "

    for s in scenes:
        if not isinstance(s, dict):
            continue
        vp = str(s.get("visual_prompt", "")).strip()
        if desc_lower not in vp.lower():
            s["visual_prompt"] = f"{prefix}{vp}".strip()


def check_character_in_visual_prompts(scenes: List[Dict[str, Any]], character_desc: str) -> Tuple[bool, str]:
    """Safety net: Ensure character description is present in every scene's visual_prompt."""
    target = character_desc.strip().lower()
    for s in scenes:
        vp = str(s.get("visual_prompt", "")).lower()
        if target not in vp:
            scene_id = s.get("id", "?")
            return False, f"Scene {scene_id} visual_prompt does not contain character description '{character_desc}'."
    return True, ""


def validate_plan(
    plan: Any,
    mode: str,
    expected_script: Optional[str] = None,
    character_desc: Optional[str] = None,
) -> List[str]:
    """Validate a plan dictionary against contract, schema, timing, and platform limits."""
    errors: List[str] = []

    if not isinstance(plan, dict):
        return [f"Output must be a JSON object, got {type(plan).__name__}"]

    required_keys = [
        "title",
        "topic",
        "hook",
        "script",
        "scenes",
        "caption",
        "hashtags",
        "platform_text",
    ]
    for key in required_keys:
        if key not in plan:
            errors.append(f"Missing required field: '{key}'")

    if errors:
        return errors

    # Check basic types
    if not isinstance(plan["title"], str):
        errors.append("Field 'title' must be a string")
    if not isinstance(plan["topic"], str):
        errors.append("Field 'topic' must be a string")
    if not isinstance(plan["hook"], str):
        errors.append("Field 'hook' must be a string")
    if not isinstance(plan["script"], str):
        errors.append("Field 'script' must be a string")
    if not isinstance(plan["scenes"], list):
        errors.append("Field 'scenes' must be an array")
    if not isinstance(plan["caption"], str):
        errors.append("Field 'caption' must be a string")
    if not isinstance(plan["hashtags"], list):
        errors.append("Field 'hashtags' must be an array")
    if not isinstance(plan["platform_text"], dict):
        errors.append("Field 'platform_text' must be an object")

    if errors:
        return errors

    # Platform text required subfields
    pt = plan["platform_text"]
    if not isinstance(pt.get("instagram"), dict):
        errors.append("Missing or invalid 'platform_text.instagram'")
    elif not isinstance(pt["instagram"].get("caption"), str):
        errors.append("Field 'platform_text.instagram.caption' must be a string")

    if not isinstance(pt.get("youtube"), dict):
        errors.append("Missing or invalid 'platform_text.youtube'")
    else:
        if not isinstance(pt["youtube"].get("title"), str):
            errors.append("Field 'platform_text.youtube.title' must be a string")
        if not isinstance(pt["youtube"].get("description"), str):
            errors.append("Field 'platform_text.youtube.description' must be a string")

    # Character limits
    yt_title = pt.get("youtube", {}).get("title", "")
    if len(yt_title) > YT_TITLE_MAX_CHARS:
        errors.append(f"YouTube title length ({len(yt_title)}) exceeds {YT_TITLE_MAX_CHARS} characters")

    yt_desc = pt.get("youtube", {}).get("description", "")
    if len(yt_desc) > YT_DESCRIPTION_MAX_CHARS:
        errors.append(f"YouTube description length ({len(yt_desc)}) exceeds {YT_DESCRIPTION_MAX_CHARS} characters")

    ig_caption = pt.get("instagram", {}).get("caption", "")
    if len(ig_caption) > IG_CAPTION_MAX_CHARS:
        errors.append(f"Instagram caption length ({len(ig_caption)}) exceeds {IG_CAPTION_MAX_CHARS} characters")

    # Mode-specific validation
    is_video_mode = mode == "video"
    scenes = plan["scenes"]

    if is_video_mode:
        if len(scenes) != 0:
            errors.append(f"Mode 'video' requires empty scenes list [], got {len(scenes)} scenes")
        if plan["script"] != "":
            errors.append(f"Mode 'video' requires empty script string \"\", got '{plan['script'][:30]}...'")
    else:
        scene_count = len(scenes)

        # In generate / character modes: check scene count and if narration needs > 8s
        if mode in ("generate", "character"):
            if not (SCENE_COUNT_MIN <= scene_count <= SCENE_COUNT_MAX):
                errors.append(
                    f"Scene count ({scene_count}) outside allowed range [{SCENE_COUNT_MIN}, {SCENE_COUNT_MAX}]"
                )
            for idx, scene in enumerate(scenes, start=1):
                if not isinstance(scene, dict):
                    errors.append(f"Scene at index {idx} must be a JSON object")
                    continue
                narration = str(scene.get("narration", ""))
                w_count = len(re.findall(r"\S+", narration))
                raw_s = (w_count / SPEECH_WORDS_PER_SECOND) + SPEECH_BUFFER_S
                if raw_s > SCENE_DURATION_MAX_S:
                    errors.append(
                        f"Scene {scene.get('id', idx)} narration has {w_count} words and needs {raw_s:.1f}s (> {SCENE_DURATION_MAX_S}s limit). "
                        f"Please shorten or split this scene (max {MAX_WORDS_PER_SCENE} words per scene)."
                    )

        elif mode == "script":
            # Script mode: max 8 scenes still applies; if longer than 8 scenes * 8s, fail with clear error
            if scene_count > SCENE_COUNT_MAX:
                errors.append(
                    f"Script requires {scene_count} scenes, exceeding the maximum allowed limit of {SCENE_COUNT_MAX} scenes."
                )
            elif scene_count < 1:
                errors.append("Mode 'script' requires at least 1 scene.")

        # Check scene items and total duration
        total_duration = 0.0
        for idx, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                continue
            scene_id = scene.get("id")
            if scene_id != idx:
                errors.append(f"Scene id at position {idx} must be {idx}, got {scene_id}")

            duration = scene.get("duration_hint_s")
            if duration is None or not isinstance(duration, (int, float)):
                errors.append(f"Scene {scene_id} missing valid 'duration_hint_s'")
            else:
                if not (SCENE_DURATION_MIN_S <= duration <= SCENE_DURATION_MAX_S):
                    errors.append(
                        f"Scene {scene_id} 'duration_hint_s' ({duration}) outside allowed bounds "
                        f"[{SCENE_DURATION_MIN_S}, {SCENE_DURATION_MAX_S}]"
                    )
                total_duration += float(duration)

        # Total duration check: 30–60s applies to generate and character only!
        if mode in ("generate", "character"):
            if SCENE_COUNT_MIN <= scene_count <= SCENE_COUNT_MAX:
                if not (TARGET_LENGTH_MIN_S <= total_duration <= TARGET_LENGTH_MAX_S):
                    errors.append(
                        f"Total duration sum ({total_duration:.1f}s) outside allowed target bounds "
                        f"[{TARGET_LENGTH_MIN_S}s, {TARGET_LENGTH_MAX_S}s]"
                    )
        elif mode == "script":
            # Script mode: no minimum duration; total follows script (capped at 8 scenes * 8s = 64s)
            max_script_duration = SCENE_COUNT_MAX * SCENE_DURATION_MAX_S
            if total_duration > max_script_duration:
                errors.append(
                    f"Total duration ({total_duration:.1f}s) exceeds maximum allowed script duration ({max_script_duration}s)."
                )

        # Hook validation: first scene's spoken line
        if scenes and isinstance(scenes[0], dict):
            first_narration = scenes[0].get("narration", "").strip()
            hook = plan.get("hook", "").strip()
            if hook != first_narration:
                errors.append(f"Hook must equal first spoken line (Scene 1 narration). Got hook='{hook}' vs scene 1='{first_narration}'")

        # Verbatim script check
        if mode == "script" and expected_script:
            matches, mismatch_err = check_verbatim_script(scenes, expected_script)
            if not matches:
                errors.append(mismatch_err)

        # Character presence check (safety net)
        if mode == "character" and character_desc:
            has_char, char_err = check_character_in_visual_prompts(scenes, character_desc)
            if not has_char:
                errors.append(char_err)

    return errors


def sanitize_and_finalize_plan(
    plan: Dict[str, Any],
    mode: str,
    character_desc: Optional[str] = None,
) -> Dict[str, Any]:
    """Sanitize fields, enforce speech timings, visual styles, brand casing, and assemble platform text."""
    is_video_mode = mode == "video"

    # 1. Correct brand name casing in text fields
    plan["title"] = correct_brand_name(plan.get("title", ""))
    plan["hook"] = correct_brand_name(plan.get("hook", ""))
    plan["caption"] = correct_brand_name(plan.get("caption", ""))
    if not is_video_mode:
        plan["script"] = correct_brand_name(plan.get("script", ""))

    # 2. Extract any inline hashtags from caption fields and merge with plan["hashtags"]
    raw_base_caption = plan.get("caption", "")
    clean_base_caption, inline_base_tags = extract_and_strip_hashtags(raw_base_caption)
    plan["caption"] = clean_base_caption

    raw_ig_caption = plan.get("platform_text", {}).get("instagram", {}).get("caption", clean_base_caption)
    clean_ig_caption, inline_ig_tags = extract_and_strip_hashtags(raw_ig_caption)

    raw_yt_desc = plan.get("platform_text", {}).get("youtube", {}).get("description", clean_base_caption)
    clean_yt_desc, inline_yt_tags = extract_and_strip_hashtags(raw_yt_desc)

    # Collect all tags (inline + list)
    all_raw_tags = list(plan.get("hashtags", [])) + inline_base_tags + inline_ig_tags + inline_yt_tags
    clean_tags = clean_hashtags(all_raw_tags, max_count=IG_MAX_HASHTAGS)
    plan["hashtags"] = clean_tags

    # 3. Instagram caption: Code builds platform_text.instagram.caption as caption + "\n\n" + hashtags
    # in EVERY mode, so the two can never differ!
    if clean_tags:
        ig_caption = f"{clean_base_caption}\n\n{' '.join(clean_tags)}".strip()
    else:
        ig_caption = clean_base_caption

    if len(ig_caption) > IG_CAPTION_MAX_CHARS:
        ig_caption = ig_caption[:IG_CAPTION_MAX_CHARS]
    plan.setdefault("platform_text", {}).setdefault("instagram", {})["caption"] = ig_caption

    # 4. YouTube text sanitization and brand name correction
    raw_yt_title = plan.get("platform_text", {}).get("youtube", {}).get("title", plan.get("title", ""))
    yt_title = sanitize_youtube_text(correct_brand_name(raw_yt_title))
    if len(yt_title) > YT_TITLE_MAX_CHARS:
        yt_title = yt_title[:YT_TITLE_MAX_CHARS]

    clean_yt_desc = sanitize_youtube_text(correct_brand_name(clean_yt_desc))
    if inline_yt_tags and clean_tags:
        clean_yt_desc = f"{clean_yt_desc}\n\n{' '.join(clean_tags)}".strip()

    if len(clean_yt_desc) > YT_DESCRIPTION_MAX_CHARS:
        clean_yt_desc = clean_yt_desc[:YT_DESCRIPTION_MAX_CHARS]

    plan["platform_text"].setdefault("youtube", {})["title"] = yt_title
    plan["platform_text"]["youtube"]["description"] = clean_yt_desc

    # 5. Scene processing for video creation modes
    if is_video_mode:
        plan["scenes"] = []
        plan["script"] = ""
    else:
        scenes = plan.get("scenes", [])

        # Guarantee character description in visual prompts
        if mode == "character" and character_desc:
            ensure_character_in_visual_prompts(scenes, character_desc)

        # Code sets the durations (speech fit) & visual prompt style suffix & brand casing
        for idx, scene in enumerate(scenes, start=1):
            scene["id"] = idx
            # Correct brand name in narration
            scene["narration"] = correct_brand_name(scene.get("narration", ""))
            # Code computes each scene's duration_hint_s from its narration: words / 2.5 + 0.5s clamped 3–8s
            scene["duration_hint_s"] = compute_scene_duration(scene["narration"])
            # Append visual prompt style suffix
            scene["visual_prompt"] = ensure_visual_prompt_style(scene.get("visual_prompt", ""))

        # Ensure hook equals scene 1 narration
        if scenes:
            plan["hook"] = scenes[0].get("narration", plan.get("hook", ""))

        # Assemble full script from scenes
        plan["script"] = " ".join(s.get("narration", "") for s in scenes)

    return plan
