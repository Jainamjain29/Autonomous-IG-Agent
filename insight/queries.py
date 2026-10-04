"""Pure read functions for the Insight metrics view.

No Streamlit import here — only plain data structures and DataFrames.
All DB access uses short-lived read-only connections (open, query, close).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text

from .checkpoints import (
    ADHOC_CHECKPOINT,
    POST_CHECKPOINTS,
    evaluate_checkpoints,
)
from .db import REPO_ROOT, make_readonly_engine, session_factory
from .models import (
    Account,
    MetricSnapshot,
    MetricValue,
    Alert,
    WeeklyReport,
    PostFeedback,
    PostTag,
    Publication,
    RawResponse,
    Recommendation,
    RecommendationSet,
)
from .timeutil import UTC
from .analysis import (
    analyze_growth_association,
    classify_performance,
    compute_baseline,
    compute_early_pace_flags,
    get_caption_length_bucket,
    get_confidence_tier,
    get_hashtag_bucket,
    get_posting_hour_block,
    get_weekday_name,
    group_comparison,
)

# ── Display configuration ──────────────────────────────────────────────
DISPLAY_TIMEZONE = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata
DISPLAY_TZ_NAME = "IST"
ALEMBIC_HEAD = "0005_reports_and_alerts"
LOG_PATH = os.path.join(REPO_ROOT, "data", "logs", "collect.log")

# Post metrics in display order
POST_METRICS = [
    "views", "reach", "likes", "comments", "shares", "saves",
    "avg_watch_time_seconds", "total_watch_time_seconds",
]
ACCOUNT_DAILY_METRICS = ["reach", "views", "profile_visits"]
CHECKPOINT_ORDER = list(POST_CHECKPOINTS.keys())  # 1h, 24h, 48h, 7d, 28d

# Completeness markers
COMPLETENESS_MARKERS = {
    "complete": "✅",
    "delayed": "⏰",
    "unavailable": "❌",
    "partial": "⚠️",
}
NOT_DUE_MARKER = "⏳"


# ── Formatting helpers ─────────────────────────────────────────────────

def format_dt(dt_utc: datetime | None) -> str:
    """UTC datetime → IST 12-hour string like '3:05 PM IST'."""
    if dt_utc is None:
        return "—"
    local = dt_utc.astimezone(DISPLAY_TIMEZONE)
    return local.strftime("%-I:%M %p").replace("%-I", str(local.hour % 12 or 12)) if os.name == "nt" else local.strftime("%-I:%M %p")


def _format_time_12h(dt_utc: datetime | None) -> str:
    """UTC datetime → IST 12-hour string like 'Oct 03, 3:05 PM'."""
    if dt_utc is None:
        return "—"
    local = dt_utc.astimezone(DISPLAY_TIMEZONE)
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%b %d')}, {hour}:{local.strftime('%M')} {ampm}"


def format_age(seconds: int | float | None) -> str:
    """Seconds → human-readable duration like '2h 15m' or '3d 4h'."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, m = divmod(seconds, 3600)
        return f"{h}h {m // 60}m" if m // 60 else f"{h}h"
    d, remainder = divmod(seconds, 86400)
    h = remainder // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def format_metric_value(value: float | None, canonical_name: str, missing_reason: str | None = None) -> str:
    """Format a metric value for display. None → '—'. Watch times get 's' suffix."""
    if value is None:
        return "—"
    if "watch_time" in canonical_name:
        return f"{value:.1f}s"
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.1f}"


def _time_ago(dt_utc: datetime | None) -> str:
    """Human-readable 'time ago' string from UTC datetime."""
    if dt_utc is None:
        return "—"
    now = datetime.now(UTC)
    delta = now - dt_utc
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# ── DB health checks ──────────────────────────────────────────────────

def check_db_exists(db_path: str | None = None) -> bool:
    """Check if the real insight.db file exists."""
    from .db import REAL_DB_PATH
    return os.path.exists(db_path or REAL_DB_PATH)


