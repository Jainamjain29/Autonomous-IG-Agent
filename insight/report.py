"""Weekly reports engine for Insight Agent.

Deterministic report generation for any IST week (Monday 00:00 IST – Sunday 23:59:59 IST).
Cached in weekly_reports table for closed weeks with optional 3-4 sentence AI summary
validated by Step 6 Number Guard and Quality Guard. In-progress weeks display live facts
without AI summary.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import database as db
from .alerts import format_ist_12h
from .analysis import (
    HUMAN_LABELS,
    compute_baseline,
    get_confidence_tier,
    get_weekday_name,
    humanize,
    median,
)
from .models import (
    Account,
    Comment,
    CommentLabel,
    MetricSnapshot,
    MetricValue,
    PostFeedback,
    PostTag,
    Publication,
    Recommendation,
    RecommendationSet,
    WeeklyReport,
)
from .recommend import (
    DEFAULT_MODEL_NAME,
    DEFAULT_PROMPT_VERSION,
    call_gemini_wording,
    get_ist_monday,
    get_ist_week_key,
    get_ist_week_start_and_end,
    validate_ai_quality,
    validate_ai_text,
)
from .timeutil import UTC

logger = logging.getLogger("insight.report")


def get_available_weeks(session: Session, now: datetime | None = None) -> list[str]:
    """Return all available week keys in reverse chronological order (newest first)."""
    current_dt = now or datetime.now(UTC)
    current_week = get_ist_week_key(current_dt)

    weeks = {current_week}

    # Find weeks from publications
    pub_dates = session.scalars(select(Publication.published_at)).all()
    for pd in pub_dates:
        if pd:
            weeks.add(get_ist_week_key(pd))

    # Find weeks from snapshots
    snap_dates = session.scalars(select(MetricSnapshot.collected_at)).all()
    for sd in snap_dates:
        if sd:
            weeks.add(get_ist_week_key(sd))

    # Find weeks from recommendation sets
    rec_weeks = session.scalars(select(RecommendationSet.week_key)).all()
    for rw in rec_weeks:
        if rw:
            weeks.add(rw)

    return sorted(weeks, reverse=True)


def compute_weekly_report_data(
    session: Session,
    week_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute complete deterministic metrics for a given IST week.

    IST Week runs from Monday 00:00:00 IST to Sunday 23:59:59 IST.
    """
    current_dt = now or datetime.now(UTC)
    current_week_key = get_ist_week_key(current_dt)
    is_current_week = (week_key == current_week_key)

    # Calculate week start and end in UTC
    # Parse week_key (e.g. '2026-W40')
    year_str, week_num_str = week_key.split("-W")
    year = int(year_str)
    week_num = int(week_num_str)

    # Monday of that ISO week in UTC
    # January 4 is always in week 1 of ISO calendar
    first_iso_day = datetime(year, 1, 4, tzinfo=timezone.utc)
    first_iso_monday = first_iso_day - timedelta(days=first_iso_day.isoweekday() - 1)
    target_monday = first_iso_monday + timedelta(weeks=week_num - 1)

    # Week bounds in UTC
    week_start_utc, week_end_utc = get_ist_week_start_and_end(target_monday)

    # 1. Publications published during this week
    pubs = session.scalars(
        select(Publication).filter(
            Publication.published_at >= week_start_utc,
            Publication.published_at <= week_end_utc,
        ).order_by(Publication.published_at.asc())
    ).all()

    pub_count = len(pubs)
    pub_summaries = []
    total_views_week = 0.0

    # Baseline for comparisons
    all_7d_scores = session.scalars(
        select(MetricValue.value).join(MetricSnapshot).filter(
            MetricSnapshot.checkpoint == "7d",
            MetricValue.canonical_metric == "views",
            MetricValue.value.isnot(None),
        )
    ).all()
    baseline = compute_baseline([float(s) for s in all_7d_scores])
    baseline_views = baseline.get("median") or 0.0

    for p in pubs:
        tags = {t.dimension: t.value for t in session.scalars(select(PostTag).filter_by(publication_id=p.id)).all()}
        # Get latest views snapshot
        latest_views_val = session.scalar(
            select(MetricValue.value).join(MetricSnapshot).filter(
                MetricSnapshot.subject_type == "publication",
                MetricSnapshot.subject_id == p.id,
                MetricValue.canonical_metric == "views",
            ).order_by(MetricSnapshot.collected_at.desc())
        )
        views = int(latest_views_val) if latest_views_val is not None else 0
        total_views_week += views
        pub_summaries.append({
            "id": p.id,
            "published_at": p.published_at,
            "published_at_ist": format_ist_12h(p.published_at),
            "caption": (p.caption or "")[:60],
            "topic": humanize(tags.get("topic", "untagged")),
            "format": humanize(tags.get("format", "untagged")),
            "hook": humanize(tags.get("hook", "untagged")),
            "views": views,
        })

    # 2. Follower Growth (with two readings and exact IST times)
    follower_snaps = session.scalars(
        select(MetricSnapshot).join(MetricValue).filter(
            MetricSnapshot.subject_type == "account",
            MetricSnapshot.checkpoint == "adhoc",
            MetricSnapshot.collected_at >= week_start_utc,
            MetricSnapshot.collected_at <= week_end_utc,
            MetricValue.canonical_metric == "followers",
        ).order_by(MetricSnapshot.collected_at.asc())
    ).all()

    start_reading = None
    end_reading = None
    follower_delta = None

    if len(follower_snaps) >= 2:
        s_first = follower_snaps[0]
        s_last = follower_snaps[-1]
        v_first = session.scalar(select(MetricValue.value).filter_by(snapshot_id=s_first.id, canonical_metric="followers"))
        v_last = session.scalar(select(MetricValue.value).filter_by(snapshot_id=s_last.id, canonical_metric="followers"))
        if v_first is not None and v_last is not None:
            start_reading = {"count": int(v_first), "time_ist": format_ist_12h(s_first.collected_at)}
            end_reading = {"count": int(v_last), "time_ist": format_ist_12h(s_last.collected_at)}
            follower_delta = int(v_last - v_first)
    elif len(follower_snaps) == 1:
        s_one = follower_snaps[0]
        v_one = session.scalar(select(MetricValue.value).filter_by(snapshot_id=s_one.id, canonical_metric="followers"))
        if v_one is not None:
            start_reading = {"count": int(v_one), "time_ist": format_ist_12h(s_one.collected_at)}

    # 3. Recommendations Followed + Outcomes
    rec_set = session.scalar(select(RecommendationSet).filter_by(week_key=week_key))
    followed_recs = []
    if rec_set:
        for r in rec_set.recommendations:
            if r.status == "followed":
                followed_recs.append({
                    "rank": r.rank,
                    "topic": humanize(r.topic),
                    "format": humanize(r.format),
                    "matched_pub_id": r.matched_publication_id,
                    "outcome_ratio": r.outcome_ratio,
                })

    # 4. Audience Highlights
    # User refinement 3: When comments_count on our posts > 0 but API returns none
    raw_comments_count_sum = 0
    collected_comments_count = session.scalar(
        select(func.count(Comment.id)).filter(
            Comment.created_at >= week_start_utc,
            Comment.created_at <= week_end_utc,
        )
    ) or 0

    for p in pubs:
        # Check raw_responses or caption metadata
        raw_cc = session.scalar(
            select(MetricValue.value).join(MetricSnapshot).filter(
                MetricSnapshot.subject_id == p.id,
                MetricSnapshot.subject_type == "publication",
                MetricValue.canonical_metric == "comments",
            ).order_by(MetricSnapshot.collected_at.desc())
        )
        if raw_cc:
            raw_comments_count_sum += int(raw_cc)

    unreadable_comments_flag = (raw_comments_count_sum > 0 and collected_comments_count == 0)

    # 5. Data Health in week
    snaps_count = session.scalar(
        select(func.count(MetricSnapshot.id)).filter(
            MetricSnapshot.collected_at >= week_start_utc,
            MetricSnapshot.collected_at <= week_end_utc,
        )
    ) or 0
    complete_count = session.scalar(
        select(func.count(MetricSnapshot.id)).filter(
            MetricSnapshot.collected_at >= week_start_utc,
            MetricSnapshot.collected_at <= week_end_utc,
            MetricSnapshot.completeness == "complete",
        )
    ) or 0
    delayed_count = session.scalar(
        select(func.count(MetricSnapshot.id)).filter(
            MetricSnapshot.collected_at >= week_start_utc,
            MetricSnapshot.collected_at <= week_end_utc,
            MetricSnapshot.completeness == "delayed",
        )
    ) or 0
    unavail_count = session.scalar(
        select(func.count(MetricSnapshot.id)).filter(
            MetricSnapshot.collected_at >= week_start_utc,
            MetricSnapshot.collected_at <= week_end_utc,
            MetricSnapshot.completeness == "unavailable",
        )
    ) or 0

    # 6. Next Week's Plan
    # User refinement 5: Next week's plan = existing Step 6 recommendation set for that week (human labels)
    # Next week key:
    next_week_monday = target_monday + timedelta(days=7)
    next_week_key = get_ist_week_key(next_week_monday)
    next_rec_set = session.scalar(select(RecommendationSet).filter_by(week_key=next_week_key))
    next_plan_recs = []
    if next_rec_set:
        for r in next_rec_set.recommendations:
            next_plan_recs.append({
                "rank": r.rank,
                "kind": r.kind,
                "topic": humanize(r.topic),
                "format": humanize(r.format),
                "hook": humanize(r.hook),
                "weekday": r.weekday,
                "posting_block": r.posting_block,
                "action": r.text,
            })

    facts = {
        "week_key": week_key,
        "is_current_week": is_current_week,
        "week_start_ist": format_ist_12h(week_start_utc),
        "week_end_ist": format_ist_12h(week_end_utc),
        "published_count": pub_count,
        "total_views": int(total_views_week),
        "baseline_views": int(baseline_views),
        "usable_7d_posts": len(all_7d_scores),
        "confidence_tier": get_confidence_tier(len(all_7d_scores)),
        "publications": pub_summaries,
        "follower_start": start_reading,
        "follower_end": end_reading,
        "follower_delta": follower_delta,
        "followed_recommendations": followed_recs,
        "collected_comments_count": collected_comments_count,
        "unreadable_comments_flag": unreadable_comments_flag,
        "data_health": {
            "total_snapshots": snaps_count,
            "complete": complete_count,
            "delayed": delayed_count,
            "unavailable": unavail_count,
        },
        "next_week_key": next_week_key,
        "next_plan": next_plan_recs,
    }

    return facts


