"""Insight Agent recommendation and evaluation engine.

Pure selection, ranking, number-guard, and follow-through tracking logic.
Turns performance analysis and audience signals into actionable weekly decisions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import database as db
from .analysis import (
    HUMAN_LABELS,
    POSTING_BLOCKS,
    compute_baseline,
    get_confidence_tier,
    get_posting_hour_block,
    get_weekday_name,
    group_comparison,
    humanize,
    median,
)
from .audience import rank_content_ideas
from .db import make_engine, session_factory
from .models import (
    Account,
    MetricSnapshot,
    MetricValue,
    PostFeedback,
    PostTag,
    Publication,
    Recommendation,
    RecommendationSet,
)
from .timeutil import UTC

logger = logging.getLogger("insight.recommend")

DEFAULT_PROMPT_VERSION = "v1"
DEFAULT_MODEL_NAME = "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS = 30
IST = ZoneInfo("Asia/Kolkata")
TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds", "taxonomy.v1.json")

# Number guard mappings
WORD_TO_NUMBER = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "hundred": 100,
    "half": 0.5,
    "twice": 2.0,
    "double": 2.0,
    "triple": 3.0,
}

FORBIDDEN_CAUSAL_WORDS = {"cause", "caused", "causes", "causing"}

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def load_taxonomy(path: str = TAXONOMY_PATH) -> dict[str, list[str]]:
    """Load taxonomy for allowed topics, formats, and hooks."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tax = data.get("taxonomy", {})
    return {
        "topic": [t for t in tax.get("topic", []) if t not in ("other", "unknown")],
        "format": [f for f in tax.get("format", []) if f not in ("other", "unknown")],
        "hook": [h for h in tax.get("hook", []) if h not in ("none", "other", "unknown")],
    }


def get_ist_week_key(dt: datetime) -> str:
    """Return the IST ISO week key (e.g. '2026-W40') where weeks start Monday 00:00 IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist_dt = dt.astimezone(IST)
    year, week, _ = ist_dt.isocalendar()
    return f"{year}-W{week:02d}"


def get_ist_week_start_and_end(dt: datetime) -> tuple[datetime, datetime]:
    """Return the Monday 00:00 IST and Sunday 23:59:59 IST as UTC datetimes."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist_dt = dt.astimezone(IST)
    monday_ist = ist_dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=ist_dt.weekday())
    sunday_ist = monday_ist + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday_ist.astimezone(timezone.utc), sunday_ist.astimezone(timezone.utc)


def get_ist_monday(dt: datetime) -> datetime:
    """Return the Monday 00:00 IST datetime as UTC."""
    start, _ = get_ist_week_start_and_end(dt)
    return start


def count_usable_7d_posts(session: Session) -> int:
    """Count publications having a complete or delayed 7d snapshot with views."""
    subq = (
        select(MetricSnapshot.publication_id)
        .join(MetricValue, MetricValue.snapshot_id == MetricSnapshot.id)
        .filter(
            MetricSnapshot.subject_type == "publication",
            MetricSnapshot.checkpoint == "7d",
            MetricSnapshot.completeness.in_(("complete", "delayed")),
            MetricValue.canonical_metric == "views",
            MetricValue.value.isnot(None),
        )
        .distinct()
    )
    return len(session.scalars(subq).all())


def get_recommendation_mode(session: Session) -> str:
    """Exploration mode until 10 posts have a usable 7d value; then evidence mode."""
    cnt = count_usable_7d_posts(session)
    return "evidence" if cnt >= 10 else "exploration"


def _extract_numbers_from_facts(facts: Any) -> set[float]:
    """Recursively extract all numeric values from facts dict."""
    nums: set[float] = set()
    if isinstance(facts, dict):
        for k, v in facts.items():
            # If the key itself contains digits (e.g. '7d', '24h', '48h', '1h')
            for match in re.findall(r"\b\d+(?:\.\d+)?\b", str(k)):
                try:
                    nums.add(round(float(match), 2))
                except ValueError:
                    pass
            nums.update(_extract_numbers_from_facts(v))
    elif isinstance(facts, (list, tuple, set)):
        for item in facts:
            nums.update(_extract_numbers_from_facts(item))
    elif isinstance(facts, (int, float)) and not isinstance(facts, bool):
        nums.add(round(float(facts), 2))
    elif isinstance(facts, str):
        for match in re.findall(r"\b\d+(?:\.\d+)?\b", facts):
            try:
                nums.add(round(float(match), 2))
            except ValueError:
                pass
    return nums