def check_alembic_version(engine) -> tuple[bool, str | None]:
    """Check if alembic_version matches the expected head.

    Returns (is_at_head, current_version).
    """
    with engine.connect() as conn:
        # Check if alembic_version table exists
        result = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ))
        if result.fetchone() is None:
            return False, None
        result = conn.execute(text("SELECT version_num FROM alembic_version"))
        row = result.fetchone()
        if row is None:
            return False, None
        return row[0] == ALEMBIC_HEAD, row[0]


def check_has_publications(engine) -> bool:
    """Check if the DB has any publications."""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM publications"))
        return result.scalar() > 0


# ── Query functions ────────────────────────────────────────────────────

def get_posts_with_snapshots(engine) -> list[dict[str, Any]]:
    """Return all publications with their snapshots and metric values.

    Each dict has:
      - publication fields (id, published_at, media_type, etc.)
      - 'snapshots': dict keyed by checkpoint label, each with metrics dict and metadata
      - 'checkpoint_status': per-checkpoint completeness/state for the summary row
    """
    now = datetime.now(UTC)
    posts = []

    with engine.connect() as conn:
        pubs = conn.execute(
            select(Publication).order_by(Publication.published_at.desc())
        ).fetchall()

        for pub in pubs:
            age_seconds = int((now - pub.published_at.replace(tzinfo=UTC)).total_seconds())

            # Get all snapshots for this publication
            snaps = conn.execute(
                select(MetricSnapshot)
                .filter_by(subject_type="publication", subject_id=pub.id)
                .order_by(MetricSnapshot.checkpoint, MetricSnapshot.collected_at)
            ).fetchall()

            # Build per-checkpoint data
            collected_checkpoints = set()
            snapshots_by_cp = {}
            for snap in snaps:
                # Get metric values for this snapshot
                vals = conn.execute(
                    select(MetricValue).filter_by(snapshot_id=snap.id)
                ).fetchall()
                metrics = {}
                for v in vals:
                    metrics[v.canonical_metric] = {
                        "value": v.value,
                        "missing_reason": v.missing_reason,
                    }
                snap_data = {
                    "id": snap.id,
                    "checkpoint": snap.checkpoint,
                    "period_key": snap.period_key,
                    "collected_at": snap.collected_at.replace(tzinfo=UTC) if snap.collected_at.tzinfo is None else snap.collected_at,
                    "time_since_publish_seconds": snap.time_since_publish_seconds,
                    "completeness": snap.completeness,
                    "metrics": metrics,
                }
                if snap.checkpoint == ADHOC_CHECKPOINT:
                    # Keep only the latest adhoc
                    if "adhoc" not in snapshots_by_cp or snap.collected_at > snapshots_by_cp["adhoc"]["collected_at"].replace(tzinfo=None):
                        snapshots_by_cp["adhoc"] = snap_data
                else:
                    snapshots_by_cp[snap.checkpoint] = snap_data
                    collected_checkpoints.add(snap.checkpoint)

            # Determine checkpoint status for uncollected ones
            cp_statuses = evaluate_checkpoints(
                pub.published_at.replace(tzinfo=UTC),
                now,
                collected=collected_checkpoints,
            )
            checkpoint_status = {}
            for s in cp_statuses:
                if s.checkpoint in snapshots_by_cp:
                    checkpoint_status[s.checkpoint] = snapshots_by_cp[s.checkpoint]["completeness"]
                elif s.state == "pending":
                    checkpoint_status[s.checkpoint] = "not_due"
                else:
                    checkpoint_status[s.checkpoint] = "overdue"

            posts.append({
                "id": pub.id,
                "platform_post_id": pub.platform_post_id,
                "published_at": pub.published_at.replace(tzinfo=UTC) if pub.published_at.tzinfo is None else pub.published_at,
                "media_type": pub.media_type,
                "media_product_type": pub.media_product_type,
                "caption": pub.caption,
                "permalink": pub.permalink,
                "age_seconds": age_seconds,
                "snapshots": snapshots_by_cp,
                "checkpoint_status": checkpoint_status,
            })

    return posts