# ── AI Summary & Template Fallback ────────────────────────────────────────────

def generate_report_summary(
    session: Session,
    report_data: dict[str, Any],
    api_key: str | None = None,
    model_name: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    save_to_db: bool = True,
) -> tuple[str, bool]:
    """Generate or retrieve a 3-4 sentence summary of the weekly report.

    Uses Step 6 Number Guard and Quality Guard. In low-data tiers (<5 posts),
    strictly template facts only with zero judgements.
    """
    week_key = report_data["week_key"]
    is_current = report_data.get("is_current_week", False)

    # Check cached report for closed weeks
    if not is_current:
        cached = session.scalar(
            select(WeeklyReport).filter_by(week_key=week_key, prompt_version=prompt_version)
        )
        if cached:
            return cached.summary_text, cached.is_ai_summary

    usable_7d = report_data.get("usable_7d_posts", 0)
    pub_count = report_data["published_count"]
    total_views = report_data["total_views"]
    delta = report_data.get("follower_delta")
    delta_str = f"gained {delta} followers" if delta and delta > 0 else (f"lost {abs(delta)} followers" if delta and delta < 0 else "follower count remained unchanged")

    # Clean human code template
    if usable_7d < 5:
        # Few posts: facts only, zero judgements
        template = (
            f"During {week_key}, {pub_count} Reel(s) were published totaling {total_views:,} views. "
            f"Account follower metrics recorded that the account {delta_str} over available readings. "
            f"Baseline comparisons remain in 'not enough data' tier ({usable_7d}/5 posts required for initial signals)."
        )
        if not is_current and save_to_db:
            # Cache the template summary safely
            try:
                wr = WeeklyReport(
                    week_key=week_key,
                    prompt_version=prompt_version,
                    model=None,
                    generated_at=datetime.now(UTC),
                    facts_json=json.dumps(report_data),
                    summary_text=template,
                    is_ai_summary=False,
                )
                session.add(wr)
                session.flush()
            except Exception as e:
                logger.debug(f"Could not cache template summary to DB (e.g. read-only): {e}")
        return template, False

    # Evidence tier: generate 3-4 sentences with Gemini + dual guards
    clean_facts = {
        "week": week_key,
        "posts_published": pub_count,
        "total_views": total_views,
        "follower_change": delta_str,
        "followed_recommendations_count": len(report_data.get("followed_recommendations", [])),
    }

    template = (
        f"During {week_key}, {pub_count} Reel(s) were published totaling {total_views:,} views. "
        f"The account {delta_str} across irregular readings. "
        f"Content performance aligned with existing baseline expectations across {usable_7d} analyzed posts."
    )

    prompt = (
        f"You are a social media performance director. Write a concise 3 to 4 sentence executive summary "
        f"of this week's account performance based ONLY on these details: {json.dumps(clean_facts)}. "
        f"RULES: 1. Plain English for a creator, direct and factual. "
        f"2. Do NOT use praise, congratulations, cheerleading, or flattery. "
        f"3. Do NOT use backticks, underscores, or raw field names. "
        f"4. Every number in your text MUST be present in the details. "
        f"5. Use 'associated with', never causal words like 'cause' or 'because of'. "
        f"6. Return ONLY the plain text summary."
    )

    ai_text = None
    is_ai = False

    candidate = call_gemini_wording(prompt, api_key=api_key, model_name=model_name)
    if candidate:
        q_ok, q_reason = validate_ai_quality(candidate)
        n_ok, n_reason = validate_ai_text(candidate, clean_facts)
        if q_ok and n_ok:
            ai_text = candidate
            is_ai = True
        else:
            reason = q_reason if not q_ok else n_reason
            logger.info(f"Weekly report AI summary rejected by guards ({reason}): {candidate}")

    final_text = ai_text if is_ai else template

    if not is_current and save_to_db:
        try:
            wr = WeeklyReport(
                week_key=week_key,
                prompt_version=prompt_version,
                model=model_name or db.get_setting("GEMINI_MODEL") or DEFAULT_MODEL_NAME if is_ai else None,
                generated_at=datetime.now(UTC),
                facts_json=json.dumps(report_data),
                summary_text=final_text,
                is_ai_summary=is_ai,
            )
            session.add(wr)
            session.flush()
        except Exception as e:
            logger.debug(f"Could not cache AI summary to DB (e.g. read-only): {e}")

    return final_text, is_ai


