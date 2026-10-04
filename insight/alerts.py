"""Alert rules engine for Insight Agent.

Pure rules in code. Event alerts are generated inside insight.collect and stored
in the alerts table (with dedupe_key to guarantee no duplicates). Offline/system
health alerts (collector_not_running, collector_failing) are computed at view load
time without writing to the database.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import database as db
from .analysis import IST_TIMEZONE, compute_baseline, compute_early_pace_flags, humanize
from .models import (
    Alert,
    Comment,
    CommentLabel,
    MetricSnapshot,
    MetricValue,
    PostTag,
    Publication,
    Recommendation,
    RecommendationSet,
)
from .timeutil import UTC
from .view_state import is_alert_dismissed, load_view_state

logger = logging.getLogger("insight.alerts")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(REPO_ROOT, "data", "logs", "collect.log")


def format_ist_12h(dt: datetime) -> str:
    """Format datetime in 12-hour IST format (e.g. 'Oct 04, 2026 at 3:30 PM IST')."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist_dt = dt.astimezone(IST_TIMEZONE)
    # Windows strftime %#I for 12-hour without leading zero, or fallback to %I with lstrip('0')
    time_part = ist_dt.strftime("%I:%M %p").lstrip("0")
    date_part = ist_dt.strftime("%b %d, %Y")
    return f"{date_part} at {time_part} IST"


# ── Alert Persistence & Auto-Resolution ───────────────────────────────────────

def upsert_alert(
    session: Session,
    kind: str,
    severity: str,
    dedupe_key: str,
    title: str,
    body: str,
    facts_json: dict[str, Any] | str | None = None,
    now: datetime | None = None,
) -> Alert:
    """Create or update an alert by its unique dedupe_key."""
    current_dt = now or datetime.now(UTC)
    facts_str = json.dumps(facts_json) if isinstance(facts_json, dict) else (facts_json or "{}")

    existing = session.scalar(select(Alert).filter_by(dedupe_key=dedupe_key))
    if existing:
        # If it was resolved, re-activate it if condition re-occurred
        if existing.resolved_at is not None:
            existing.resolved_at = None
            existing.created_at = current_dt
        existing.severity = severity
        existing.title = title
        existing.body = body
        existing.facts_json = facts_str
        session.flush()
        return existing

    new_alert = Alert(
        kind=kind,
        severity=severity,
        dedupe_key=dedupe_key,
        title=title,
        body=body,
        facts_json=facts_str,
        created_at=current_dt,
        resolved_at=None,
    )
    session.add(new_alert)
    session.flush()
    return new_alert


def auto_resolve_alerts(
    session: Session,
    kind: str,
    active_dedupe_keys: set[str],
    now: datetime | None = None,
) -> int:
    """Auto-resolve any open alerts of `kind` whose dedupe_key is no longer active."""
    current_dt = now or datetime.now(UTC)
    open_alerts = session.scalars(
        select(Alert).filter(Alert.kind == kind, Alert.resolved_at.is_(None))
    ).all()

    resolved_count = 0
    for a in open_alerts:
        if a.dedupe_key not in active_dedupe_keys:
            a.resolved_at = current_dt
            resolved_count += 1

    session.flush()
    return resolved_count


# ── Rule 1: Token Expiry Stages ───────────────────────────────────────────────