def validate_ai_text(text: str, facts: dict[str, Any]) -> tuple[bool, str]:
    """Validate that every number and multiplier word in the text appears in facts."""
    if not text or not text.strip():
        return False, "Empty text"

    lower_text = text.lower()

    # Reject causal words
    tokens = re.findall(r"\b\w+\b", lower_text)
    for token in tokens:
        if token in FORBIDDEN_CAUSAL_WORDS:
            return False, f"Forbidden causal word '{token}' found"

    # Extract all numbers from facts
    allowed_numbers = _extract_numbers_from_facts(facts)

    # 1. Check numeric tokens (digits)
    text_num_matches = re.findall(r"\b\d+(?:\.\d+)?\b", text)
    for num_str in text_num_matches:
        try:
            val = round(float(num_str), 2)
            # Allow matching either exact float or near integer
            matched = any(abs(val - a) < 0.05 for a in allowed_numbers)
            if not matched:
                return False, f"Number {num_str} not in facts"
        except ValueError:
            return False, f"Invalid number {num_str}"

    # 2. Check word numbers and multipliers
    for token in tokens:
        if token in WORD_TO_NUMBER:
            val = WORD_TO_NUMBER[token]
            matched = any(abs(val - a) < 0.05 for a in allowed_numbers)
            if not matched:
                return False, f"Multiplier or number word '{token}' ({val}) not in facts"

    return True, ""


def validate_ai_quality(text: str) -> tuple[bool, str]:
    """Validate that AI text has no backticks, underscores, internal terms, or raw snake_case taxonomy keys."""
    if not text or not text.strip():
        return False, "Empty text"

    # 1. Reject backticks
    if "`" in text:
        return False, "Text contains backticks"

    # 2. Reject underscores
    if "_" in text:
        return False, "Text contains underscores"

    lower = text.lower()

    # 3. Reject internal terms: rank, mode, checkpoint
    for term in ("rank", "mode", "checkpoint"):
        if re.search(rf"\b{term}\b", lower):
            return False, f"Text contains forbidden internal term '{term}'"

    # 4. Reject any taxonomy key in raw snake_case
    for key in HUMAN_LABELS:
        if "_" in key and key in lower:
            return False, f"Text contains raw snake_case taxonomy key '{key}'"

    return True, ""


def call_gemini_wording(prompt: str, api_key: str | None = None, model_name: str | None = None) -> str | None:
    """Call Gemini for language generation with 30s timeout."""
    key = api_key or db.get_setting("GEMINI_API_KEY")
    if not key:
        return None

    model = model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME

    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        client = genai.GenerativeModel(model)
        resp = client.generate_content(prompt, request_options={"timeout": GEMINI_TIMEOUT_SECONDS})
        if resp and resp.text:
            return resp.text.strip().replace('"', '')
    except Exception as e:
        logger.warning(f"Gemini wording call failed: {e}")
        return None

    return None