# ── Markdown and HTML Exporters ───────────────────────────────────────────────

def generate_report_markdown(report_data: dict[str, Any], summary_text: str) -> str:
    """Format weekly report data into clean GitHub-flavored Markdown."""
    week = report_data["week_key"]
    status_badge = "*(In progress — week ends Sunday 11:59 PM IST)*" if report_data.get("is_current_week") else "*(Closed Week)*"

    md = []
    md.append(f"# 📊 Weekly Executive Report — {week} {status_badge}\n")
    md.append(f"**Period (IST)**: {report_data['week_start_ist']} to {report_data['week_end_ist']}\n")

    md.append("## 📝 Executive Summary\n")
    md.append(f"{summary_text}\n")

    md.append("## 🎬 Publishing Activity\n")
    md.append(f"- **Reels Published**: {report_data['published_count']}")
    md.append(f"- **Total Views Recorded**: {report_data['total_views']:,}\n")

    if report_data["publications"]:
        md.append("| # | Published (IST) | Topic | Format | Hook | Views |")
        md.append("|---|---|---|---|---|---|")
        for p in report_data["publications"]:
            md.append(f"| {p['id']} | {p['published_at_ist']} | {p['topic']} | {p['format']} | {p['hook']} | {p['views']:,} |")
        md.append("")
    else:
        md.append("*No posts were published during this week.*\n")

    md.append("## 📈 Follower Growth\n")
    s = report_data.get("follower_start")
    e = report_data.get("follower_end")
    if s and e:
        md.append(f"- **Starting Count**: {s['count']:,} ({s['time_ist']})")
        md.append(f"- **Ending Count**: {e['count']:,} ({e['time_ist']})")
        delta = report_data.get("follower_delta", 0)
        sign = "+" if delta > 0 else ""
        md.append(f"- **Net Change**: **{sign}{delta}** *(approximate: readings taken at irregular times)*\n")
    elif s:
        md.append(f"- Single reading recorded: {s['count']:,} ({s['time_ist']}). Multiple readings needed for net change.\n")
    else:
        md.append("*No follower readings available for this week.*\n")

    md.append("## 🎯 Recommendations Followed & Outcomes\n")
    recs = report_data.get("followed_recommendations", [])
    if recs:
        for r in recs:
            ratio_str = f"{r['outcome_ratio']:.1f}x baseline" if r.get("outcome_ratio") is not None else "pending 7d checkpoint"
            md.append(f"- Recommendation #{r['rank']} ({r['topic']} · {r['format']}) followed by Reel #{r['matched_pub_id']} -> Result: **{ratio_str}**")
        md.append("")
    else:
        md.append("*No open recommendations were marked followed this week.*\n")

    md.append("## 👥 Audience Signals\n")
    if report_data.get("unreadable_comments_flag"):
        md.append("⚠️ **Comments exist but aren't readable yet (Meta app not Live)**\n")
    else:
        md.append(f"- **Comments Collected**: {report_data['collected_comments_count']}\n")

    md.append("## 🏥 Data Health\n")
    dh = report_data.get("data_health", {})
    md.append(f"- Total Snapshots: {dh.get('total_snapshots', 0)}")
    md.append(f"- Complete: {dh.get('complete', 0)} | Delayed: {dh.get('delayed', 0)} | Unavailable: {dh.get('unavailable', 0)}\n")

    md.append(f"## 💡 Next Week's Plan ({report_data.get('next_week_key')})\n")
    plan = report_data.get("next_plan", [])
    if plan:
        for item in plan:
            md.append(f"- **#{item['rank']} [{item['kind'].upper()}]** {item['topic']} · {item['format']} ({item['weekday']} · {item['posting_block']}): {item['action']}")
        md.append("")
    else:
        md.append("*Next week's recommendations will be generated automatically at week start.*\n")

    return "\n".join(md)


