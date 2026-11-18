"""Validation and sanitization rules for Content Agent outputs."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from agents.content.settings import (
    IG_CAPTION_MAX_CHARS,
    IG_MAX_HASHTAGS,
    SCENE_COUNT_MAX,
    SCENE_COUNT_MIN,
    SCENE_DURATION_MAX_S,
    SCENE_DURATION_MIN_S,
    TARGET_LENGTH_MAX_S,
    TARGET_LENGTH_MIN_S,
    YT_DESCRIPTION_MAX_CHARS,
    YT_TITLE_MAX_CHARS,
)


def clean_hashtags(raw_tags: List[str], max_count: int = IG_MAX_HASHTAGS) -> List[str]:
    """Clean, deduplicate case-insensitively, cap, and format hashtags with '#'.
    
    1. Strip leading '#'
    2. Remove spaces and symbols
    3. Deduplicate preserving order
    4. Cap at max_count
    5. Format each hashtag as '#tag'
    """
    cleaned: List[str] = []
    seen: set = set()

    for item in raw_tags:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        # Strip leading #
        tag = tag.lstrip("#")
        # Remove whitespace and symbols
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


def check_character_in_visual_prompts(scenes: List[Dict[str, Any]], character_desc: str) -> Tuple[bool, str]:
    """Ensure character description is present in every scene's visual_prompt."""
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
    """Validate a plan dictionary against contract, schema, and platform limits."""
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

    # Check types
    if not isinstance(plan["title"], str):
        errors.append("Field 'title' must be a string")
    if not isinstance(plan["topic"], str):
        errors.append("Field 'topic' must be a string")
    if not isinstance(plan["hook"], str):
        errors.append("Field 'hook' must be a string")
    if not isinstance(plan["script"], str):
        errors.append("Field 'script' must be a string")
    if not isinstance(plan["scenes"], list):
        errors.append("Field 'scenes' must be a list")
    if not isinstance(plan["caption"], str):
        errors.append("Field 'caption' must be a string")
    if not isinstance(plan["hashtags"], list):
        errors.append("Field 'hashtags' must be a list")
    if not isinstance(plan["platform_text"], dict):
        errors.append("Field 'platform_text' must be an object")

    # Platform text validation
    pt = plan.get("platform_text", {})
    if isinstance(pt, dict):
        if "instagram" not in pt or not isinstance(pt["instagram"], dict):
            errors.append("Field 'platform_text.instagram' must be an object")
        elif "caption" not in pt["instagram"] or not isinstance(pt["instagram"]["caption"], str):
            errors.append("Field 'platform_text.instagram.caption' must be a string")

        if "youtube" not in pt or not isinstance(pt["youtube"], dict):
            errors.append("Field 'platform_text.youtube' must be an object")
        else:
            if "title" not in pt["youtube"] or not isinstance(pt["youtube"]["title"], str):
                errors.append("Field 'platform_text.youtube.title' must be a string")
            if "description" not in pt["youtube"] or not isinstance(pt["youtube"]["description"], str):
                errors.append("Field 'platform_text.youtube.description' must be a string")

    # Mode-specific validation
    is_video_mode = mode in ("video", "existing video", "existing_video")
    scenes = plan.get("scenes", [])

    if is_video_mode:
        if len(scenes) != 0:
            errors.append(f"In existing video mode, 'scenes' must be empty [], got {len(scenes)} scenes")
        if plan.get("script") != "":
            errors.append("In existing video mode, 'script' must be an empty string \"\"")
    else:
        # Standard video creation modes: generate, script, character
        scene_count = len(scenes)
        if not (SCENE_COUNT_MIN <= scene_count <= SCENE_COUNT_MAX):
            errors.append(
                f"Scene count must be between {SCENE_COUNT_MIN} and {SCENE_COUNT_MAX}, got {scene_count}"
            )

        total_duration = 0.0
        expected_id = 1

        for i, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                errors.append(f"Scene at index {i} must be an object")
                continue

            scene_id = scene.get("id")
            if scene_id != expected_id:
                errors.append(f"Scene at index {i} has id {scene_id}, expected sequential id {expected_id}")
            expected_id += 1

            if not isinstance(scene.get("narration"), str) or not scene.get("narration", "").strip():
                errors.append(f"Scene {scene_id} missing or empty 'narration'")

            if not isinstance(scene.get("visual_prompt"), str) or not scene.get("visual_prompt", "").strip():
                errors.append(f"Scene {scene_id} missing or empty 'visual_prompt'")

            duration = scene.get("duration_hint_s")
            if not isinstance(duration, (int, float)):
                errors.append(f"Scene {scene_id} 'duration_hint_s' must be a number")
            else:
                if not (SCENE_DURATION_MIN_S <= duration <= SCENE_DURATION_MAX_S):
                    errors.append(
                        f"Scene {scene_id} 'duration_hint_s' ({duration}) outside allowed bounds "
                        f"[{SCENE_DURATION_MIN_S}, {SCENE_DURATION_MAX_S}]"
                    )
                total_duration += float(duration)

        if SCENE_COUNT_MIN <= scene_count <= SCENE_COUNT_MAX:
            if not (TARGET_LENGTH_MIN_S <= total_duration <= TARGET_LENGTH_MAX_S):
                errors.append(
                    f"Total duration sum ({total_duration:.1f}s) outside allowed target bounds "
                    f"[{TARGET_LENGTH_MIN_S}s, {TARGET_LENGTH_MAX_S}s]"
                )

        # Hook validation: first scene's spoken line
        if scenes and isinstance(scenes[0], dict):
            first_narration = scenes[0].get("narration", "").strip()
            hook = plan.get("hook", "").strip()
            if hook != first_narration:
                errors.append(f"Hook must equal first spoken line (Scene 1 narration). Got hook='{hook}' vs scene 1='{first_narration}'")

        # Verbatim script check
        if mode in ("script", "user script", "user_script") and expected_script:
            matches, mismatch_err = check_verbatim_script(scenes, expected_script)
            if not matches:
                errors.append(mismatch_err)

        # Character presence check
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
    """Sanitize fields, enforce limits, and assemble final platform_text."""
    # 1. Clean hashtags
    raw_hashtags = plan.get("hashtags", [])
    clean_tags = clean_hashtags(raw_hashtags, max_count=IG_MAX_HASHTAGS)
    plan["hashtags"] = clean_tags

    # 2. Instagram caption: code appends hashtags if not already present
    ig_caption = plan.get("platform_text", {}).get("instagram", {}).get("caption", plan.get("caption", ""))
    if clean_tags:
        tags_str = " ".join(clean_tags)
        if tags_str not in ig_caption:
            ig_caption = f"{ig_caption}\n\n{tags_str}".strip()
    if len(ig_caption) > IG_CAPTION_MAX_CHARS:
        ig_caption = ig_caption[:IG_CAPTION_MAX_CHARS]
    plan.setdefault("platform_text", {}).setdefault("instagram", {})["caption"] = ig_caption

    # 3. YouTube text sanitization
    yt_title = sanitize_youtube_text(plan.get("platform_text", {}).get("youtube", {}).get("title", plan.get("title", "")))
    if len(yt_title) > YT_TITLE_MAX_CHARS:
        yt_title = yt_title[:YT_TITLE_MAX_CHARS]

    yt_desc = sanitize_youtube_text(
        plan.get("platform_text", {}).get("youtube", {}).get("description", plan.get("caption", ""))
    )
    if len(yt_desc) > YT_DESCRIPTION_MAX_CHARS:
        yt_desc = yt_desc[:YT_DESCRIPTION_MAX_CHARS]

    plan["platform_text"].setdefault("youtube", {})["title"] = yt_title
    plan["platform_text"]["youtube"]["description"] = yt_desc

    # 4. Mode-specific adjustments
    is_video_mode = mode in ("video", "existing video", "existing_video")
    if is_video_mode:
        plan["scenes"] = []
        plan["script"] = ""
    else:
        # Ensure scene IDs are 1..N
        for idx, scene in enumerate(plan.get("scenes", []), start=1):
            scene["id"] = idx
            if mode == "character" and character_desc:
                # Ensure character description is in visual prompt
                vp = scene.get("visual_prompt", "")
                if character_desc.lower() not in vp.lower():
                    scene["visual_prompt"] = f"{vp.rstrip('.')}. Character: {character_desc}."

        # Ensure hook equals scene 1 narration
        if plan.get("scenes"):
            plan["hook"] = plan["scenes"][0].get("narration", plan.get("hook", ""))

        # Assemble full script from scenes if not present or inconsistent
        if not plan.get("script"):
            plan["script"] = " ".join(s.get("narration", "") for s in plan.get("scenes", []))

    return plan