def get_daily_account_metrics(engine) -> list[dict[str, Any]]:
    """Return daily account snapshots with metric values, sorted by date."""
    rows = []
    with engine.connect() as conn:
        snaps = conn.execute(
            select(MetricSnapshot)
            .filter_by(subject_type="account", checkpoint="daily")
            .order_by(MetricSnapshot.period_key.asc())
        ).fetchall()

        for snap in snaps:
            vals = conn.execute(
                select(MetricValue).filter_by(snapshot_id=snap.id)
            ).fetchall()
            metrics = {}
            for v in vals:
                metrics[v.canonical_metric] = v.value
            rows.append({
                "date": snap.period_key,
                "collected_at": snap.collected_at.replace(tzinfo=UTC) if snap.collected_at.tzinfo is None else snap.collected_at,
                "completeness": snap.completeness,
                **{m: metrics.get(m) for m in ACCOUNT_DAILY_METRICS},
            })
    return rows


def get_followers_over_time(engine) -> list[dict[str, Any]]:
    """Return adhoc account snapshots that contain followers, sorted by collection time."""
    rows = []
    with engine.connect() as conn:
        snaps = conn.execute(
            select(MetricSnapshot)
            .filter_by(subject_type="account", checkpoint="adhoc")
            .order_by(MetricSnapshot.collected_at.asc())
        ).fetchall()

        for snap in snaps:
            vals = conn.execute(
                select(MetricValue)
                .filter_by(snapshot_id=snap.id, canonical_metric="followers")
            ).fetchall()
            if vals and vals[0].value is not None:
                rows.append({
                    "collected_at": snap.collected_at.replace(tzinfo=UTC) if snap.collected_at.tzinfo is None else snap.collected_at,
                    "followers": vals[0].value,
                })
    return rows


