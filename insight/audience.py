"""Audience intelligence and comment classification for the Insight Agent.

Categorizes comments into a fixed taxonomy, determines sentiment, flags
comments needing replies, extracts content ideas, and identifies spam/abuse.
All operations are ANALYSIS ONLY; comments are never posted, replied to, or deleted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import distinct, func, select

import database as db
from .models import Comment, CommentLabel, MetricSnapshot, MetricValue, Publication
from .timeutil import UTC

logger = logging.getLogger("insight.audience")

DEFAULT_PROMPT_VERSION = "v1"
DEFAULT_MODEL_NAME = "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS = 30
BATCH_SIZE = 20
MAX_COMMENTS_PER_RUN = 100

TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds", "comment_taxonomy.v1.json")


def load_comment_taxonomy(path: str = TAXONOMY_PATH) -> dict[str, list[str]]:
    """Load the versioned comment taxonomy seed."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("taxonomy", {})


def hash_comment_input(text: str | None) -> str:
    """SHA-256 hash of comment text."""
    normalized = (text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _build_batch_prompt(comments_batch: list[dict[str, Any]], taxonomy: dict[str, list[str]], existing_themes: list[str]) -> str:
    categories = json.dumps(taxonomy.get("category", []))
    sentiments = json.dumps(taxonomy.get("sentiment", []))
    themes_str = json.dumps(existing_themes[:50])

    items_repr = json.dumps([{"id": c["id"], "text": c["text"]} for c in comments_batch], indent=2)

    return f"""You are an expert community manager and audience intelligence analyst for tech Instagram Reels.
Classify each audience comment below.

Taxonomy:
- category: {categories}
- sentiment: {sentiments}
- known existing themes (reuse where appropriate to avoid duplicates): {themes_str}

Rules:
1. category:
   - "question": viewer asks a technical or informational question
   - "request": viewer asks for specific content, tutorials, code, or follow-ups
   - "praise": compliment, appreciation, or positive encouragement
   - "complaint": bug report, dissatisfaction, or criticism of content
   - "feedback": constructive suggestions or alternative solutions
   - "spam": unsolicited promotion, crypto/bot spam, generic follow-for-follow
   - "abuse": hate speech, harassment, insults, offensive language
   - "other": anything else not fitting above
2. sentiment: "positive", "neutral", "negative", or "unknown"
3. needs_reply: true if the creator should respond to this comment (e.g. genuine question, constructive feedback, complaint, or high-intent request). False for spam, abuse, simple praise ("nice!"), or rhetorical remarks.
4. needs_reply_reason: short 1-sentence reason why reply is needed (null if needs_reply is false).
5. theme: concise topic or question theme (2-4 words, title case, e.g. "Docker Deployment", "Model Pricing", "Error Handling"). If a comment matches an existing theme, use it.

Comments to classify:
{items_repr}

Output format: Return ONLY a valid JSON array of objects, one per comment:
[
  {{
    "id": <comment_id>,
    "category": "<category>",
    "sentiment": "<sentiment>",
    "confidence": <float 0.0 to 1.0>,
    "needs_reply": <true or false>,
    "needs_reply_reason": "<reason or null>",
    "theme": "<concise theme>"
  }}
]
"""


def classify_comments_batch(
    comments_batch: list[dict[str, Any]],
    existing_themes: list[str] | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> list[dict[str, Any]]:
    """Call Gemini to classify a batch of comments."""
    import google.generativeai as genai

    taxonomy = load_comment_taxonomy()
    allowed_categories = set(taxonomy.get("category", []))
    allowed_sentiments = set(taxonomy.get("sentiment", []))

    key = api_key or db.get_setting("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY for audience classification")

    model_to_use = model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME
    genai.configure(api_key=key)
    model = genai.GenerativeModel(model_to_use)

    prompt = _build_batch_prompt(comments_batch, taxonomy, existing_themes or [])
    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
        request_options={"timeout": GEMINI_TIMEOUT_SECONDS},
    )

    raw_text = response.text.strip()
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict) and "comments" in data:
            data = data["comments"]
        if not isinstance(data, list):
            data = []
    except Exception as exc:
        logger.warning(f"[audience] Failed to parse model JSON: {raw_text[:200]} ({exc})")
        data = []

    # Map responses by comment id
    parsed_by_id = {}
    for item in data:
        if isinstance(item, dict) and "id" in item:
            parsed_by_id[item["id"]] = item

    results = []
    for c in comments_batch:
        c_id = c["id"]
        entry = parsed_by_id.get(c_id, {})
        cat = entry.get("category")
        sent = entry.get("sentiment")
        conf = entry.get("confidence", 0.0)
        needs_rep = bool(entry.get("needs_reply", False))
        rep_reason = entry.get("needs_reply_reason")
        theme = (entry.get("theme") or "General").strip()

        # Strict validation against taxonomy:
        if cat not in allowed_categories:
            cat = "other"
            conf = 0.0
        if sent not in allowed_sentiments:
            sent = "unknown"

        # Never flag spam or abuse as needing reply
        if cat in ("spam", "abuse"):
            needs_rep = False
            rep_reason = None

        try:
            conf_float = max(0.0, min(1.0, float(conf)))
        except (ValueError, TypeError):
            conf_float = 0.0

        results.append({
            "id": c_id,
            "category": cat,
            "sentiment": sent,
            "confidence": round(conf_float, 2),
            "needs_reply": needs_rep,
            "needs_reply_reason": rep_reason,
            "theme": theme,
        })

    return results


