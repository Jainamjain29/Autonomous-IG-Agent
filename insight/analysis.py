"""Performance analysis module (growth-focused).

Pure functions, no Streamlit imports, no DB writes.
Computes baselines, percentiles, classifications, early pace flags,
comparison breakdowns, and growth associations entirely in code.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .timeutil import UTC

# Constants
MIN_POSTS_EARLY_SIGNAL = 5
MIN_POSTS_FULL_CONFIDENCE = 10
MIN_POSTS_PACE_FLAG = 5
MIN_GROUP_POSTS_FOR_CONCLUSION = 3
MIN_DAYS_GROWTH_CONFIDENCE = 14
BASELINE_WINDOW_SIZE = 20

# IST timezone for posting hour analysis
IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

# ── Shared time-block definition (single source of truth) ──────────────────────
# IST blocks: Morning 6 AM – 11:59 AM, Afternoon 12 PM – 4:59 PM,
# Evening 5 PM – 9:59 PM, Night 10 PM – 5:59 AM.
POSTING_BLOCKS = [
    "Morning (6:00 AM–11:59 AM)",
    "Afternoon (12:00 PM–4:59 PM)",
    "Evening (5:00 PM–9:59 PM)",
    "Night (10:00 PM–5:59 AM)",
]

# ── Human-readable label map for every taxonomy value ──────────────────────────
# One shared place; used by recommend.py for facts/prompts, view.py for display.
HUMAN_LABELS: dict[str, str] = {
    # Topics
    "ai_tools": "AI tools",
    "smartphones_gadgets": "Smartphones & gadgets",
    "software_apps": "Software & apps",
    "internet_cloud_basics": "Internet & cloud basics",
    "cybersecurity": "Cybersecurity",
    "programming": "Programming",
    "tech_news": "Tech news",
    "other": "Other",
    "unknown": "Unknown",
    # Formats
    "explainer": "Explainer",
    "how_to": "How-to",
    "comparison": "Comparison",
    "news_update": "News update",
    "top_list": "Top list",
    "myth_busting": "Myth-busting",
    # Hooks
    "question": "Question",
    "bold_claim": "Bold claim",
    "surprising_stat": "Surprising stat",
    "problem_solution": "Problem & solution",
    "story": "Story",
    "demo_visual": "Demo / visual",
    "none": "None",
    # Time blocks (identity map for display)
    "Morning (6:00 AM – 11:59 AM)": "Morning (6:00 AM – 11:59 AM)",
    "Afternoon (12:00 PM – 4:59 PM)": "Afternoon (12:00 PM – 4:59 PM)",
    "Evening (5:00 PM – 9:59 PM)": "Evening (5:00 PM – 9:59 PM)",
    "Night (10:00 PM – 5:59 AM)": "Night (10:00 PM – 5:59 AM)",
}


def humanize(key: str) -> str:
    """Return the human label for a taxonomy key, or title-case the key itself."""
    return HUMAN_LABELS.get(key, key.replace("_", " ").title())


def percentile(data: list[float], p: float) -> float | None:
    """Calculate the p-th percentile (0.0 to 1.0) using linear interpolation."""
    if not data:
        return None
    sorted_d = sorted(data)
    if len(sorted_d) == 1:
        return sorted_d[0]
    k = (len(sorted_d) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_d[int(k)]
    d0 = sorted_d[int(f)] * (c - k)
    d1 = sorted_d[int(c)] * (k - f)
    return d0 + d1


def median(data: list[float]) -> float | None:
    return percentile(data, 0.5)


def get_confidence_tier(sample_size: int) -> str:
    """Confidence tier based on number of posts with usable 7d values."""
    if sample_size < MIN_POSTS_EARLY_SIGNAL:
        return "not enough data"
    if sample_size < MIN_POSTS_FULL_CONFIDENCE:
        return "early signal, low confidence"
    return "full"


def classify_performance(score: float, p25: float, p75: float) -> str:
    """Classify a metric score relative to baseline percentiles."""
    if score > p75:
        return "Above Normal"
    if score < p25:
        return "Below Normal"
    return "At Normal"


def compute_baseline(scores: list[float], window_size: int = BASELINE_WINDOW_SIZE) -> dict[str, Any]:
    """Compute baseline median and 25th-75th percentile range over recent posts."""
    recent = scores[-window_size:] if len(scores) > window_size else scores
    if not recent:
        return {
            "median": None,
            "p25": None,
            "p75": None,
            "sample_size": 0,
            "tier": "not enough data",
        }
    med = median(recent)
    p25 = percentile(recent, 0.25)
    p75 = percentile(recent, 0.75)
    tier = get_confidence_tier(len(recent))
    return {
        "median": round(med, 1) if med is not None else None,
        "p25": round(p25, 1) if p25 is not None else None,
        "p75": round(p75, 1) if p75 is not None else None,
        "sample_size": len(recent),
        "tier": tier,
    }


def compute_early_pace_flags(
    views_at_checkpoint: float | None,
    checkpoint_history: list[float],
) -> str | None:
    """Compute early pace flag (taking off / slow start / normal) for 24h or 48h checkpoint.

    Requires at least 5 historical posts at the checkpoint; returns None otherwise.
    """
    if views_at_checkpoint is None or len(checkpoint_history) < MIN_POSTS_PACE_FLAG:
        return None
    med = median(checkpoint_history)
    if med is None or med <= 0:
        return "normal"
    ratio = views_at_checkpoint / med
    if ratio > 2.0:
        return "taking off"
    if ratio < 0.5:
        return "slow start"
    return "normal"


def get_posting_hour_block(dt_utc: datetime) -> str:
    """Map UTC datetime to IST posting-hour block."""
    local = dt_utc.astimezone(IST_TIMEZONE)
    hour = local.hour
    if 6 <= hour < 12:
        return POSTING_BLOCKS[0]
    if 12 <= hour < 17:
        return POSTING_BLOCKS[1]
    if 17 <= hour < 22:
        return POSTING_BLOCKS[2]
    return POSTING_BLOCKS[3]


def get_weekday_name(dt_utc: datetime) -> str:
    """Weekday name in IST."""
    local = dt_utc.astimezone(IST_TIMEZONE)
    return local.strftime("%A")


def get_caption_length_bucket(caption: str | None) -> str:
    """Bucket caption length."""
    length = len((caption or "").strip())
    if length < 50:
        return "< 50 chars"
    if length <= 150:
        return "50–150 chars"
    return "> 150 chars"


def count_hashtags(caption: str | None) -> int:
    """Count hashtags in caption."""
    if not caption:
        return 0
    return len(re.findall(r"#\w+", caption))


def get_hashtag_bucket(caption: str | None) -> str:
    """Bucket hashtag count."""
    n = count_hashtags(caption)
    if n == 0:
        return "0"
    if 1 <= n <= 3:
        return "1–3"
    if 4 <= n <= 10:
        return "4–10"
    return "> 10"


def group_comparison(
    posts: list[dict[str, Any]],
    key_func: Any,
) -> list[dict[str, Any]]:
    """Group posts by key_func and compute count & median main score (7d views).

    Groups with fewer than 3 posts are marked 'too few posts'.
    """
    groups: dict[str, list[float]] = {}
    for p in posts:
        score = p.get("main_score")
        k = key_func(p)
        if k is not None:
            if k not in groups:
                groups[k] = []
            if score is not None:
                groups[k].append(float(score))
            else:
                # Still track the post even if 7d score is not yet ready
                if not groups[k]:
                    groups[k] = []

    results = []
    for grp_name, scores in sorted(groups.items()):
        cnt = len(scores)
        if cnt < MIN_GROUP_POSTS_FOR_CONCLUSION:
            results.append({
                "group": grp_name,
                "count": cnt,
                "median_views": None,
                "status": "too few posts",
            })
        else:
            med = median(scores)
            results.append({
                "group": grp_name,
                "count": cnt,
                "median_views": round(med, 1) if med is not None else None,
                "status": "sufficient data",
            })
    return results


def analyze_growth_association(
    follower_snapshots: list[dict[str, Any]],
    publication_dates: list[datetime],
) -> dict[str, Any]:
    """Analyze follower trend between consecutive adhoc readings vs posting days.

    follower_snapshots: [{'collected_at': dt, 'followers': count}, ...] sorted asc
    publication_dates: list of post published_at datetimes
    """
    if len(follower_snapshots) < 2:
        return {
            "tier": "not enough data",
            "message": "Fewer than 2 follower snapshots available.",
            "post_day_growth_rate": None,
            "non_post_day_growth_rate": None,
            "intervals": [],
        }

    first_dt = follower_snapshots[0]["collected_at"]
    last_dt = follower_snapshots[-1]["collected_at"]
    total_days = max(1, (last_dt - first_dt).total_seconds() / 86400.0)

    # Post days set: day of publish + next day (in UTC date)
    active_days = set()
    for pub_dt in publication_dates:
        p_date = pub_dt.date()
        active_days.add(p_date)
        active_days.add(p_date + timedelta(days=1))

    intervals = []
    post_day_rates = []
    non_post_day_rates = []

    for i in range(len(follower_snapshots) - 1):
        s1 = follower_snapshots[i]
        s2 = follower_snapshots[i + 1]
        t1, t2 = s1["collected_at"], s2["collected_at"]
        f1, f2 = s1["followers"], s2["followers"]

        hours = max(0.1, (t2 - t1).total_seconds() / 3600.0)
        delta = f2 - f1
        rate_24h = delta / (hours / 24.0)

        # Check if interval overlaps any active posting days
        cur_date = t1.date()
        is_post_related = False
        while cur_date <= t2.date():
            if cur_date in active_days:
                is_post_related = True
                break
            cur_date += timedelta(days=1)

        interval_info = {
            "start": t1,
            "end": t2,
            "hours": round(hours, 1),
            "delta": delta,
            "rate_per_24h": round(rate_24h, 2),
            "is_post_related": is_post_related,
        }
        intervals.append(interval_info)

        if is_post_related:
            post_day_rates.append(rate_24h)
        else:
            non_post_day_rates.append(rate_24h)

    tier = "not enough data" if total_days < MIN_DAYS_GROWTH_CONFIDENCE else "sufficient data"

    return {
        "tier": tier,
        "total_days_observed": round(total_days, 1),
        "post_day_median_rate_24h": round(median(post_day_rates), 2) if post_day_rates else None,
        "non_post_day_median_rate_24h": round(median(non_post_day_rates), 2) if non_post_day_rates else None,
        "intervals": intervals,
        "disclaimer": "association, not cause",
    }