def evaluate_token_expiry(
    session: Session,
    expires_at_str: str | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Evaluate Meta Access Token expiry stages.

    Stages:
    - Missing -> Warning "Token expiry date not set" (dedupe_key: 'token_expiring:missing')
    - Expired (<0 days) -> Critical "Access token expired" (dedupe_key: 'token_expiring:expired')
    - < 3 days -> Critical "Access token expires soon" (dedupe_key: 'token_expiring:critical_3d')
    - < 14 days -> Warning "Access token renewal needed" (dedupe_key: 'token_expiring:warning_14d')
    - >= 14 days -> Auto-resolves all token alerts.
    """
    current_dt = now or datetime.now(UTC)
    raw_val = expires_at_str or os.environ.get("META_TOKEN_EXPIRES_AT") or db.get_setting("META_TOKEN_EXPIRES_AT")

    active_keys: set[str] = set()

    if not raw_val or not raw_val.strip():
        key = "token_expiring:missing"
        active_keys.add(key)
        a = upsert_alert(
            session=session,
            kind="token_expiring",
            severity="warning",
            dedupe_key=key,
            title="Token expiry date not set",
            body=(
                "META_TOKEN_EXPIRES_AT is not configured in .env. "
                "Get the token expiration from Meta's Access Token Debugger and add it to avoid unexpected outages."
            ),
            facts_json={"configured": False},
            now=current_dt,
        )
        auto_resolve_alerts(session, "token_expiring", active_keys, now=current_dt)
        return [a]

    # Parse ISO datetime
    clean_val = raw_val.strip()
    if clean_val.endswith("Z"):
        clean_val = clean_val[:-1] + "+00:00"

    try:
        exp_dt = datetime.fromisoformat(clean_val)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    except Exception as parse_err:
        key = "token_expiring:invalid"
        active_keys.add(key)
        a = upsert_alert(
            session=session,
            kind="token_expiring",
            severity="warning",
            dedupe_key=key,
            title="Token expiry date format invalid",
            body=f"Could not parse META_TOKEN_EXPIRES_AT ({raw_val}): {parse_err}. Use ISO 8601 format like 2026-11-01T00:00:00Z.",
            facts_json={"raw_value": raw_val, "error": str(parse_err)},
            now=current_dt,
        )
        auto_resolve_alerts(session, "token_expiring", active_keys, now=current_dt)
        return [a]

    seconds_left = (exp_dt - current_dt).total_seconds()
    days_left = seconds_left / 86400.0

    exp_ist = format_ist_12h(exp_dt)

    if days_left <= 0:
        key = "token_expiring:expired"
        active_keys.add(key)
        a = upsert_alert(
            session=session,
            kind="token_expiring",
            severity="critical",
            dedupe_key=key,
            title="Meta access token expired",
            body=f"The Meta access token expired on {exp_ist}. Metric collection cannot reach Instagram until a new token is pasted.",
            facts_json={"expires_at": exp_dt.isoformat(), "days_remaining": round(days_left, 1)},
            now=current_dt,
        )
    elif days_left < 3:
        key = "token_expiring:critical_3d"
        active_keys.add(key)
        a = upsert_alert(
            session=session,
            kind="token_expiring",
            severity="critical",
            dedupe_key=key,
            title=f"Access token expires in {days_left:.1f} days",
            body=f"The Meta access token will expire on {exp_ist}. Renew it immediately in the Meta Developer Portal to prevent collection failure.",
            facts_json={"expires_at": exp_dt.isoformat(), "days_remaining": round(days_left, 1)},
            now=current_dt,
        )
    elif days_left < 14:
        key = "token_expiring:warning_14d"
        active_keys.add(key)
        a = upsert_alert(
            session=session,
            kind="token_expiring",
            severity="warning",
            dedupe_key=key,
            title=f"Access token expires in {int(days_left)} days",
            body=f"The Meta access token expires on {exp_ist}. Plan to refresh your token within the next two weeks.",
            facts_json={"expires_at": exp_dt.isoformat(), "days_remaining": round(days_left, 1)},
            now=current_dt,
        )

    # Auto-resolve previous stages or auto-resolve all if >= 14 days
    auto_resolve_alerts(session, "token_expiring", active_keys, now=current_dt)
    return [a] if active_keys else []


# ── Rule 2: Reel Early Pace Flags (24h, 48h) ──────────────────────────────────

def evaluate_pace_flag_alerts(
    session: Session,
    now: datetime | None = None,
) -> list[Alert]:
    """Check if any recent publication's 24h or 48h checkpoint indicates taking off or slow start."""
    current_dt = now or datetime.now(UTC)
    alerts: list[Alert] = []

    # Requires at least 5 historical posts with 7d views
    all_7d = session.scalars(
        select(MetricValue.value).join(MetricSnapshot).filter(
            MetricSnapshot.checkpoint == "7d",
            MetricValue.canonical_metric == "views",
            MetricValue.value.isnot(None),
        )
    ).all()

    if len(all_7d) < 5:
        return alerts  # Suppressed when fewer than 5 historical posts

    # Check 24h and 48h snapshots created recently (within last 7 days)
    recent_cutoff = current_dt - timedelta(days=7)
    snapshots = session.scalars(
        select(MetricSnapshot).filter(
            MetricSnapshot.checkpoint.in_(["24h", "48h"]),
            MetricSnapshot.collected_at >= recent_cutoff,
            MetricSnapshot.completeness.in_(["complete", "partial"]),
        ).order_by(MetricSnapshot.collected_at.desc())
    ).all()

    for snap in snapshots:
        val = session.scalar(
            select(MetricValue.value).filter_by(snapshot_id=snap.id, canonical_metric="views")
        )
        if val is None:
            continue

        # Get history of that checkpoint
        cp_history = session.scalars(
            select(MetricValue.value).join(MetricSnapshot).filter(
                MetricSnapshot.checkpoint == snap.checkpoint,
                MetricSnapshot.id != snap.id,
                MetricValue.canonical_metric == "views",
                MetricValue.value.isnot(None),
            )
        ).all()

        flag = compute_early_pace_flags(val, cp_history)
        if flag in ("taking off", "slow start"):
            pub = session.scalar(select(Publication).filter_by(id=snap.publication_id))
            pub_title = f"Reel #{pub.id}" if pub else f"Reel #{snap.publication_id}"
            caption_preview = f" ('{(pub.caption or '')[:40]}...')" if pub and pub.caption else ""

            key = f"reel_pace_flag:{snap.publication_id}:{snap.checkpoint}:{flag}"
            severity = "info" if flag == "taking off" else "warning"
            title = f"{pub_title} is {flag} at {snap.checkpoint}"
            body = (
                f"{pub_title}{caption_preview} reached {int(val):,} views at its {snap.checkpoint} checkpoint "
                f"({format_ist_12h(snap.collected_at)}), which is {flag} compared to typical historical performance."
            )
            a = upsert_alert(
                session=session,
                kind="reel_pace_flag",
                severity=severity,
                dedupe_key=key,
                title=title,
                body=body,
                facts_json={
                    "publication_id": snap.publication_id,
                    "checkpoint": snap.checkpoint,
                    "views": int(val),
                    "flag": flag,
                },
                now=current_dt,
            )
            alerts.append(a)

    return alerts


# ── Rule 3: 7-Day Result In ───────────────────────────────────────────────────

def evaluate_checkpoint_result_alerts(
    session: Session,
    now: datetime | None = None,
) -> list[Alert]:
    """Alert when a publication completes its mature 7-day checkpoint."""
    current_dt = now or datetime.now(UTC)
    alerts: list[Alert] = []

    # Check 7d snapshots collected in the last 48 hours
    cutoff = current_dt - timedelta(hours=48)
    snaps_7d = session.scalars(
        select(MetricSnapshot).filter(
            MetricSnapshot.checkpoint == "7d",
            MetricSnapshot.collected_at >= cutoff,
            MetricSnapshot.completeness.in_(["complete", "partial"]),
        )
    ).all()

    for snap in snaps_7d:
        val = session.scalar(
            select(MetricValue.value).filter_by(snapshot_id=snap.id, canonical_metric="views")
        )
        if val is None:
            continue

        key = f"checkpoint_result_in:{snap.publication_id}:7d"
        pub = session.scalar(select(Publication).filter_by(id=snap.publication_id))
        pub_title = f"Reel #{pub.id}" if pub else f"Reel #{snap.publication_id}"
        caption_preview = f" ('{(pub.caption or '')[:40]}...')" if pub and pub.caption else ""

        a = upsert_alert(
            session=session,
            kind="checkpoint_result_in",
            severity="info",
            dedupe_key=key,
            title=f"7-day result ready for {pub_title}",
            body=(
                f"{pub_title}{caption_preview} recorded {int(val):,} views at 7 days "
                f"({format_ist_12h(snap.collected_at)}). Full performance comparison and learning feedback are ready."
            ),
            facts_json={"publication_id": snap.publication_id, "views": int(val)},
            now=current_dt,
        )
        alerts.append(a)

    return alerts


# ── Rule 4: Recommendations & Outcomes ────────────────────────────────────────

def evaluate_recommendation_alerts(
    session: Session,
    now: datetime | None = None,
) -> list[Alert]:
    """Alert when a new recommendation set is created or a followed recommendation gets its 7d outcome."""
    current_dt = now or datetime.now(UTC)
    alerts: list[Alert] = []

    # 1. New weekly recommendation set generated in last 48 hours
    cutoff = current_dt - timedelta(hours=48)
    sets = session.scalars(
        select(RecommendationSet).filter(RecommendationSet.generated_at >= cutoff)
    ).all()

    for r_set in sets:
        key = f"weekly_recommendations_ready:{r_set.week_key}"
        rec_count = len(r_set.recommendations)
        a = upsert_alert(
            session=session,
            kind="weekly_recommendations_ready",
            severity="info",
            dedupe_key=key,
            title=f"New recommendations ready for {r_set.week_key}",
            body=(
                f"Generated {rec_count} actionable content recommendation(s) for {r_set.week_key} "
                f"in {r_set.mode.upper()} mode ({format_ist_12h(r_set.generated_at)})."
            ),
            facts_json={"week_key": r_set.week_key, "mode": r_set.mode, "count": rec_count},
            now=current_dt,
        )
        alerts.append(a)

    # 2. Recommendation outcome recorded
    followed_with_outcome = session.scalars(
        select(Recommendation).filter(
            Recommendation.status == "followed",
            Recommendation.outcome_ratio.isnot(None),
        )
    ).all()

    for rec in followed_with_outcome:
        key = f"recommendation_outcome_recorded:{rec.id}"
        ratio = rec.outcome_ratio or 1.0
        perf_text = "above baseline" if ratio >= 1.2 else ("below baseline" if ratio <= 0.8 else "at baseline")
        a = upsert_alert(
            session=session,
            kind="recommendation_outcome_recorded",
            severity="info",
            dedupe_key=key,
            title=f"Outcome recorded for #{rec.rank} [{humanize(rec.topic)}]",
            body=(
                f"Recommendation #{rec.rank} for {humanize(rec.topic)} was followed by Reel #{rec.matched_publication_id} "
                f"and achieved {ratio:.1f}x baseline ({perf_text}) at its 7-day checkpoint."
            ),
            facts_json={
                "recommendation_id": rec.id,
                "publication_id": rec.matched_publication_id,
                "outcome_ratio": ratio,
            },
            now=current_dt,
        )
        alerts.append(a)

    return alerts


# ── Rule 5: Needs Reply (Dormant until comments exist) ─────────────────────────

def evaluate_needs_reply_alerts(
    session: Session,
    now: datetime | None = None,
) -> list[Alert]:
    """Alert for unanswered viewer questions or requests (dormant until comments exist)."""
    current_dt = now or datetime.now(UTC)
    alerts: list[Alert] = []

    unanswered = session.scalars(
        select(Comment).join(CommentLabel).filter(
            CommentLabel.needs_reply == True,
            Comment.is_own_account == False,
        )
    ).all()

    for c in unanswered:
        key = f"needs_reply:{c.id}"
        label = session.scalar(select(CommentLabel).filter_by(comment_id=c.id))
        category = label.category if label else "inquiry"
        a = upsert_alert(
            session=session,
            kind="needs_reply",
            severity="info",
            dedupe_key=key,
            title=f"Viewer {category} needs a reply",
            body=f"Comment #{c.id} on Reel #{c.publication_id} was flagged as a viewer {category}. Reply in the Instagram app.",
            facts_json={"comment_id": c.id, "publication_id": c.publication_id, "category": category},
            now=current_dt,
        )
        alerts.append(a)

    return alerts


# ── Collector All-Events Wrapper ───────────────────────────────────────────────

def evaluate_all_collector_alerts(
    session: Session,
    now: datetime | None = None,
) -> list[Alert]:
    """Evaluate all event-driven alerts that run inside insight.collect."""
    cur = now or datetime.now(UTC)
    alerts: list[Alert] = []

    try:
        alerts.extend(evaluate_token_expiry(session, now=cur))
    except Exception as e:
        logger.warning(f"Error evaluating token expiry alert: {e}")

    try:
        alerts.extend(evaluate_pace_flag_alerts(session, now=cur))
    except Exception as e:
        logger.warning(f"Error evaluating pace flag alerts: {e}")

    try:
        alerts.extend(evaluate_checkpoint_result_alerts(session, now=cur))
    except Exception as e:
        logger.warning(f"Error evaluating checkpoint result alerts: {e}")

    try:
        alerts.extend(evaluate_recommendation_alerts(session, now=cur))
    except Exception as e:
        logger.warning(f"Error evaluating recommendation alerts: {e}")

    try:
        alerts.extend(evaluate_needs_reply_alerts(session, now=cur))
    except Exception as e:
        logger.warning(f"Error evaluating needs reply alerts: {e}")

    return alerts


# ── Offline / View-Time Computed Alerts (Read-Only) ────────────────────────────

def parse_collector_log_recent_runs(
    log_path: str = LOG_PATH,
    max_lines: int = 500,
) -> list[dict[str, Any]]:
    """Parse recent collector run summary lines from collect.log."""
    if not os.path.exists(log_path):
        return []

    runs = []
    summary_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}).*?\[collect\]\s+(OK|ERROR|FAIL|FAILED)"
    )

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
            for line in lines:
                m = summary_pattern.search(line)
                if m:
                    dt_str, status = m.groups()
                    try:
                        run_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except Exception:
                        run_dt = datetime.now(timezone.utc)
                    runs.append({
                        "timestamp": run_dt,
                        "status": status,
                        "raw_line": line.strip(),
                    })
    except Exception as log_err:
        logger.warning(f"Could not parse collector log {log_path}: {log_err}")

    return runs