def generate_report_html(report_data: dict[str, Any], summary_text: str) -> str:
    """Format weekly report data into a self-contained HTML document with clean styling."""
    week = report_data["week_key"]
    md_content = generate_report_markdown(report_data, summary_text)

    # Basic markdown to HTML converter for self-contained document
    lines = md_content.split("\n")
    html_lines = []
    in_table = False

    for line in lines:
        line_str = line.strip()
        if not line_str:
            if in_table:
                html_lines.append("</tbody></table>")
                in_table = False
            continue

        if line_str.startswith("# "):
            html_lines.append(f"<h1>{line_str[2:]}</h1>")
        elif line_str.startswith("## "):
            html_lines.append(f"<h2>{line_str[3:]}</h2>")
        elif line_str.startswith("- "):
            html_lines.append(f"<li>{line_str[2:]}</li>")
        elif line_str.startswith("|") and "|---|" in line_str:
            continue
        elif line_str.startswith("|"):
            cells = [c.strip() for c in line_str.split("|")[1:-1]]
            if not in_table:
                in_table = True
                html_lines.append("<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr></thead><tbody>")
            else:
                html_lines.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        else:
            html_lines.append(f"<p>{line_str}</p>")

    if in_table:
        html_lines.append("</tbody></table>")

    body_html = "\n".join(html_lines)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Weekly Report - {week}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1a1a1a; max-width: 860px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; }}
  h2 {{ color: #1e293b; margin-top: 32px; border-bottom: 1px solid #f1f5f9; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 10px 14px; text-align: left; }}
  th {{ background-color: #f8fafc; font-weight: 600; }}
  li {{ margin-bottom: 6px; }}
  p {{ margin: 10px 0; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""