def generate_exploration_recommendations(session: Session, week_key: str) -> list[dict[str, Any]]:
    """Generate 3–5 exploration recommendations with distinct topics, prioritizing untried dimensions."""
    tax = load_taxonomy()
    topics = tax["topic"]
    formats = tax["format"]
    hooks = tax["hook"]

    # Frequency analysis from existing post_tags
    topic_counts: dict[str, int] = {t: 0 for t in topics}
    format_counts: dict[str, int] = {f: 0 for f in formats}
    hook_counts: dict[str, int] = {h: 0 for h in hooks}

    tags = session.scalars(select(PostTag)).all()
    for t in tags:
        if t.dimension == "topic" and t.value in topic_counts:
            topic_counts[t.value] += 1
        elif t.dimension == "format" and t.value in format_counts:
            format_counts[t.value] += 1
        elif t.dimension == "hook" and t.value in hook_counts:
            hook_counts[t.value] += 1

    # Deterministic seed per week_key
    seed_val = int(hashlib.md5(week_key.encode("utf-8")).hexdigest(), 16) % (10**8)
    rng = random.Random(seed_val)

    # Sort topics by frequency asc, tie-break by rng/alphabetical
    sorted_topics = sorted(topics, key=lambda t: (topic_counts[t], rng.random()))
    # Select 4 distinct topics for the week
    selected_topics = sorted_topics[:4]

    sorted_formats = sorted(formats, key=lambda f: (format_counts[f], rng.random()))
    sorted_hooks = sorted(hooks, key=lambda h: (hook_counts[h], rng.random()))

    # Shuffle weekdays and time blocks deterministically
    shuffled_weekdays = list(WEEKDAY_NAMES)
    rng.shuffle(shuffled_weekdays)
    shuffled_blocks = list(POSTING_BLOCKS)
    rng.shuffle(shuffled_blocks)

    usable_7d = count_usable_7d_posts(session)
    recommendations: list[dict[str, Any]] = []

    for rank, topic in enumerate(selected_topics, start=1):
        fmt = sorted_formats[(rank - 1) % len(sorted_formats)]
        hook = sorted_hooks[(rank - 1) % len(sorted_hooks)]
        weekday = shuffled_weekdays[(rank - 1) % len(shuffled_weekdays)]
        block = shuffled_blocks[(rank - 1) % len(shuffled_blocks)]
        period = block.split()[0].lower()
        scheduled_str = f"{weekday} {period}"
        facts = {
            "topic": humanize(topic),
            "format": humanize(fmt),
            "hook": humanize(hook),
            "recommended_schedule": scheduled_str,
            "time_window": block,
        }

        template_text = (
            f"Try a {humanize(fmt).lower()} on {humanize(topic)}, opening with a {humanize(hook).lower()} hook, "
            f"posted {scheduled_str}."
        )

        recommendations.append({
            "rank": rank,
            "kind": "exploration",
            "topic": topic,
            "format": fmt,
            "hook": hook,
            "posting_block": block,
            "weekday": weekday,
            "facts_json": facts,
            "template_text": template_text,
            "confidence": "exploration, not evidence",
        })

    return recommendations


def _get_posts_for_analysis_session(session: Session) -> list[dict[str, Any]]:
    """Fetch posts with 7d views and tags for recommendation analysis within session."""
    pubs = session.scalars(select(Publication).order_by(Publication.published_at.desc())).all()
    posts = []
    for pub in pubs:
        snap_7d = session.scalar(
            select(MetricSnapshot).filter_by(
                subject_type="publication",
                subject_id=pub.id,
                checkpoint="7d",
            )
        )
        views_7d = None
        if snap_7d and snap_7d.completeness != "unavailable":
            mv = session.scalar(
                select(MetricValue).filter_by(
                    snapshot_id=snap_7d.id,
                    canonical_metric="views",
                )
            )
            if mv and mv.value is not None:
                views_7d = mv.value

        tags = session.scalars(select(PostTag).filter_by(publication_id=pub.id)).all()
        tag_dict = {t.dimension: t.value for t in tags}

        pub_dt = pub.published_at.replace(tzinfo=UTC) if pub.published_at.tzinfo is None else pub.published_at
        posts.append({
            "id": pub.id,
            "published_at": pub_dt,
            "caption": pub.caption,
            "views_7d": views_7d,
            "main_score": views_7d,
            "tags": tag_dict,
        })
    return posts