def get_snapshot_completeness_counts(engine) -> dict[str, int]:
    """Return snapshot counts grouped by completeness."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(MetricSnapshot.completeness, func.count())
            .group_by(MetricSnapshot.completeness)
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def get_overdue_checkpoints(engine) -> list[dict[str, Any]]:
    """Find publications with checkpoints that are due/missed but not yet collected."""
    now = datetime.now(UTC)
    overdue = []
    with engine.connect() as conn:
        pubs = conn.execute(select(Publication)).fetchall()
        for pub in pubs:
            age = (now - pub.published_at.replace(tzinfo=UTC)).total_seconds()
            if age > 28 * 86400:
                continue  # Past tracking window
            collected = set()
            snaps = conn.execute(
                select(MetricSnapshot.checkpoint)
                .filter_by(subject_type="publication", subject_id=pub.id)
            ).fetchall()
            for s in snaps:
                collected.add(s[0])
            statuses = evaluate_checkpoints(
                pub.published_at.replace(tzinfo=UTC), now, collected=collected
            )
            for s in statuses:
                if s.state in ("due", "missed", "unrecoverable") and s.checkpoint not in collected:
                    overdue.append({
                        "publication_id": pub.id,
                        "platform_post_id": pub.platform_post_id,
                        "caption": (pub.caption or "")[:60],
                        "checkpoint": s.checkpoint,
                        "state": s.state,
                        "due_at": s.due_at,
                        "lateness_seconds": s.lateness_seconds,
                    })
    return overdue


def get_raw_response_stats(engine) -> dict[str, Any]:
    """Return count and latest fetched_at for raw_responses."""
    with engine.connect() as conn:
        count = conn.execute(select(func.count(RawResponse.id))).scalar()
        latest = conn.execute(select(func.max(RawResponse.fetched_at))).scalar()
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return {"count": count or 0, "latest_fetched_at": latest}


def get_account_info(engine) -> dict[str, Any] | None:
    """Return basic account info, or None if no account exists."""
    with engine.connect() as conn:
        row = conn.execute(select(Account).limit(1)).fetchone()
    if row is None:
        return None
    return {
        "handle": row.handle,
        "platform": row.platform,
        "connected_at": row.connected_at.replace(tzinfo=UTC) if row.connected_at.tzinfo is None else row.connected_at,
    }


# ── Log parsing ────────────────────────────────────────────────────────

# Matches: [2026-10-02 23:44:29,490] INFO: [collect] OK: synced=2 snap_new=0 ...
_LOG_OK_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\] INFO: \[collect\] OK: "
    r"synced=(?P<synced>\d+) (?:tagged=(?P<tagged>\d+) )?"
    r"(?:comments=(?P<comments>\d+) )?(?:labelled=(?P<labelled>\d+) )?"
    r"snap_new=(?P<snap_new>\d+) "
    r"snap_unavail=(?P<snap_unavail>\d+) errors=(?P<errors>\d+) "
    r"calls=(?P<calls>\d+)/(?P<call_cap>\d+) elapsed=(?P<elapsed>[\d.]+)s"
)


# Matches error/fatal lines
_LOG_ERROR_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\] (?:ERROR|CRITICAL): \[collect\] (?P<msg>.+)"
)


def parse_collect_log(log_path: str | None = None) -> list[dict[str, Any]]:
    """Parse the collector log file and return the last 20 run entries."""
    log_path = log_path or LOG_PATH
    if not os.path.exists(log_path):
        return []

    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = _LOG_OK_RE.search(line)
            if m:
                ts_str = m.group("ts").replace(",", ".")
                # Parse as naive UTC (collector logs in UTC)
                ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                tagged_val = int(m.group("tagged")) if m.group("tagged") is not None else 0
                comments_val = int(m.group("comments")) if m.group("comments") is not None else 0
                labelled_val = int(m.group("labelled")) if m.group("labelled") is not None else 0
                entries.append({
                    "time": ts,
                    "status": "OK",
                    "synced": int(m.group("synced")),
                    "tagged": tagged_val,
                    "comments": comments_val,
                    "labelled": labelled_val,
                    "snap_new": int(m.group("snap_new")),
                    "snap_unavail": int(m.group("snap_unavail")),
                    "errors": int(m.group("errors")),
                    "calls": f"{m.group('calls')}/{m.group('call_cap')}",
                    "elapsed": m.group("elapsed") + "s",
                })
                continue
            m = _LOG_ERROR_RE.search(line)
            if m:
                ts_str = m.group("ts").replace(",", ".")
                ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                entries.append({
                    "time": ts,
                    "status": "FAILED",
                    "synced": 0,
                    "tagged": 0,
                    "comments": 0,
                    "labelled": 0,
                    "snap_new": 0,
                    "snap_unavail": 0,
                    "errors": 1,
                    "calls": "—",
                    "elapsed": "—",
                    "message": m.group("msg"),
                })

    return entries[-20:]  # last 20


def last_successful_run(log_entries: list[dict]) -> datetime | None:
    """Return the timestamp of the last OK run, or None."""
    for entry in reversed(log_entries):
        if entry["status"] == "OK":
            return entry["time"]
    return None


# ── Performance Analysis Queries ───────────────────────────────────────

def get_posts_for_analysis(engine) -> list[dict[str, Any]]:
    """Return publications enriched with 7d, 24h, 48h snapshot metrics and AI tags."""
    posts = []
    with engine.connect() as conn:
        pubs = conn.execute(
            select(Publication).order_by(Publication.published_at.desc())
        ).fetchall()

        for pub in pubs:
            snaps = conn.execute(
                select(MetricSnapshot).filter_by(
                    subject_type="publication",
                    subject_id=pub.id,
                )
            ).fetchall()

            metrics_by_cp: dict[str, dict[str, float]] = {}
            completeness_by_cp: dict[str, str] = {}
            for snap in snaps:
                completeness_by_cp[snap.checkpoint] = snap.completeness
                if snap.completeness != "unavailable":
                    vals = conn.execute(
                        select(MetricValue).filter_by(snapshot_id=snap.id)
                    ).fetchall()
                    metrics_by_cp[snap.checkpoint] = {
                        v.canonical_metric: v.value for v in vals if v.value is not None
                    }

            # AI Tags
            tags = conn.execute(
                select(PostTag).filter_by(publication_id=pub.id)
            ).fetchall()
            tag_dict = {
                t.dimension: {"value": t.value, "confidence": t.confidence}
                for t in tags
            }

            m7d = metrics_by_cp.get("7d", {})
            views_7d = m7d.get("views")
            reach_7d = m7d.get("reach")
            interactions_7d = m7d.get("total_interactions")
            erg_7d = (interactions_7d / reach_7d) if (reach_7d and reach_7d > 0 and interactions_7d is not None) else None

            pub_dt = pub.published_at.replace(tzinfo=UTC) if pub.published_at.tzinfo is None else pub.published_at

            posts.append({
                "id": pub.id,
                "platform_post_id": pub.platform_post_id,
                "caption": pub.caption,
                "permalink": pub.permalink,
                "published_at": pub_dt,
                "views_7d": views_7d,
                "reach_7d": reach_7d,
                "engagement_rate_7d": erg_7d,
                "avg_watch_time_7d": m7d.get("avg_watch_time_seconds"),
                "is_7d_delayed": (completeness_by_cp.get("7d") == "delayed"),
                "views_24h": metrics_by_cp.get("24h", {}).get("views"),
                "views_48h": metrics_by_cp.get("48h", {}).get("views"),
                "main_score": views_7d,
                "tags": tag_dict,
            })
    return posts


def get_performance_overview(engine) -> dict[str, Any]:
    """Compute overall performance baseline, post classifications, and pace flags."""
    posts = get_posts_for_analysis(engine)
    valid_7d_scores = [p["views_7d"] for p in posts if p["views_7d"] is not None]
    baseline_info = compute_baseline(valid_7d_scores)

    hist_24h = [p["views_24h"] for p in posts if p["views_24h"] is not None]
    hist_48h = [p["views_48h"] for p in posts if p["views_48h"] is not None]

    enriched_posts = []
    p25 = baseline_info.get("p25")
    p75 = baseline_info.get("p75")
    tier = baseline_info.get("tier")

    for p in posts:
        classification = None
        if tier != "not enough data" and p["views_7d"] is not None and p25 is not None and p75 is not None:
            classification = classify_performance(p["views_7d"], p25, p75)

        pace_24h = compute_early_pace_flags(p["views_24h"], hist_24h)
        pace_48h = compute_early_pace_flags(p["views_48h"], hist_48h)

        enriched_posts.append({
            **p,
            "classification": classification,
            "pace_24h": pace_24h,
            "pace_48h": pace_48h,
        })

    return {
        "baseline": baseline_info,
        "posts": enriched_posts,
    }


def get_comparison_breakdowns(engine) -> dict[str, list[dict[str, Any]]]:
    """Compute group comparisons across time blocks, weekdays, caption, hashtags, and AI tags."""
    posts = get_posts_for_analysis(engine)
    return {
        "posting_hour": group_comparison(posts, lambda p: get_posting_hour_block(p["published_at"])),
        "weekday": group_comparison(posts, lambda p: get_weekday_name(p["published_at"])),
        "caption_length": group_comparison(posts, lambda p: get_caption_length_bucket(p["caption"])),
        "hashtag_count": group_comparison(posts, lambda p: get_hashtag_bucket(p["caption"])),
        "topic": group_comparison(posts, lambda p: p["tags"].get("topic", {}).get("value")),
        "format": group_comparison(posts, lambda p: p["tags"].get("format", {}).get("value")),
        "hook": group_comparison(posts, lambda p: p["tags"].get("hook", {}).get("value")),
    }


# ── Audience Intelligence Queries ──────────────────────────────────────

def get_audience_overview_query(engine) -> dict[str, Any]:
    """Overview stats and confidence tier for audience comments."""
    from .audience import get_audience_overview
    with session_factory(engine)() as session:
        return get_audience_overview(session)


def get_needs_reply_queue(engine) -> list[dict[str, Any]]:
    """Comments requiring reply that haven't received an own-account response yet."""
    from .audience import get_needs_reply_comments
    with session_factory(engine)() as session:
        return get_needs_reply_comments(session)


