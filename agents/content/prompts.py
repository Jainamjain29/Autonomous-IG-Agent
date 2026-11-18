"""Prompt templates for the Content Agent."""
from __future__ import annotations

from typing import List, Optional
from agents.content.settings import (
    DEFAULT_BRAND_VOICE,
    DEFAULT_LANGUAGE,
    IG_MAX_HASHTAGS,
    SCENE_COUNT_MAX,
    SCENE_COUNT_MIN,
    SCENE_DURATION_MAX_S,
    SCENE_DURATION_MIN_S,
    TARGET_LENGTH_MAX_S,
    TARGET_LENGTH_MIN_S,
)

PROMPT_VERSION: str = "1.0.0"


def build_system_prompt(brand_voice: str = DEFAULT_BRAND_VOICE, language: str = DEFAULT_LANGUAGE) -> str:
    """Build the core system prompt establishing identity, brand voice, and output constraints."""
    return (
        f"You are the Content Agent for @streamovate. Version: {PROMPT_VERSION}.\n"
        f"Language: {language}.\n"
        f"Brand Voice: {brand_voice}\n\n"
        f"Your task is to produce structured content plans for short-form vertical video.\n"
        f"Requirements:\n"
        f"- Target video length: between {int(TARGET_LENGTH_MIN_S)}s and {int(TARGET_LENGTH_MAX_S)}s.\n"
        f"- Scene count: {SCENE_COUNT_MIN} to {SCENE_COUNT_MAX} scenes.\n"
        f"- Scene duration_hint_s: {int(SCENE_DURATION_MIN_S)}s to {int(SCENE_DURATION_MAX_S)}s per scene.\n"
        f"- Scene IDs must start at 1 and increment by 1 (1, 2, 3...).\n"
        f"- Hook must match the first spoken line of narration (scene 1 narration).\n"
        f"- Output up to {IG_MAX_HASHTAGS} relevant hashtags.\n"
        f"- Do NOT include hashtags directly inside the caption text (code will place them).\n"
        f"- Do NOT use '<' or '>' characters in YouTube title or description.\n"
        f"- Respond strictly in valid JSON adhering to the provided JSON schema."
    )


def build_generate_prompt(topic: str) -> str:
    """Prompt for generating a complete concept from a topic."""
    return (
        f"Create a short-form vertical video plan for the topic: '{topic}'.\n\n"
        f"1. Hook: Write a compelling first spoken sentence that grabs attention immediately.\n"
        f"2. Script: Full narration text consisting of all scenes joined together.\n"
        f"3. Scenes: 4 to 8 sequential scenes. Each scene has id (integer starting at 1), "
        f"narration (spoken line), visual_prompt (descriptive video prompt for video generator), "
        f"and duration_hint_s (3.0 to 8.0, summing to 30-60s).\n"
        f"4. Caption: Engaging description for social media without hashtags.\n"
        f"5. Hashtags: Up to 5 relevant hashtags.\n"
        f"6. Platform Text: Instagram caption, YouTube title (<= 100 chars, no < or >) and description (<= 5000 chars, no < or >)."
    )


def build_script_prompt(script: str, topic: Optional[str] = None) -> str:
    """Prompt for splitting a user-provided script into scenes without changing words."""
    topic_str = f"Topic: {topic}\n" if topic else ""
    return (
        f"{topic_str}"
        f"The user has provided an exact voiceover script below.\n"
        f"CRITICAL REQUIREMENT: The narration across all scenes must match this script WORD FOR WORD. "
        f"Do NOT add, remove, rewrite, paraphrase, or change ANY word or punctuation. "
        f"Only divide the exact script text across 4 to 8 sequential scenes.\n\n"
        f"User Script:\n\"\"\"{script}\"\"\"\n\n"
        f"Tasks:\n"
        f"1. Divide the script across 4 to 8 scenes (duration_hint_s 3.0 to 8.0 each, total sum 30-60s).\n"
        f"2. Hook must be the first spoken line (scene 1 narration).\n"
        f"3. Write rich, descriptive visual_prompt for each scene.\n"
        f"4. Write title, caption, hashtags (up to 5), and platform_text (Instagram and YouTube)."
    )


def build_character_prompt(topic: str, character_desc: str) -> str:
    """Prompt for concept generation with a consistent character."""
    return (
        f"Topic: '{topic}'\n"
        f"Character Specification: '{character_desc}'\n\n"
        f"CRITICAL REQUIREMENT: The character description ('{character_desc}') MUST appear in the "
        f"visual_prompt of EVERY single scene so that the character remains visually consistent throughout the video.\n\n"
        f"Generate the full video plan with 4 to 8 scenes, matching hook, durations 3.0 to 8.0s each (summing 30-60s), "
        f"caption, hashtags, and platform text."
    )


def build_video_mode_prompt(topic: Optional[str] = None, description: Optional[str] = None) -> str:
    """Prompt for existing video mode (metadata/caption only)."""
    context = topic or description or "Short-form video"
    return (
        f"An existing video file has already been produced for: '{context}'.\n"
        f"Do NOT generate new scenes or script narration.\n"
        f"Requirements:\n"
        f"- scenes MUST be an empty array []\n"
        f"- script MUST be an empty string \"\"\n"
        f"- Provide a catchy title, hook, engaging caption, up to 5 hashtags, and platform_text for Instagram and YouTube.\n"
        f"- YouTube title <= 100 chars, description <= 5000 chars, no '<' or '>' characters."
    )


def build_repair_prompt(errors: List[str], raw_text: str) -> str:
    """Prompt for repairing validation errors."""
    error_list = "\n".join(f"- {err}" for err in errors)
    return (
        f"Your previous output failed validation with the following errors:\n"
        f"{error_list}\n\n"
        f"Please fix all the errors listed above and return a completely valid JSON plan.\n"
        f"Previous output was:\n{raw_text}"
    )


def build_script_retry_prompt(script: str, mismatch_detail: str) -> str:
    """Prompt for retrying script splitting when joined narration was not verbatim."""
    return (
        f"CRITICAL ERROR: Your previous response changed or dropped words from the original script.\n"
        f"Mismatch detail: {mismatch_detail}\n\n"
        f"The joined narration across all scenes must equal the user's script WORD FOR WORD when normalized.\n"
        f"Original Script:\n\"\"\"{script}\"\"\"\n\n"
        f"Please fix this immediately. Keep every single word identical to the original script."
    )