def generate_evidence_recommendations(session: Session, week_key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate evidence recommendations (>=1.2x baseline, >=3 posts) and avoid notes (<=0.8x baseline, >=3 posts)."""
    usable_7d = count_usable_7d_posts(session)
    posts = _get_posts_for_analysis_session(session)
    scores_7d = [p["views_7d"] for p in posts if p["views_7d"] is not None]
    baseline_stats = compute_baseline(scores_7d)
    baseline_views = baseline_stats.get("median") or 0.0

    # 1. Check Step 5 audience ideas (themes with 2+ distinct authors)
    qualified_ideas, _ = rank_content_ideas(session)

    # 2. Group comparisons across dimensions
    evidence_candidates: list[dict[str, Any]] = []
    avoid_notes: list[dict[str, Any]] = []

    if baseline_views > 0:
        comparisons = {
            "topic": group_comparison(posts, lambda p: p["tags"].get("topic")),
            "format": group_comparison(posts, lambda p: p["tags"].get("format")),
            "hook": group_comparison(posts, lambda p: p["tags"].get("hook")),
            "weekday": group_comparison(posts, lambda p: get_weekday_name(p["published_at"])),
            "posting_block": group_comparison(posts, lambda p: get_posting_hour_block(p["published_at"])),
        }

        for dim_key, groups in comparisons.items():
            for g in groups:
                n_posts = g.get("count", 0)
                med_v = g.get("median_views")
                grp_name = g.get("group")
                if n_posts >= 3 and med_v is not None and grp_name:
                    ratio = round(med_v / baseline_views, 2)
                    if ratio >= 1.2:
                        evidence_candidates.append({
                            "dimension": dim_key,
                            "value": grp_name,
                            "post_count": n_posts,
                            "median_views": med_v,
                            "ratio": ratio,
                        })
                    elif ratio <= 0.8:
                        avoid_notes.append({
                            "dimension": dim_key,
                            "value": grp_name,
                            "post_count": n_posts,
                            "median_views": med_v,
                            "ratio": ratio,
                            "note": f"{dim_key} '{grp_name}' is associated with {ratio:.1f}x baseline across {n_posts} posts.",
                        })

    # Sort evidence candidates by ratio descending
    evidence_candidates.sort(key=lambda c: c["ratio"], reverse=True)

    recommendations: list[dict[str, Any]] = []
    tax = load_taxonomy()
    used_topics: set[str] = set()

    # Step 5 audience ideas rank FIRST
    current_rank = 1
    for idea in qualified_ideas[:2]:  # at most 2 audience ideas
        theme = idea.get("theme", "Audience Theme")
        author_cnt = idea.get("author_count", 0)
        comment_cnt = idea.get("comment_count", 0)

        # Pick top format or default to explainer
        top_fmt = next((c["value"] for c in evidence_candidates if c["dimension"] == "format"), "explainer")
        top_hook = next((c["value"] for c in evidence_candidates if c["dimension"] == "hook"), "question")
        top_block = next((c["value"] for c in evidence_candidates if c["dimension"] == "posting_block"), POSTING_BLOCKS[2])
        top_weekday = next((c["value"] for c in evidence_candidates if c["dimension"] == "weekday"), "Wednesday")

        # Map theme to closest taxonomy topic or software_apps
        topic_val = "software_apps"
        for t in tax["topic"]:
            if t.lower() in theme.lower():
                topic_val = t
                break
        used_topics.add(topic_val)

        period = top_block.split()[0].lower()
        scheduled_str = f"{top_weekday} {period}"
        facts = {
            "topic": humanize(topic_val),
            "format": humanize(top_fmt),
            "hook": humanize(top_hook),
            "recommended_schedule": scheduled_str,
            "time_window": top_block,
            "theme": theme,
            "viewers_asking_count": author_cnt,
        }

        template_text = (
            f"Create a {humanize(top_fmt).lower()} on {humanize(topic_val)} answering questions on '{theme}', "
            f"posted {scheduled_str}. {author_cnt} viewers requested this."
        )

        recommendations.append({
            "rank": current_rank,
            "kind": "audience_idea",
            "topic": topic_val,
            "format": top_fmt,
            "hook": top_hook,
            "posting_block": top_block,
            "weekday": top_weekday,
            "facts_json": facts,
            "template_text": template_text,
            "confidence": "early signal, low confidence",
        })
        current_rank += 1

    # Remaining evidence slots (up to 4 total)
    for cand in evidence_candidates:
        if current_rank > 4:
            break
        dim = cand["dimension"]
        val = cand["value"]

        topic_val = val if dim == "topic" else next((t for t in tax["topic"] if t not in used_topics), "tech_news")
        if topic_val in used_topics and len(used_topics) < len(tax["topic"]):
            continue
        used_topics.add(topic_val)

        fmt_val = val if dim == "format" else "explainer"
        hook_val = val if dim == "hook" else "problem_solution"
        weekday_val = val if dim == "weekday" else "Thursday"
        block_val = val if dim == "posting_block" else POSTING_BLOCKS[2]

        period = block_val.split()[0].lower()
        scheduled_str = f"{weekday_val} {period}"
        facts = {
            "topic": humanize(topic_val),
            "format": humanize(fmt_val),
            "hook": humanize(hook_val),
            "recommended_schedule": scheduled_str,
            "time_window": block_val,
            "past_posts_count": cand["post_count"],
            "median_views": int(cand["median_views"]),
            "baseline_views": int(baseline_views),
        }

        template_text = (
            f"Post a {humanize(fmt_val).lower()} on {humanize(topic_val)} with a {humanize(hook_val).lower()} hook "
            f"on {scheduled_str}. Past posts achieved a median of {int(cand['median_views'])} views "
            f"across {cand['post_count']} posts."
        )

        recommendations.append({
            "rank": current_rank,
            "kind": "evidence",
            "topic": topic_val,
            "format": fmt_val,
            "hook": hook_val,
            "posting_block": block_val,
            "weekday": weekday_val,
            "facts_json": facts,
            "template_text": template_text,
            "confidence": "full",
        })
        current_rank += 1

    # If fewer than 3 evidence recommendations could be formed, fill with exploration items
    if len(recommendations) < 3:
        fillers = generate_exploration_recommendations(session, week_key)
        for filler in fillers:
            if len(recommendations) >= 3:
                break
            if filler["topic"] not in used_topics:
                used_topics.add(filler["topic"])
                filler["rank"] = len(recommendations) + 1
                filler["facts_json"]["rank"] = filler["rank"]
                recommendations.append(filler)

    return recommendations, avoid_notes


def generate_weekly_recommendation_set(
    session: Session,
    now: datetime | None = None,
    force: bool = False,
    api_key: str | None = None,
    model_name: str | None = None,
) -> RecommendationSet:
    """Generate or retrieve the recommendation set for the current IST week."""
    current_dt = now or datetime.now(UTC)
    week_key = get_ist_week_key(current_dt)

    existing = session.scalar(select(RecommendationSet).filter_by(week_key=week_key))
    if existing and not force:
        return existing

    if existing and force:
        session.delete(existing)
        session.flush()

    mode = get_recommendation_mode(session)
    rec_items: list[dict[str, Any]] = []

    if mode == "exploration":
        rec_items = generate_exploration_recommendations(session, week_key)
    else:
        rec_items, _ = generate_evidence_recommendations(session, week_key)

    rec_set = RecommendationSet(
        week_key=week_key,
        mode=mode,
        generated_at=current_dt,
        model=model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME,
        prompt_version=DEFAULT_PROMPT_VERSION,
    )
    session.add(rec_set)
    session.flush()

    for item in rec_items:
        facts = item["facts_json"]
        template_text = item["template_text"]
        ai_text = None
        is_ai = False

        # Attempt AI wording
        prompt = (
            f"You are a social media content strategist advising a video creator. "
            f"Write 1 to 2 concrete, plain English sentences recommending what video to create next based ONLY on these details: "
            f"{json.dumps(facts)}. "
            f"Style example: 'Try a how-to on software and apps, opening with a bold claim, posted Friday afternoon.' "
            f"RULES: "
            f"1. Be concrete, concise, and direct. "
            f"2. Do NOT use praise, cheerleading, congratulations, or flattery (e.g. no 'great job', 'momentum'). "
            f"3. Do NOT mention field names, keys, ranks, modes, or checkpoints. "
            f"4. Do NOT use backticks, quotes around categories, underscores, or raw taxonomy IDs. "
            f"5. Every number in your text MUST be present in the details. "
            f"6. Return ONLY the plain text sentences."
        )
        candidate = call_gemini_wording(prompt, api_key=api_key, model_name=model_name)
        if candidate:
            q_ok, q_reason = validate_ai_quality(candidate)
            n_ok, n_reason = validate_ai_text(candidate, facts)
            if q_ok and n_ok:
                ai_text = candidate
                is_ai = True
            else:
                reason = q_reason if not q_ok else n_reason
                logger.info(f"AI wording rejected by guards ({reason}): {candidate}")

        final_text = ai_text if is_ai else template_text

        rec = Recommendation(
            set_id=rec_set.id,
            rank=item["rank"],
            kind=item["kind"],
            topic=item["topic"],
            format=item["format"],
            hook=item["hook"],
            posting_block=item["posting_block"],
            weekday=item["weekday"],
            facts_json=json.dumps(facts),
            text=final_text,
            is_ai_text=is_ai,
            confidence=item["confidence"],
            status="open",
        )
        session.add(rec)

    session.flush()
    return rec_set


def generate_post_feedback(
    session: Session,
    publication: Publication,
    now: datetime | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> PostFeedback | None:
    """Generate per-post feedback. In <5 posts tier, strictly template facts only; no Gemini call."""
    current_dt = now or datetime.now(UTC)

    # Find the most mature snapshot available (prefer 7d > 48h > 24h > 1h > adhoc)
    snaps = session.scalars(
        select(MetricSnapshot)
        .filter_by(subject_type="publication", subject_id=publication.id)
        .order_by(MetricSnapshot.collected_at.desc())
    ).all()

    if not snaps:
        return None

    checkpoint_priority = {"7d": 5, "48h": 4, "24h": 3, "1h": 2, "adhoc": 1}
    sorted_snaps = sorted(snaps, key=lambda s: checkpoint_priority.get(s.checkpoint, 0), reverse=True)
    basis_snap = sorted_snaps[0]
    basis_cp = basis_snap.checkpoint

    # Check if feedback for this post + basis_checkpoint + prompt_version already exists
    existing = session.scalar(
        select(PostFeedback).filter_by(
            publication_id=publication.id,
            basis_checkpoint=basis_cp,
            prompt_version=DEFAULT_PROMPT_VERSION,
        )
    )
    if existing:
        return existing

    # Read metric values from snapshot
    vals = {
        mv.canonical_metric: mv.value
        for mv in session.scalars(select(MetricValue).filter_by(snapshot_id=basis_snap.id)).all()
    }
    views = vals.get("views") or 0.0
    reach = vals.get("reach") or 0.0
    interactions = vals.get("total_interactions") or 0.0
    avg_watch_time = vals.get("avg_watch_time_seconds") or 0.0

    # Usable 7d count and performance tier
    usable_7d = count_usable_7d_posts(session)
    tier = get_confidence_tier(usable_7d)
    posts = _get_posts_for_analysis_session(session)
    scores_7d = [p["views_7d"] for p in posts if p["views_7d"] is not None]
    baseline_stats = compute_baseline(scores_7d)
    baseline_views = baseline_stats.get("median") or 0.0
    ratio = round(views / baseline_views, 2) if baseline_views > 0 else 1.0

    classification = "at_normal"
    if baseline_views > 0:
        if views >= baseline_stats.get("p75", baseline_views * 1.5):
            classification = "above_normal"
        elif views <= baseline_stats.get("p25", baseline_views * 0.5):
            classification = "below_normal"

    # Post tags
    tags = session.scalars(select(PostTag).filter_by(publication_id=publication.id)).all()
    tag_map = {t.dimension: t.value for t in tags}

    age_hours = round((basis_snap.collected_at - publication.published_at).total_seconds() / 3600, 1)

    facts: dict[str, Any] = {
        "publication_id": publication.id,
        "platform_post_id": publication.platform_post_id,
        "basis_checkpoint": basis_cp,
        "checkpoint_days": 7 if basis_cp == "7d" else round(age_hours / 24, 1),
        "checkpoint_hours": age_hours,
        "views": int(views),
        "reach": int(reach),
        "total_interactions": int(interactions),
        "avg_watch_time_seconds": round(avg_watch_time, 1),
        "usable_7d_count": usable_7d,
        "tier": tier,
        "baseline_views": round(baseline_views, 1),
        "ratio_to_baseline": ratio,
        "classification": classification,
        "topic": tag_map.get("topic", "untagged"),
        "format": tag_map.get("format", "untagged"),
        "hook": tag_map.get("hook", "untagged"),
    }

    # Approved change 4: In "not enough data" tier (<5 usable 7d posts),
    # do NOT call Gemini at all; use code template only (facts, no judgements).
    if tier == "not enough data" or usable_7d < 5:
        template_text = (
            f"Recorded {int(views)} views, {int(reach)} reach, and {int(interactions)} interactions at the "
            f"{basis_cp} snapshot ({age_hours:.1f} hours after publish). "
            f"Account has {usable_7d} post(s) with 7-day data (baseline comparisons require 5 posts)."
        )
        fb = PostFeedback(
            publication_id=publication.id,
            basis_checkpoint=basis_cp,
            facts_json=json.dumps(facts),
            text=template_text,
            is_ai_text=False,
            model=None,
            prompt_version=DEFAULT_PROMPT_VERSION,
            generated_at=current_dt,
        )
        session.add(fb)
        session.flush()
        return fb

    # Evidence tiers (>=5 posts): call Gemini with number guard
    template_text = (
        f"At {basis_cp}, this Reel recorded {int(views)} views ({ratio:.1f}x baseline of {baseline_views:.0f} views, "
        f"{classification.replace('_', ' ')}). Average watch time was {avg_watch_time:.1f}s across {int(reach)} reach."
    )
    ai_text = None
    is_ai = False

    ai_facts = {
        "topic": humanize(tag_map.get("topic", "")),
        "format": humanize(tag_map.get("format", "")),
        "hook": humanize(tag_map.get("hook", "")),
        "views": int(views),
        "reach": int(reach),
        "total_interactions": int(interactions),
        "avg_watch_time_seconds": round(avg_watch_time, 1),
        "baseline_views": int(baseline_views),
        "checkpoint": basis_cp,
        "hours_since_published": age_hours,
    }

    prompt = (
        f"You are a video performance reviewer advising a creator. Write 1 to 2 concise sentences summarizing this video's results "
        f"based ONLY on these details: {json.dumps(ai_facts)}. "
        f"RULES: 1. Plain English for a creator, concrete and direct. "
        f"2. Do NOT use praise, cheerleading, or flattery. "
        f"3. Do NOT use backticks, underscores, or raw field names. "
        f"4. Every number in your text MUST be present in the details. "
        f"5. Use 'associated with', NEVER causal words like 'cause' or 'because of'. "
        f"6. Return only plain text."
    )
    candidate = call_gemini_wording(prompt, api_key=api_key, model_name=model_name)
    if candidate:
        q_ok, q_reason = validate_ai_quality(candidate)
        n_ok, n_reason = validate_ai_text(candidate, ai_facts)
        if q_ok and n_ok:
            ai_text = candidate
            is_ai = True
        else:
            reason = q_reason if not q_ok else n_reason
            logger.info(f"Post feedback AI wording rejected by guards ({reason}): {candidate}")

    final_text = ai_text if is_ai else template_text

    fb = PostFeedback(
        publication_id=publication.id,
        basis_checkpoint=basis_cp,
        facts_json=json.dumps(facts),
        text=final_text,
        is_ai_text=is_ai,
        model=model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME if is_ai else None,
        prompt_version=DEFAULT_PROMPT_VERSION,
        generated_at=current_dt,
    )
    session.add(fb)
    session.flush()
    return fb


def generate_all_post_feedback(session: Session, now: datetime | None = None) -> int:
    """Generate or update post feedback for all publications with snapshots."""
    pubs = session.scalars(select(Publication)).all()
    count = 0
    for pub in pubs:
        res = generate_post_feedback(session, pub, now=now)
        if res:
            count += 1
    session.flush()
    return count


def reconcile_follow_through(session: Session, now: datetime | None = None) -> int:
    """Reconcile open recommendations with publications; enforce one-to-one matching and expiration."""
    current_dt = now or datetime.now(UTC)
    expiry_cutoff = current_dt - timedelta(days=14)

    # 1. Expire open recommendations older than 14 days
    open_recs = session.scalars(
        select(Recommendation)
        .join(RecommendationSet, Recommendation.set_id == RecommendationSet.id)
        .filter(Recommendation.status == "open")
        .order_by(RecommendationSet.generated_at.asc(), Recommendation.rank.asc())
    ).all()

    for r in open_recs:
        if r.recommendation_set.generated_at < expiry_cutoff:
            r.status = "expired"

    session.flush()

    # 2. Match remaining open recommendations against published posts
    active_open_recs = [r for r in open_recs if r.status == "open"]
    if not active_open_recs:
        return 0

    # Get already matched publication IDs to guarantee one-to-one matching
    already_matched_pub_ids = set(
        session.scalars(
            select(Recommendation.matched_publication_id).filter(
                Recommendation.matched_publication_id.isnot(None)
            )
        ).all()
    )

    # Find candidate publications
    earliest_set_time = min(r.recommendation_set.generated_at for r in active_open_recs)
    candidate_pubs = session.scalars(
        select(Publication)
        .filter(
            Publication.published_at >= earliest_set_time,
            Publication.id.not_in(already_matched_pub_ids) if already_matched_pub_ids else True,
        )
        .order_by(Publication.published_at.asc())
    ).all()

    # Pre-fetch post tags for candidates
    pub_tags: dict[int, dict[str, str]] = {}
    for pub in candidate_pubs:
        tags = session.scalars(select(PostTag).filter_by(publication_id=pub.id)).all()
        pub_tags[pub.id] = {t.dimension: t.value for t in tags}

    posts = _get_posts_for_analysis_session(session)
    scores_7d = [p["views_7d"] for p in posts if p["views_7d"] is not None]
    baseline_stats = compute_baseline(scores_7d)
    baseline_views = baseline_stats.get("median") or 0.0

    matched_count = 0
    used_pubs_this_run: set[int] = set()

    # Approved change 5: One-to-one matching, highest rank wins
    for rec in sorted(active_open_recs, key=lambda r: (r.recommendation_set.generated_at, r.rank)):
        if rec.status != "open":
            continue

        target_topic = rec.topic
        target_format = rec.format
        set_time = rec.recommendation_set.generated_at

        for pub in candidate_pubs:
            if pub.id in used_pubs_this_run or pub.published_at < set_time:
                continue

            tags = pub_tags.get(pub.id, {})
            if tags.get("topic") == target_topic and tags.get("format") == target_format:
                # Match found!
                rec.status = "followed"
                rec.matched_publication_id = pub.id
                used_pubs_this_run.add(pub.id)
                matched_count += 1

                # Check if 7d snapshot exists to record outcome
                snap_7d = session.scalar(
                    select(MetricSnapshot).filter_by(
                        subject_type="publication",
                        subject_id=pub.id,
                        checkpoint="7d",
                    )
                )
                if snap_7d and baseline_views > 0:
                    val_7d = session.scalar(
                        select(MetricValue).filter_by(
                            snapshot_id=snap_7d.id,
                            canonical_metric="views",
                        )
                    )
                    if val_7d and val_7d.value is not None:
                        rec.outcome_ratio = round(val_7d.value / baseline_views, 2)

                break

    # Also update outcome_ratio on previously followed recommendations if 7d just arrived
    followed_recs = session.scalars(
        select(Recommendation).filter(
            Recommendation.status == "followed",
            Recommendation.outcome_ratio.is_(None),
            Recommendation.matched_publication_id.isnot(None),
        )
    ).all()

    for r in followed_recs:
        snap_7d = session.scalar(
            select(MetricSnapshot).filter_by(
                subject_type="publication",
                subject_id=r.matched_publication_id,
                checkpoint="7d",
            )
        )
        if snap_7d and baseline_views > 0:
            val_7d = session.scalar(
                select(MetricValue).filter_by(
                    snapshot_id=snap_7d.id,
                    canonical_metric="views",
                )
            )
            if val_7d and val_7d.value is not None:
                r.outcome_ratio = round(val_7d.value / baseline_views, 2)

    session.flush()
    return matched_count


def main():
    parser = argparse.ArgumentParser(description="Insight Agent Recommendations CLI")
    parser.add_argument("--regenerate-week", action="store_true", help="Force regenerate current week's recommendations")
    args = parser.parse_args()

    engine = make_engine()
    with session_factory(engine)() as session:
        rec_set = generate_weekly_recommendation_set(session, force=args.regenerate_week)
        fb_count = generate_all_post_feedback(session)
        matches = reconcile_follow_through(session)
        session.commit()

        print(f"=== Recommendations for {rec_set.week_key} (Mode: {rec_set.mode.upper()}) ===")
        print(f"Generated at: {rec_set.generated_at.isoformat()} | Post Feedback refreshed: {fb_count} | New matches: {matches}\n")

        for r in rec_set.recommendations:
            badge = "[AI-written]" if r.is_ai_text else "[Template]"
            print(f"#{r.rank} [{r.kind.upper()}] {r.topic} | {r.format} | {r.hook} | {r.weekday} ({r.posting_block})")
            print(f"   Status: {r.status} | Confidence: {r.confidence} | {badge}")
            print(f"   Action: {r.text}\n")


if __name__ == "__main__":
    main()
