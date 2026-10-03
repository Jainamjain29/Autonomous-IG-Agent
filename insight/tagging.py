"""AI Tagging for publications into a fixed taxonomy.

Uses google.generativeai directly (does NOT import agent_brain).
Validates model output strictly against taxonomy.v1.json.
Re-tagging occurs only if caption/input changed or prompt_version changes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select

import database as db
from .models import PostTag, Publication
from .timeutil import UTC

logger = logging.getLogger("insight.tagging")

DEFAULT_PROMPT_VERSION = "v1"
DEFAULT_MODEL_NAME = "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS = 30
TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds", "taxonomy.v1.json")


def load_taxonomy(path: str = TAXONOMY_PATH) -> dict[str, list[str]]:
    """Load the versioned taxonomy seed."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("taxonomy", {})


def hash_input(text: str | None) -> str:
    """SHA-256 hash of input text."""
    normalized = (text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _build_prompt(caption: str, taxonomy: dict[str, list[str]]) -> str:
    return f"""You are an expert social media analyst for tech Instagram Reels.
Classify the following video caption into exactly one value for each of the three dimensions below.

Allowed values per dimension:
- topic: {json.dumps(taxonomy.get("topic", []))}
- format: {json.dumps(taxonomy.get("format", []))}
- hook: {json.dumps(taxonomy.get("hook", []))}

Caption:
\"\"\"{caption}\"\"\"

Output format: Return ONLY valid JSON matching this exact schema:
{{
  "topic": {{"value": "<selected_topic>", "confidence": <float 0.0 to 1.0>}},
  "format": {{"value": "<selected_format>", "confidence": <float 0.0 to 1.0>}},
  "hook": {{"value": "<selected_hook>", "confidence": <float 0.0 to 1.0>}}
}}
"""


def classify_caption(
    caption: str,
    api_key: str | None = None,
    model_name: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> dict[str, dict[str, Any]]:
    """Call Gemini to classify a caption into topic, format, and hook.

    Values outside the taxonomy are rejected and replaced with 'unknown' (confidence 0.0).
    """
    import google.generativeai as genai

    taxonomy = load_taxonomy()
    key = api_key or db.get_setting("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY for AI tagging")

    model_to_use = model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME
    genai.configure(api_key=key)
    model = genai.GenerativeModel(model_to_use)

    prompt = _build_prompt(caption, taxonomy)
    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
        request_options={"timeout": GEMINI_TIMEOUT_SECONDS},
    )

    raw_text = response.text.strip()
    try:
        data = json.loads(raw_text)
    except Exception as exc:
        logger.warning(f"[tagging] Failed to parse model JSON: {raw_text[:200]} ({exc})")
        data = {}

    result = {}
    for dim in ("topic", "format", "hook"):
        allowed = set(taxonomy.get(dim, []))
        entry = data.get(dim, {})
        val = entry.get("value")
        conf = entry.get("confidence", 0.0)

        if val not in allowed or val is None:
            result[dim] = {"value": "unknown", "confidence": 0.0}
        else:
            try:
                conf_float = max(0.0, min(1.0, float(conf)))
            except (ValueError, TypeError):
                conf_float = 0.0
            result[dim] = {"value": val, "confidence": round(conf_float, 2)}

    return result


def tag_publication(
    session,
    publication: Publication,
    model_name: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> bool:
    """Classify and save tags for one publication. Returns True if tagged, False if skipped/failed."""
    caption = publication.caption or ""
    curr_hash = hash_input(caption)
    model_used = model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME

    # Check if already tagged with same input_hash and prompt_version
    existing_tags = session.scalars(
        select(PostTag).filter_by(
            publication_id=publication.id,
            prompt_version=prompt_version,
        )
    ).all()

    if existing_tags:
        # If input hash matches, skip
        if all(t.input_hash == curr_hash for t in existing_tags):
            return False
        # If input changed, delete existing tags for this version and re-tag
        for t in existing_tags:
            session.delete(t)
        session.flush()

    now = datetime.now(UTC)
    classified = classify_caption(
        caption,
        model_name=model_used,
        prompt_version=prompt_version,
    )

    for dim, info in classified.items():
        tag = PostTag(
            publication_id=publication.id,
            dimension=dim,
            value=info["value"],
            confidence=info["confidence"],
            source="ai",
            model=model_used,
            prompt_version=prompt_version,
            input_hash=curr_hash,
            tagged_at=now,
        )
        session.add(tag)

    session.flush()
    return True


def tag_untagged_publications(
    session,
    publications: list[Publication],
    max_posts: int = 10,
    model_name: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> int:
    """Tag untagged or updated publications up to max_posts. Never raises."""
    tagged_count = 0
    for pub in publications:
        if tagged_count >= max_posts:
            break
        try:
            if tag_publication(session, pub, model_name=model_name, prompt_version=prompt_version):
                tagged_count += 1
                logger.info(f"[tagging] Successfully tagged publication {pub.platform_post_id}")
        except Exception as e:
            logger.warning(f"[tagging] Error tagging publication {pub.platform_post_id}: {e}")
    return tagged_count