def evaluate_view_time_system_alerts(
    last_run_dt: datetime | None,
    log_path: str = LOG_PATH,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Compute offline/system alerts at view load time without writing to the database.

    1. collector_not_running: No successful collector run in past 24 hours.
    2. collector_failing: 3 consecutive runs whose summary line is not OK.
    """
    current_dt = now or datetime.now(timezone.utc)
    alerts: list[dict[str, Any]] = []

    # 1. Collector not running for 24h
    if last_run_dt is None:
        alerts.append({
            "kind": "collector_not_running",
            "severity": "critical",
            "dedupe_key": "collector_not_running:never",
            "title": "Collector has never successfully run",
            "body": "No successful collector runs have been recorded. Verify your scheduler or run python -m insight.collect.",
            "created_at": current_dt,
        })
    else:
        hours_since = (current_dt - last_run_dt).total_seconds() / 3600.0
        if hours_since >= 24.0:
            last_ist = format_ist_12h(last_run_dt)
            alerts.append({
                "kind": "collector_not_running",
                "severity": "critical",
                "dedupe_key": f"collector_not_running:{last_run_dt.date().isoformat()}",
                "title": f"Collector has not run in {int(hours_since)} hours",
                "body": f"Last successful collector run was {last_ist}. Ensure the Windows scheduled task or collector process is active.",
                "created_at": current_dt,
            })

    # 2. Collector failing 3 consecutive runs
    recent_runs = parse_collector_log_recent_runs(log_path=log_path)
    if len(recent_runs) >= 3:
        last_3 = recent_runs[-3:]
        all_failed = all(r["status"] != "OK" for r in last_3)
        if all_failed:
            last_fail_dt = last_3[-1]["timestamp"]
            alerts.append({
                "kind": "collector_failing",
                "severity": "critical",
                "dedupe_key": f"collector_failing:{last_fail_dt.strftime('%Y%m%d%H%M')}",
                "title": "Collector failed 3 consecutive runs",
                "body": (
                    f"The last 3 collector runs failed ({format_ist_12h(last_fail_dt)}). "
                    "Check data/logs/collect.log for API errors, token expiry, or network failures."
                ),
                "created_at": current_dt,
            })

    return alerts