def tag_unlabelled_comments(
    session,
    max_comments: int = MAX_COMMENTS_PER_RUN,
    model_name: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> int:
    """Find unlabelled audience comments (excluding own account) and classify them in batches."""
    # Find existing labelled comment IDs for this prompt version
    labelled_comment_ids = select(CommentLabel.comment_id).filter_by(prompt_version=prompt_version)

    # Comments needing labels: not own account, not already labelled
    unlabelled = session.scalars(
        select(Comment)
        .filter(
            Comment.is_own_account == False,
            ~Comment.id.in_(labelled_comment_ids),
        )
        .order_by(Comment.created_at.desc())
        .limit(max_comments)
    ).all()

    if not unlabelled:
        return 0

    # Retrieve existing themes from DB to pass to the model for clustering
    existing_themes = [
        t[0] for t in session.execute(
            select(distinct(CommentLabel.theme)).filter(CommentLabel.theme != "General")
        ).all()
        if t[0]
    ]

    model_used = model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME
    total_tagged = 0

    # Process in batches
    for i in range(0, len(unlabelled), BATCH_SIZE):
        batch = unlabelled[i:i + BATCH_SIZE]
        batch_dicts = [{"id": c.id, "text": c.text} for c in batch]

        try:
            classified = classify_comments_batch(
                batch_dicts,
                existing_themes=existing_themes,
                model_name=model_used,
                prompt_version=prompt_version,
            )
        except Exception as exc:
            logger.warning(f"[audience] Gemini classification batch error: {exc}")
            continue

        now = datetime.now(UTC)
        for item in classified:
            c_obj = next((c for c in batch if c.id == item["id"]), None)
            if not c_obj:
                continue

            label = CommentLabel(
                comment_id=c_obj.id,
                category=item["category"],
                sentiment=item["sentiment"],
                confidence=item["confidence"],
                needs_reply=item["needs_reply"],
                needs_reply_reason=item["needs_reply_reason"],
                theme=item["theme"],
                source="ai",
                model=model_used,
                prompt_version=prompt_version,
                input_hash=hash_comment_input(c_obj.text),
                labelled_at=now,
            )
            session.add(label)
            total_tagged += 1

        session.flush()

    return total_tagged


# =========================================================================
# Pure analysis / query helpers for audience intelligence
# =========================================================================

def get_needs_reply_comments(session) -> list[dict[str, Any]]:
    """Return audience comments needing a reply where no own-account reply exists yet."""
    # Find all comment IDs that have an own-account reply
    own_reply_parent_ids = select(Comment.parent_comment_id).filter(
        Comment.parent_comment_id.isnot(None),
        Comment.is_own_account == True,
    )

    rows = session.execute(
        select(Comment, CommentLabel, Publication)
        .join(CommentLabel, Comment.id == CommentLabel.comment_id)
        .outerjoin(Publication, Comment.publication_id == Publication.id)
        .filter(
            Comment.is_own_account == False,
            CommentLabel.needs_reply == True,
            ~CommentLabel.category.in_(["spam", "abuse"]),
            ~Comment.id.in_(own_reply_parent_ids),
        )
        .order_by(Comment.created_at.desc())
    ).all()

    results = []
    for c, lbl, pub in rows:
        results.append({
            "id": c.id,
            "text": c.text,
            "created_at": c.created_at,
            "like_count": c.like_count or 0,
            "category": lbl.category,
            "sentiment": lbl.sentiment,
            "confidence": lbl.confidence,
            "reason": lbl.needs_reply_reason or "Question or request from audience",
            "theme": lbl.theme,
            "publication_permalink": pub.permalink if pub else None,
            "publication_caption": pub.caption if pub else None,
        })
    return results


def rank_content_ideas(session) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group questions and requests by theme.

    Applies the 2-author rule:
    - Themes with >= 2 distinct authors qualify as Content Ideas.
    - Themes with only 1 author are placed in Single Mentions.
    """
    rows = session.execute(
        select(Comment, CommentLabel)
        .join(CommentLabel, Comment.id == CommentLabel.comment_id)
        .filter(
            Comment.is_own_account == False,
            CommentLabel.category.in_(["question", "request"]),
        )
    ).all()

    # Aggregate by theme
    themes: dict[str, dict[str, Any]] = {}
    for c, lbl in rows:
        theme = lbl.theme or "General"
        if theme not in themes:
            themes[theme] = {
                "theme": theme,
                "authors": set(),
                "comment_count": 0,
                "total_likes": 0,
                "latest_comment_at": c.created_at,
                "samples": [],
            }
        t = themes[theme]
        # Use author_hash if available, else comment id as fallback identifier
        author_key = c.author_hash or f"comment_{c.id}"
        t["authors"].add(author_key)
        t["comment_count"] += 1
        t["total_likes"] += (c.like_count or 0)
        if c.created_at > t["latest_comment_at"]:
            t["latest_comment_at"] = c.created_at
        if len(t["samples"]) < 3 and c.text not in t["samples"]:
            t["samples"].append(c.text)

    qualified = []
    single_mentions = []

    for theme_data in themes.values():
        distinct_authors_count = len(theme_data["authors"])
        item = {
            "theme": theme_data["theme"],
            "distinct_authors": distinct_authors_count,
            "comment_count": theme_data["comment_count"],
            "total_likes": theme_data["total_likes"],
            "latest_comment_at": theme_data["latest_comment_at"],
            "samples": theme_data["samples"],
        }
        if distinct_authors_count >= 2:
            qualified.append(item)
        else:
            single_mentions.append(item)

    # Sort qualified by distinct_authors desc, then total_likes desc, then latest_comment_at desc
    qualified.sort(
        key=lambda x: (x["distinct_authors"], x["total_likes"], x["latest_comment_at"]),
        reverse=True,
    )
    # Sort single mentions by latest_comment_at desc
    single_mentions.sort(key=lambda x: x["latest_comment_at"], reverse=True)

    return qualified, single_mentions


def compute_sentiment_breakdown(session) -> dict[str, Any]:
    """Compute sentiment distribution excluding 'unknown' and own account."""
    rows = session.execute(
        select(CommentLabel.sentiment, func.count(CommentLabel.id))
        .join(Comment, Comment.id == CommentLabel.comment_id)
        .filter(Comment.is_own_account == False)
        .group_by(CommentLabel.sentiment)
    ).all()

    counts = {"positive": 0, "neutral": 0, "negative": 0}
    unknown_count = 0
    for s_name, cnt in rows:
        if s_name in counts:
            counts[s_name] = cnt
        else:
            unknown_count += cnt

    total_valid = sum(counts.values())
    pcts = {
        s: round((cnt / total_valid * 100.0), 1) if total_valid > 0 else 0.0
        for s, cnt in counts.items()
    }

    return {
        "counts": counts,
        "percentages": pcts,
        "total_valid": total_valid,
        "unknown_count": unknown_count,
    }


def compute_category_breakdown(session) -> dict[str, Any]:
    """Compute comment category distribution for audience comments."""
    rows = session.execute(
        select(CommentLabel.category, func.count(CommentLabel.id))
        .join(Comment, Comment.id == CommentLabel.comment_id)
        .filter(Comment.is_own_account == False)
        .group_by(CommentLabel.category)
    ).all()

    counts = {r[0]: r[1] for r in rows}
    total = sum(counts.values())
    pcts = {
        cat: round((cnt / total * 100.0), 1) if total > 0 else 0.0
        for cat, cnt in counts.items()
    }

    return {
        "counts": counts,
        "percentages": pcts,
        "total": total,
    }


def get_spam_abuse_comments(session) -> list[dict[str, Any]]:
    """Return all comments flagged as spam or abuse."""
    rows = session.execute(
        select(Comment, CommentLabel)
        .join(CommentLabel, Comment.id == CommentLabel.comment_id)
        .filter(
            Comment.is_own_account == False,
            CommentLabel.category.in_(["spam", "abuse"]),
        )
        .order_by(Comment.created_at.desc())
    ).all()

    results = []
    for c, lbl in rows:
        results.append({
            "id": c.id,
            "text": c.text,
            "category": lbl.category,
            "confidence": lbl.confidence,
            "created_at": c.created_at,
        })
    return results


def get_audience_overview(session) -> dict[str, Any]:
    """Overview statistics and confidence level for the Audience tab."""
    total_comments = session.scalar(
        select(func.count(Comment.id)).filter(Comment.is_own_account == False)
    ) or 0

    total_labelled = session.scalar(
        select(func.count(CommentLabel.id))
        .join(Comment, Comment.id == CommentLabel.comment_id)
        .filter(Comment.is_own_account == False)
    ) or 0

    posts_comments_metric_sum = session.scalar(
        select(func.sum(MetricValue.value))
        .join(MetricSnapshot, MetricSnapshot.id == MetricValue.snapshot_id)
        .filter(
            MetricSnapshot.subject_type == "publication",
            MetricValue.canonical_metric == "comments",
            MetricValue.value > 0,
        )
    ) or 0

    unreadable_comments = bool(posts_comments_metric_sum > 0 and total_comments == 0)
    confidence_tier = "full" if total_comments >= 20 else "not enough data"

    return {
        "total_comments": total_comments,
        "total_labelled": total_labelled,
        "confidence_tier": confidence_tier,
        "unreadable_comments": unreadable_comments,
    }