def get_ranked_ideas(engine) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ranked content ideas (>=2 authors) and single mentions (<2 authors)."""
    from .audience import rank_content_ideas
    with session_factory(engine)() as session:
        return rank_content_ideas(session)


def get_sentiment_breakdown(engine) -> dict[str, Any]:
    """Sentiment breakdown excluding 'unknown' and own-account."""
    from .audience import compute_sentiment_breakdown
    with session_factory(engine)() as session:
        return compute_sentiment_breakdown(session)


def get_category_breakdown(engine) -> dict[str, Any]:
    """Audience comment category distribution."""
    from .audience import compute_category_breakdown
    with session_factory(engine)() as session:
        return compute_category_breakdown(session)


def get_spam_abuse_comments_query(engine) -> list[dict[str, Any]]:
    """Audience comments classified as spam or abuse."""
    from .audience import get_spam_abuse_comments
    with session_factory(engine)() as session:
        return get_spam_abuse_comments(session)


def get_growth_overview(engine) -> dict[str, Any]:
    """Compute growth association between adhoc followers and posting days."""
    followers = get_followers_over_time(engine)
    posts = get_posts_for_analysis(engine)
    pub_dates = [p["published_at"] for p in posts]
    return analyze_growth_association(followers, pub_dates)


def get_recommendation_overview(engine, week_key: str | None = None) -> dict[str, Any] | None:
    """Return the specified or latest recommendation set with items."""
    import json
    with session_factory(engine)() as session:
        if week_key:
            rec_set = session.scalar(select(RecommendationSet).filter_by(week_key=week_key))
        else:
            rec_set = session.scalar(
                select(RecommendationSet).order_by(RecommendationSet.generated_at.desc()).limit(1)
            )

        if not rec_set:
            return None

        recs = session.scalars(
            select(Recommendation).filter_by(set_id=rec_set.id).order_by(Recommendation.rank.asc())
        ).all()

        rec_items = []
        for r in recs:
            facts = {}
            if r.facts_json:
                try:
                    facts = json.loads(r.facts_json)
                except Exception:
                    pass

            rec_items.append({
                "id": r.id,
                "rank": r.rank,
                "kind": r.kind,
                "topic": r.topic,
                "format": r.format,
                "hook": r.hook,
                "posting_block": r.posting_block,
                "weekday": r.weekday,
                "text": r.text,
                "is_ai_text": r.is_ai_text,
                "confidence": r.confidence,
                "status": r.status,
                "facts": facts,
                "matched_publication_id": r.matched_publication_id,
                "outcome_ratio": r.outcome_ratio,
            })

        return {
            "id": rec_set.id,
            "week_key": rec_set.week_key,
            "mode": rec_set.mode,
            "generated_at": _format_time_12h(rec_set.generated_at),
            "model": rec_set.model,
            "prompt_version": rec_set.prompt_version,
            "recommendations": rec_items,
        }


def get_avoid_notes_query(engine) -> list[dict[str, Any]]:
    """Return groups with >=3 posts performing <= 0.8x baseline."""
    from .recommend import generate_evidence_recommendations
    with session_factory(engine)() as session:
        _, avoid = generate_evidence_recommendations(session, "current")
        return avoid


def get_follow_through_scoreboard(engine) -> dict[str, Any]:
    """Scoreboard metrics and history of recommendations across all weeks."""
    with session_factory(engine)() as session:
        recs = session.scalars(
            select(Recommendation)
            .join(RecommendationSet, Recommendation.set_id == RecommendationSet.id)
            .order_by(RecommendationSet.generated_at.desc(), Recommendation.rank.asc())
        ).all()

        total = len(recs)
        followed = [r for r in recs if r.status == "followed"]
        beat_normal = [r for r in followed if r.outcome_ratio is not None and r.outcome_ratio >= 1.0]

        records = []
        for r in recs:
            records.append({
                "week_key": r.recommendation_set.week_key,
                "rank": r.rank,
                "kind": r.kind,
                "topic": r.topic,
                "format": r.format,
                "weekday": r.weekday,
                "status": r.status,
                "matched_pub_id": r.matched_publication_id,
                "outcome_ratio": r.outcome_ratio,
                "is_ai_text": r.is_ai_text,
                "text": r.text,
            })

        return {
            "total_recommended": total,
            "followed_count": len(followed),
            "beat_normal_count": len(beat_normal),
            "beat_normal_rate": round((len(beat_normal) / len(followed) * 100), 1) if followed else 0.0,
            "records": records,
        }


def get_recommendation_history(engine) -> list[dict[str, Any]]:
    """List of all recommendation sets in reverse chronological order."""
    with session_factory(engine)() as session:
        sets = session.scalars(
            select(RecommendationSet).order_by(RecommendationSet.generated_at.desc())
        ).all()
        return [
            {
                "id": s.id,
                "week_key": s.week_key,
                "mode": s.mode,
                "generated_at": _format_time_12h(s.generated_at),
                "item_count": len(s.recommendations),
            }
            for s in sets
        ]


def get_post_feedback_map(engine) -> dict[int, list[dict[str, Any]]]:
    """Mapping from publication_id to feedback entries."""
    with session_factory(engine)() as session:
        feedbacks = session.scalars(
            select(PostFeedback).order_by(PostFeedback.generated_at.desc())
        ).all()

        fb_map: dict[int, list[dict[str, Any]]] = {}
        for fb in feedbacks:
            fb_map.setdefault(fb.publication_id, []).append({
                "basis_checkpoint": fb.basis_checkpoint,
                "text": fb.text,
                "is_ai_text": fb.is_ai_text,
                "generated_at": _format_time_12h(fb.generated_at),
            })
        return fb_map


def get_db_alerts(engine) -> list[dict[str, Any]]:
    """Return all alerts from DB ordered by created_at desc."""
    with session_factory(engine)() as session:
        alerts = session.scalars(select(Alert).order_by(Alert.created_at.desc())).all()
        return [
            {
                "id": a.id,
                "kind": a.kind,
                "severity": a.severity,
                "dedupe_key": a.dedupe_key,
                "title": a.title,
                "body": a.body,
                "facts": json.loads(a.facts_json) if a.facts_json else {},
                "created_at": a.created_at,
                "created_at_ist": _format_time_12h(a.created_at),
                "resolved_at": a.resolved_at,
                "is_resolved": (a.resolved_at is not None),
            }
            for a in alerts
        ]


def get_available_report_weeks_query(engine) -> list[str]:
    """Return available ISO weeks for reports."""
    from .report import get_available_weeks
    with session_factory(engine)() as session:
        return get_available_weeks(session)


def get_weekly_report_query(engine, week_key: str) -> dict[str, Any]:
    """Compute and format full weekly report for display and download."""
    from .report import (
        compute_weekly_report_data,
        generate_report_summary,
        generate_report_markdown,
        generate_report_html,
    )
    with session_factory(engine)() as session:
        data = compute_weekly_report_data(session, week_key)
        summary, is_ai = generate_report_summary(session, data, save_to_db=False)
        md = generate_report_markdown(data, summary)
        html = generate_report_html(data, summary)
        return {
            "data": data,
            "summary_text": summary,
            "is_ai_summary": is_ai,
            "markdown": md,
            "html": html,
        }



