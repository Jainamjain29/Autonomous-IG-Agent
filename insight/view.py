"""Streamlit metrics view for the Insight Agent.

Run:  streamlit run insight/view.py

Strictly read-only: opens data/insight.db in mode=ro, never writes.
Does NOT run Alembic upgrades or trigger collection.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# Ensure repo root is on sys.path so insight is importable when view.py is run as a script
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import streamlit as st

from insight.db import make_readonly_engine
from insight.queries import (
    ACCOUNT_DAILY_METRICS,
    CHECKPOINT_ORDER,
    COMPLETENESS_MARKERS,
    NOT_DUE_MARKER,
    POST_METRICS,
    check_alembic_version,
    check_db_exists,
    check_has_publications,
    format_age,
    format_metric_value,
    get_account_info,
    get_daily_account_metrics,
    get_followers_over_time,
    get_overdue_checkpoints,
    get_posts_with_snapshots,
    get_raw_response_stats,
    get_snapshot_completeness_counts,
    last_successful_run,
    parse_collect_log,
    _format_time_12h,
    _time_ago,
)
from insight.timeutil import UTC

# ── Page config ────────────────────────────────────────────────────────
st.set_page_config(page_title="Insight · Metrics View", page_icon="📊", layout="wide")


def _get_db_path() -> str | None:
    return os.environ.get("INSIGHT_VIEW_DB_PATH")


def _get_engine():
    """Get or create the read-only engine. Cached in session state."""
    db_path = _get_db_path()
    if "ro_engine" not in st.session_state or st.session_state.ro_engine is None:
        st.session_state.ro_engine = make_readonly_engine(db_path=db_path)
    return st.session_state.ro_engine


# ── Cached query wrappers ─────────────────────────────────────────────

@st.cache_data(ttl=60)
def _cached_posts():
    engine = _get_engine()
    if engine is None:
        return []
    return get_posts_with_snapshots(engine)


@st.cache_data(ttl=60)
def _cached_daily_metrics():
    engine = _get_engine()
    if engine is None:
        return []
    return get_daily_account_metrics(engine)


@st.cache_data(ttl=60)
def _cached_followers():
    engine = _get_engine()
    if engine is None:
        return []
    return get_followers_over_time(engine)


@st.cache_data(ttl=60)
def _cached_completeness():
    engine = _get_engine()
    if engine is None:
        return {}
    return get_snapshot_completeness_counts(engine)


@st.cache_data(ttl=60)
def _cached_overdue():
    engine = _get_engine()
    if engine is None:
        return []
    return get_overdue_checkpoints(engine)


@st.cache_data(ttl=60)
def _cached_raw_stats():
    engine = _get_engine()
    if engine is None:
        return {"count": 0, "latest_fetched_at": None}
    return get_raw_response_stats(engine)


@st.cache_data(ttl=60)
def _cached_log():
    return parse_collect_log()


@st.cache_data(ttl=60)
def _cached_account_info():
    engine = _get_engine()
    if engine is None:
        return None
    return get_account_info(engine)


# ── Pre-flight checks ─────────────────────────────────────────────────

def _preflight() -> bool:
    """Run pre-flight checks. Returns True if we can proceed."""
    if not check_db_exists(_get_db_path()):
        st.warning(
            "🚫 **No data yet** — the collector has not run on this machine.\n\n"
            "Run `python -m insight.collect` first, or wait for the scheduled task.",
            icon="⚠️",
        )
        return False

    engine = _get_engine()
    if engine is None:
        st.warning("🚫 **No data yet** — the collector has not run on this machine.")
        return False

    at_head, version = check_alembic_version(engine)
    if not at_head:
        st.warning(
            f"⚠️ Database schema is not at the latest migration (current: `{version}`). "
            "Run `python -m insight.collect` to upgrade.",
        )

    if not check_has_publications(engine):
        st.warning(
            "🚫 **No data yet** — the collector has not run on this machine.\n\n"
            "Run `python -m insight.collect` first, or wait for the scheduled task.",
        )
        return False

    return True


# ── Legend ─────────────────────────────────────────────────────────────

def _show_legend():
    """Show the completeness markers legend."""
    st.caption(
        "**Legend:** "
        "✅ complete · "
        "⏰ delayed · "
        "⚠️ partial · "
        "❌ unavailable · "
        "⏳ not due yet"
    )


# ── Tab: Posts ─────────────────────────────────────────────────────────

def _render_posts_tab():
    st.subheader("📋 Publications")
    _show_legend()

    posts = _cached_posts()
    if not posts:
        st.info("No publications found.")
        return

    for post in posts:
        # Summary row
        published_str = _format_time_12h(post["published_at"])
        age_str = format_age(post["age_seconds"])
        media_label = post.get("media_product_type") or post.get("media_type", "—")
        caption_short = (post["caption"] or "")[:60] or "—"

        # Header line
        cols = st.columns([2, 1, 1, 3, 1])
        cols[0].markdown(f"**{published_str}**")
        cols[1].markdown(f"Age: **{age_str}**")
        cols[2].markdown(f"`{media_label}`")
        cols[3].markdown(caption_short)
        if post.get("permalink"):
            cols[4].markdown(f"[🔗 Link]({post['permalink']})")

        # Checkpoint summary
        cp_cols = st.columns(len(CHECKPOINT_ORDER) + 1)
        cp_cols[0].markdown("**Checkpoint →**")
        for i, cp in enumerate(CHECKPOINT_ORDER):
            status = post["checkpoint_status"].get(cp, "not_due")
            if status == "not_due":
                marker = NOT_DUE_MARKER
            elif status == "overdue":
                marker = "⚡"
            else:
                marker = COMPLETENESS_MARKERS.get(status, "?")

            snap = post["snapshots"].get(cp)
            if snap and snap["metrics"]:
                views_val = snap["metrics"].get("views", {}).get("value")
                views_str = format_metric_value(views_val, "views") if views_val is not None else "—"
                real_age = format_age(snap.get("time_since_publish_seconds"))
                cp_cols[i + 1].markdown(f"**{cp}** {marker}\n\n👁 {views_str}\n\n⏱ {real_age}")
            else:
                cp_cols[i + 1].markdown(f"**{cp}** {marker}")

        # Latest adhoc
        adhoc = post["snapshots"].get("adhoc")
        if adhoc and adhoc["metrics"]:
            adhoc_views = adhoc["metrics"].get("views", {}).get("value")
            st.caption(
                f"Latest adhoc: {_format_time_12h(adhoc['collected_at'])} · "
                f"👁 {format_metric_value(adhoc_views, 'views')}"
            )

        # Expander with full detail
        with st.expander(f"📊 All snapshots for post {post['platform_post_id'][:12]}…"):
            for cp_label in CHECKPOINT_ORDER + ["adhoc"]:
                snap = post["snapshots"].get(cp_label)
                if snap is None:
                    status = post["checkpoint_status"].get(cp_label, "not_due")
                    if status == "not_due":
                        st.caption(f"**{cp_label}**: {NOT_DUE_MARKER} not due yet")
                    continue

                marker = COMPLETENESS_MARKERS.get(snap["completeness"], "?")
                collected_str = _format_time_12h(snap["collected_at"])
                age_str = format_age(snap.get("time_since_publish_seconds"))

                st.markdown(
                    f"**{snap['checkpoint']}** {marker} `{snap['completeness']}` — "
                    f"Collected: {collected_str} · Real age: {age_str}"
                )

                # Metrics table
                metric_rows = []
                for m in POST_METRICS:
                    mv = snap["metrics"].get(m, {})
                    val = format_metric_value(mv.get("value"), m, mv.get("missing_reason"))
                    reason = mv.get("missing_reason", "")
                    metric_rows.append({"Metric": m, "Value": val, "Missing reason": reason or ""})
                if metric_rows:
                    st.table(metric_rows)

        st.divider()


# ── Tab: Account ───────────────────────────────────────────────────────

def _render_account_tab():
    st.subheader("📈 Account Metrics")

    account = _cached_account_info()
    if account:
        st.caption(f"Account: **@{account['handle']}** ({account['platform']}) · Connected: {_format_time_12h(account['connected_at'])}")

    # Daily metrics table
    daily = _cached_daily_metrics()
    if daily:
        st.markdown("### Daily Metrics (reach, views, profile visits)")

        table_data = []
        chart_data = {"date": [], "reach": [], "views": [], "profile_visits": []}
        for row in daily:
            table_data.append({
                "Date": row["date"],
                "Reach": format_metric_value(row.get("reach"), "reach"),
                "Views": format_metric_value(row.get("views"), "views"),
                "Profile Visits": format_metric_value(row.get("profile_visits"), "profile_visits"),
                "Status": COMPLETENESS_MARKERS.get(row["completeness"], "?"),
            })
            chart_data["date"].append(row["date"])
            chart_data["reach"].append(row.get("reach") or 0)
            chart_data["views"].append(row.get("views") or 0)
            chart_data["profile_visits"].append(row.get("profile_visits") or 0)

        st.table(table_data)

        if len(chart_data["date"]) > 1:
            import pandas as pd
            df = pd.DataFrame(chart_data).set_index("date")
            st.line_chart(df)
    else:
        st.info("No daily account metrics collected yet.")

    # Followers over time
    st.markdown("### Followers Over Time")
    followers = _cached_followers()
    if followers:
        import pandas as pd
        follower_data = {
            "time": [_format_time_12h(r["collected_at"]) for r in followers],
            "followers": [r["followers"] for r in followers],
        }
        st.table([
            {"Collected At": _format_time_12h(r["collected_at"]), "Followers": format_metric_value(r["followers"], "followers")}
            for r in followers
        ])
        if len(followers) > 1:
            df = pd.DataFrame({
                "collected_at": [r["collected_at"] for r in followers],
                "followers": [r["followers"] for r in followers],
            }).set_index("collected_at")
            st.line_chart(df)
    else:
        st.info("No follower snapshots collected yet.")


# ── Tab: Data Health ───────────────────────────────────────────────────

def _render_health_tab():
    st.subheader("🏥 Data Health")

    # Collector runs
    st.markdown("### Collector Runs")
    log_entries = _cached_log()
    if log_entries:
        last_ok = last_successful_run(log_entries)
        if last_ok:
            ago = _time_ago(last_ok)
            minutes_since = (datetime.now(UTC) - last_ok).total_seconds() / 60
            if minutes_since > 90:
                st.error(
                    f"⚠️ Last successful collector run was **{ago}** ({_format_time_12h(last_ok)}). "
                    "The scheduler may be stopped — check Windows Task Scheduler."
                )
            else:
                st.success(f"✅ Last successful run: **{ago}** ({_format_time_12h(last_ok)})")

        table_rows = []
        for entry in reversed(log_entries):
            table_rows.append({
                "Time": _format_time_12h(entry["time"]),
                "Status": "✅ OK" if entry["status"] == "OK" else "❌ FAILED",
                "Synced": entry["synced"],
                "New Snaps": entry["snap_new"],
                "Unavail": entry["snap_unavail"],
                "Errors": entry["errors"],
                "API Calls": entry["calls"],
                "Elapsed": entry["elapsed"],
            })
        st.table(table_rows)
    else:
        st.info("No collector log found. Run `python -m insight.collect` to start collecting.")

    # Snapshot completeness
    st.markdown("### Snapshot Completeness")
    counts = _cached_completeness()
    if counts:
        cols = st.columns(len(counts))
        for i, (status, count) in enumerate(sorted(counts.items())):
            marker = COMPLETENESS_MARKERS.get(status, "?")
            cols[i].metric(f"{marker} {status}", count)
    else:
        st.info("No snapshots in database.")

    # Overdue checkpoints
    st.markdown("### Overdue Checkpoints")
    overdue = _cached_overdue()
    if overdue:
        st.warning(f"⚡ {len(overdue)} overdue checkpoint(s) found:")
        overdue_rows = []
        for o in overdue:
            overdue_rows.append({
                "Post": o["platform_post_id"][:16] + "…",
                "Caption": o["caption"] or "—",
                "Checkpoint": o["checkpoint"],
                "State": o["state"],
                "Due At": _format_time_12h(o["due_at"]),
                "Overdue By": format_age(o["lateness_seconds"]),
            })
        st.table(overdue_rows)
    else:
        st.success("✅ No overdue checkpoints.")

    # Raw responses
    st.markdown("### Raw Responses")
    raw_stats = _cached_raw_stats()
    col1, col2 = st.columns(2)
    col1.metric("Total Responses Stored", raw_stats["count"])
    col2.metric("Latest Fetch", _format_time_12h(raw_stats["latest_fetched_at"]))


# ── Main ───────────────────────────────────────────────────────────────

def main():
    st.title("📊 Insight · Metrics View")

    # Refresh button
    if st.button("🔄 Refresh", help="Clear cache and reload data"):
        st.cache_data.clear()
        # Re-create engine to get fresh connection
        st.session_state.ro_engine = make_readonly_engine(db_path=_get_db_path())
        st.rerun()

    if not _preflight():
        return

    tab_posts, tab_account, tab_health = st.tabs(["📋 Posts", "📈 Account", "🏥 Data Health"])

    with tab_posts:
        _render_posts_tab()

    with tab_account:
        _render_account_tab()

    with tab_health:
        _render_health_tab()


if __name__ == "__page__":
    main()
else:
    main()
