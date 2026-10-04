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

from insight.analysis import humanize
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
    get_comparison_breakdowns,
    get_growth_overview,
    get_overdue_checkpoints,
    get_performance_overview,
    get_posts_for_analysis,
    get_posts_with_snapshots,
    get_raw_response_stats,
    get_snapshot_completeness_counts,
    get_audience_overview_query,
    get_needs_reply_queue,
    get_ranked_ideas,
    get_sentiment_breakdown,
    get_category_breakdown,
    get_spam_abuse_comments_query,
    get_avoid_notes_query,
    get_follow_through_scoreboard,
    get_post_feedback_map,
    get_recommendation_history,
    get_recommendation_overview,
    get_db_alerts,
    get_available_report_weeks_query,
    get_weekly_report_query,
    last_successful_run,
    parse_collect_log,
    _format_time_12h,
    _time_ago,
)
from insight.alerts import evaluate_view_time_system_alerts
from insight.view_state import dismiss_alert, load_view_state, mark_alerts_seen
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


@st.cache_data(ttl=60)
def _cached_performance():
    engine = _get_engine()
    if engine is None:
        return {"baseline": {}, "posts": []}
    return get_performance_overview(engine)


@st.cache_data(ttl=60)
def _cached_comparisons():
    engine = _get_engine()
    if engine is None:
        return {}
    return get_comparison_breakdowns(engine)


@st.cache_data(ttl=60)
def _cached_growth():
    engine = _get_engine()
    if engine is None:
        return {}
    return get_growth_overview(engine)


@st.cache_data(ttl=60)
def _cached_audience_overview():
    engine = _get_engine()
    if engine is None:
        return {"total_comments": 0, "total_labelled": 0, "confidence_tier": "not enough data"}
    return get_audience_overview_query(engine)


@st.cache_data(ttl=60)
def _cached_needs_reply():
    engine = _get_engine()
    if engine is None:
        return []
    return get_needs_reply_queue(engine)


@st.cache_data(ttl=60)
def _cached_ranked_ideas():
    engine = _get_engine()
    if engine is None:
        return ([], [])
    return get_ranked_ideas(engine)


@st.cache_data(ttl=60)
def _cached_sentiment():
    engine = _get_engine()
    if engine is None:
        return {"counts": {}, "percentages": {}, "total_valid": 0, "unknown_count": 0}
    return get_sentiment_breakdown(engine)


@st.cache_data(ttl=60)
def _cached_category():
    engine = _get_engine()
    if engine is None:
        return {"counts": {}, "percentages": {}, "total": 0}
    return get_category_breakdown(engine)


@st.cache_data(ttl=60)
def _cached_spam_abuse():
    engine = _get_engine()
    if engine is None:
        return []
    return get_spam_abuse_comments_query(engine)


@st.cache_data(ttl=60)
def _cached_recommendation_overview(week_key: str | None = None):
    engine = _get_engine()
    if engine is None:
        return None
    return get_recommendation_overview(engine, week_key=week_key)


@st.cache_data(ttl=60)
def _cached_avoid_notes():
    engine = _get_engine()
    if engine is None:
        return []
    return get_avoid_notes_query(engine)


@st.cache_data(ttl=60)
def _cached_follow_through_scoreboard():
    engine = _get_engine()
    if engine is None:
        return {"total_recommended": 0, "followed_count": 0, "beat_normal_count": 0, "beat_normal_rate": 0.0, "records": []}
    return get_follow_through_scoreboard(engine)


@st.cache_data(ttl=60)
def _cached_recommendation_history():
    engine = _get_engine()
    if engine is None:
        return []
    return get_recommendation_history(engine)


@st.cache_data(ttl=60)
def _cached_post_feedback_map():
    engine = _get_engine()
    if engine is None:
        return {}
    return get_post_feedback_map(engine)


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
    fb_map = _cached_post_feedback_map()
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

            # Per-post learning feedback
            fb_list = fb_map.get(post["id"], [])
            if fb_list:
                st.markdown("---")
                st.markdown("💡 **Learning Feedback**")
                for fb in fb_list:
                    badge = "🤖 AI-written" if fb["is_ai_text"] else "📝 Template"
                    st.info(f"**{fb['basis_checkpoint']} Snapshot** ({badge} · {fb['generated_at']}):\n\n{fb['text']}")

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


# ── Tab: Performance ───────────────────────────────────────────────────

def _render_performance_tab():
    st.subheader("🎯 Performance Analysis (Growth-Focused)")

    perf = _cached_performance()
    baseline = perf.get("baseline", {})
    tier = baseline.get("tier", "not enough data")
    sample_size = baseline.get("sample_size", 0)

    # 1. Confidence Tier Banner
    if tier == "not enough data":
        st.info(
            f"ℹ️ **Confidence Tier: Not Enough Data** ({sample_size}/5 posts with 7d views).\n\n"
            "Raw metrics are displayed below. Performance classifications, pace flags, and conclusions are paused until at least 5 posts reach their 7-day checkpoint."
        )
    elif tier == "early signal, low confidence":
        st.warning(
            f"⚡ **Confidence Tier: Early Signal, Low Confidence** ({sample_size} posts with 7d views).\n\n"
            "Trends are emerging but sample size is small (5–9 posts). Use with caution."
        )
    else:
        st.success(
            f"✅ **Confidence Tier: Full Confidence** ({sample_size} posts with 7d views).\n\n"
            "Robust baseline established (10+ posts)."
        )

    # 2. Baseline Summary Card
    med = baseline.get("median")
    p25 = baseline.get("p25")
    p75 = baseline.get("p75")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Median 7d Views", f"{med:,.1f}" if med is not None else "—")
    c2.metric("Typical Range (25th–75th)", f"{p25:,.0f} – {p75:,.0f}" if p25 is not None and p75 is not None else "—")
    c3.metric("Baseline Window", f"{sample_size} posts (max 20)")
    c4.metric("Confidence Tier", tier.title())

    st.divider()

    # 3. Posts Performance & Classifications
    st.markdown("### 🎬 Post Scores & Pace")
    posts = perf.get("posts", [])
    if posts:
        post_rows = []
        for p in posts:
            views_7d_str = format_metric_value(p["views_7d"], "views")
            if p.get("is_7d_delayed") and p["views_7d"] is not None:
                views_7d_str += " ⏰ (delayed)"

            erg = p.get("engagement_rate_7d")
            erg_str = f"{erg * 100:.1f}%" if erg is not None else "—"
            awt = p.get("avg_watch_time_7d")
            awt_str = f"{awt:.1f}s" if awt is not None else "—"

            cls_badge = p.get("classification") or "—"
            if cls_badge == "Above Normal":
                cls_badge = "🟢 Above Normal"
            elif cls_badge == "Below Normal":
                cls_badge = "🔴 Below Normal"
            elif cls_badge == "At Normal":
                cls_badge = "⚪ At Normal"

            pace_24 = p.get("pace_24h") or "—"
            if pace_24 == "taking off":
                pace_24 = "🚀 taking off"
            elif pace_24 == "slow start":
                pace_24 = "🐢 slow start"

            tags = p.get("tags", {})
            tag_parts = []
            for dim in ("topic", "format", "hook"):
                t = tags.get(dim)
                if t:
                    tag_parts.append(f"{dim}: {t['value']} ({int(t['confidence']*100)}%)")
            tag_label = " · ".join(tag_parts) if tag_parts else "Untagged"

            post_rows.append({
                "Published": _format_time_12h(p["published_at"]),
                "Caption": (p["caption"] or "")[:50] + ("…" if len(p.get("caption") or "") > 50 else ""),
                "7d Views (Main Score)": views_7d_str,
                "Engagement (7d)": erg_str,
                "Avg Watch (7d)": awt_str,
                "Classification": cls_badge,
                "24h Pace": pace_24,
                "AI Tags (Topic · Format · Hook)": tag_label,
            })
        st.table(post_rows)
    else:
        st.info("No posts available for analysis.")

    st.divider()

    # 4. Breakdowns & Comparisons
    st.markdown("### 📊 Performance Comparisons (7d Views)")
    st.caption("Groups with fewer than 3 posts display **too few posts** and draw no conclusions.")

    comps = _cached_comparisons()
    dim_tabs = st.tabs([
        "⏰ Posting Hour (IST)",
        "📅 Weekday",
        "📝 Caption Length",
        "🏷️ Hashtags",
        "💡 Topic (AI-tagged)",
        "📐 Format (AI-tagged)",
        "🪝 Hook (AI-tagged)",
    ])

    dim_keys = [
        ("posting_hour", "Hour Block"),
        ("weekday", "Weekday"),
        ("caption_length", "Caption Length"),
        ("hashtag_count", "Hashtag Count"),
        ("topic", "Topic"),
        ("format", "Format"),
        ("hook", "Hook"),
    ]

    for i, (key, col_name) in enumerate(dim_keys):
        with dim_tabs[i]:
            grp_data = comps.get(key, [])
            if grp_data:
                rows = []
                for g in grp_data:
                    med_val = f"{g['median_views']:,.1f}" if g.get("median_views") is not None else "—"
                    status_lbl = "⚠️ too few posts (<3)" if g.get("status") == "too few posts" else "✅ sufficient data"
                    rows.append({
                        col_name: g["group"],
                        "Posts": g["count"],
                        "Median 7d Views": med_val,
                        "Status": status_lbl,
                    })
                st.table(rows)
            else:
                st.info("No comparison data yet.")

    st.divider()

    # 5. Growth Layer
    st.markdown("### 📈 Growth Layer (Account Follower Trends)")
    growth = _cached_growth()
    g_tier = growth.get("tier", "not enough data")
    days_obs = growth.get("total_days_observed", 0)

    if g_tier == "not enough data":
        st.info(
            f"ℹ️ **Growth Confidence: Not Enough Data** ({days_obs:.1f}/14 days observed).\n\n"
            "Follower trend patterns require at least 14 days of consecutive snapshot readings to identify reliable correlations."
        )
    else:
        st.success(f"✅ **Growth Confidence: Sufficient Data** ({days_obs:.1f} days observed).")

    post_rate = growth.get("post_day_median_rate_24h")
    non_post_rate = growth.get("non_post_day_median_rate_24h")

    gc1, gc2, gc3 = st.columns(3)
    gc1.metric("Publish Days Net Follower Rate / 24h", f"{post_rate:+,.1f}" if post_rate is not None else "—")
    gc2.metric("Non-Publish Days Net Follower Rate / 24h", f"{non_post_rate:+,.1f}" if non_post_rate is not None else "—")
    gc3.metric("Follower Observation Span", f"{days_obs:.1f} days")

    st.warning("⚠️ **Notice**: All relationships between posting and follower growth represent an **observed association, not causation**.", icon="ℹ️")

    intervals = growth.get("intervals", [])
    if intervals:
        with st.expander("🔍 View Consecutive Adhoc Follower Intervals & Normalized 24h Rates"):
            int_rows = []
            for it in intervals:
                int_rows.append({
                    "Period Start": _format_time_12h(it["start"]),
                    "Period End": _format_time_12h(it["end"]),
                    "Gap Length": f"{it['hours']:.1f} hours",
                    "Net Follower Change": f"{int(it['delta']):+d}",
                    "Rate / 24h": f"{it['rate_per_24h']:+.2f}",
                    "Post-Associated Window": "Yes (Publish + Next Day)" if it["is_post_related"] else "No",
                })
            st.table(int_rows)


# ── Tab: Audience ──────────────────────────────────────────────────────

def _render_audience_tab():
    st.subheader("👥 Audience Intelligence")

    ov = _cached_audience_overview()
    needs_reply = _cached_needs_reply()
    ideas, single_mentions = _cached_ranked_ideas()
    sentiment = _cached_sentiment()
    category = _cached_category()
    spam_abuse = _cached_spam_abuse()

    # 1. Confidence banner
    if ov.get("unreadable_comments"):
        st.warning("⚠️ **Comments exist but aren't readable yet (Meta app not Live)**")
    elif ov.get("confidence_tier") == "not enough data":
        st.warning(
            f"⚠️ **Not enough data** ({ov.get('total_comments', 0)}/20 audience comments). "
            "Metrics and classifications shown below are preliminary signals."
        )
    else:
        st.success(f"✅ **Full confidence** ({ov.get('total_comments', 0)} audience comments analyzed).")

    # 2. Overview metrics
    mcol1, mcol2, mcol3, mcol4 = st.columns(4)
    with mcol1:
        if ov.get("unreadable_comments"):
            st.metric("Audience Comments", "Unreadable (app not Live)")
        else:
            st.metric("Audience Comments", ov.get("total_comments", 0))
    with mcol2:
        st.metric("AI-Classified", ov.get("total_labelled", 0))
    with mcol3:
        st.metric("Needs Reply", len(needs_reply))
    with mcol4:
        st.metric("Content Ideas", len(ideas))

    st.markdown("---")

    # 3. Needs-Reply Queue
    st.markdown("### 🚨 Needs Reply Queue")
    st.caption("Action items for community engagement. Humans reply in the Instagram app; this agent never posts or replies.")

    if not needs_reply:
        if ov.get("unreadable_comments"):
            st.info("Comments exist but aren't readable yet (Meta app not Live). Needs-reply queue is pending.")
        else:
            st.info("No comments currently waiting for a reply. All caught up!")
    else:
        rows = []
        for c in needs_reply:
            time_str = _format_time_12h(c["created_at"])
            link = f"[View Post]({c['publication_permalink']})" if c.get("publication_permalink") else "—"
            rows.append({
                "Comment": c["text"],
                "Reason": c["reason"],
                "Category": c["category"].capitalize(),
                "Sentiment": c["sentiment"].capitalize(),
                "Theme": c["theme"],
                "Time (IST)": time_str,
                "Likes": c["like_count"],
                "Post Link": link,
            })
        st.table(rows)

    st.markdown("---")

    # 4. Content Ideas
    st.markdown("### 💡 Content Ideas")
    st.caption("Themes repeated by 2 or more distinct viewers qualify as actionable content ideas, ranked by demand.")

    if not ideas:
        if ov.get("unreadable_comments"):
            st.info("Comments exist but aren't readable yet (Meta app not Live). Content ideas will appear once comments are readable.")
        else:
            st.info("No repeating content ideas yet (requires questions/requests from >=2 distinct viewers).")
    else:
        for idx, idea in enumerate(ideas, 1):
            st.markdown(f"#### #{idx} {idea['theme']}")
            st.caption(f"👥 **{idea['distinct_authors']} viewers** · 💬 {idea['comment_count']} comments · ❤️ {idea['total_likes']} likes · Latest: {_format_time_12h(idea['latest_comment_at'])}")
            if idea["samples"]:
                st.markdown("**Viewer questions/requests:**")
                for s in idea["samples"]:
                    st.markdown(f"- *\"{s}\"*")
            st.markdown("")

    if single_mentions:
        with st.expander(f"🔍 Single Mentions ({len(single_mentions)} emerging topics from 1 viewer)"):
            sm_rows = []
            for sm in single_mentions:
                sm_rows.append({
                    "Theme": sm["theme"],
                    "Comments": sm["comment_count"],
                    "Likes": sm["total_likes"],
                    "Sample": sm["samples"][0] if sm["samples"] else "—",
                })
            st.table(sm_rows)

    st.markdown("---")

    # 5. Sentiment and Category Breakdown
    st.markdown("### 📊 Audience Sentiment & Categories")
    bcol1, bcol2 = st.columns(2)

    with bcol1:
        st.markdown("#### Sentiment Distribution")
        st.caption("Excludes 'unknown' and own-account comments.")
        if sentiment.get("total_valid", 0) == 0:
            st.info("No sentiment data available yet.")
        else:
            s_rows = []
            for s_name in ("positive", "neutral", "negative"):
                cnt = sentiment.get("counts", {}).get(s_name, 0)
                pct = sentiment.get("percentages", {}).get(s_name, 0.0)
                s_rows.append({
                    "Sentiment": s_name.capitalize(),
                    "Count": cnt,
                    "Share": f"{pct:.1f}%",
                })
            st.table(s_rows)
            if sentiment.get("unknown_count", 0) > 0:
                st.caption(f"*Note: {sentiment['unknown_count']} comment(s) had unclassified/unknown sentiment.*")

    with bcol2:
        st.markdown("#### Category Breakdown")
        st.caption("Distribution across the 8 taxonomy categories.")
        if category.get("total", 0) == 0:
            st.info("No category data available yet.")
        else:
            c_rows = []
            for cat, cnt in sorted(category.get("counts", {}).items(), key=lambda x: x[1], reverse=True):
                pct = category.get("percentages", {}).get(cat, 0.0)
                c_rows.append({
                    "Category": cat.capitalize(),
                    "Count": cnt,
                    "Share": f"{pct:.1f}%",
                })
            st.table(c_rows)

    # 6. Filtered Spam & Abuse
    if spam_abuse:
        with st.expander(f"🛡️ Filtered Spam & Abuse ({len(spam_abuse)} comments)"):
            sa_rows = []
            for sa in spam_abuse:
                sa_rows.append({
                    "Text": sa["text"],
                    "Flag": sa["category"].upper(),
                    "Confidence": f"{sa['confidence']:.2f}",
                    "Time (IST)": _format_time_12h(sa["created_at"]),
                })
            st.table(sa_rows)


# ── Tab: Recommendations ───────────────────────────────────────────────

def _render_recommendations_tab():
    st.subheader("💡 What to Post Next & Learning Feedback")

    overview = _cached_recommendation_overview()
    avoid_notes = _cached_avoid_notes()
    scoreboard = _cached_follow_through_scoreboard()
    history = _cached_recommendation_history()

    if not overview:
        st.info("No recommendations generated yet. Run the collector or CLI to generate recommendations for this week.")
        return

    mode = overview["mode"]
    week_key = overview["week_key"]
    gen_time = overview["generated_at"]

    # 1. Mode Banner
    if mode == "exploration":
        st.warning(
            f"🧭 **Exploration Mode** ({week_key} · Generated {gen_time})\n\n"
            "Account has fewer than 10 posts with 7-day data. Suggestions test varied, untried or least-tried combinations. "
            "Labelled **exploration, not evidence**."
        )
    else:
        st.success(
            f"🎯 **Evidence Mode** ({week_key} · Generated {gen_time})\n\n"
            "Account has 10+ posts with 7-day data. Suggestions prioritize proven combinations with >=1.2x baseline (and >=3 posts), "
            "with top audience ideas ranked first."
        )

    st.markdown("### 📋 Recommended Actions for this Week")
    recs = overview.get("recommendations", [])
    if not recs:
        st.info("No recommendations found in this week's set.")
    else:
        for r in recs:
            rank = r["rank"]
            kind = r["kind"].upper().replace("_", " ")
            topic = r["topic"]
            fmt = r["format"]
            hook = r["hook"]
            timing = f"{r['weekday']} · {r['posting_block']}"
            conf = r["confidence"]
            status = r["status"].upper()
            is_ai = r["is_ai_text"]
            badge = "🤖 AI-written" if is_ai else "📝 Template"

            status_icon = "🟢" if r["status"] == "followed" else ("⚪" if r["status"] == "open" else "⚫")

            with st.container():
                c1, c2 = st.columns([4, 1])
                c1.markdown(f"#### #{rank} [{kind}] {humanize(topic)} · {humanize(fmt)}")
                c2.caption(f"{status_icon} **{status}** · {badge}")
                st.markdown(f"**Action**: {r['text']}")
                st.caption(f"🎯 **Hook**: {humanize(hook)} · ⏰ **Target Schedule**: {timing} · 📊 **Confidence**: {conf}")

                # If followed, show outcome ratio
                if r["status"] == "followed":
                    ratio = r.get("outcome_ratio")
                    ratio_str = f"{ratio:.1f}x baseline" if ratio is not None else "⏳ 7d checkpoint pending"
                    st.success(f"✅ Followed by publication #{r.get('matched_publication_id')} · 7d Result: **{ratio_str}**")

                facts = r.get("facts", {})
                if facts:
                    with st.expander(f"🔍 Underlying Facts (Rank #{rank})"):
                        st.json(facts)
                st.divider()

    # 2. Avoid notes in evidence mode
    if mode == "evidence" and avoid_notes:
        st.markdown("### ⚠️ Avoid Notes")
        st.caption("Groups with at least 3 posts performing <= 0.8x baseline (underperforming):")
        for an in avoid_notes:
            st.warning(f"• **{an['dimension']}**: `{an['value']}` (median {an['ratio']:.1f}x baseline across {an['post_count']} posts)")
        st.divider()

    # 3. Follow-Through Scoreboard
    st.markdown("### 🎯 Follow-Through Scoreboard")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Recommended", scoreboard["total_recommended"])
    m2.metric("Followed Posts", scoreboard["followed_count"])
    m3.metric("Beat-Normal Count", scoreboard["beat_normal_count"])
    m4.metric("Beat-Normal Rate", f"{scoreboard['beat_normal_rate']}%")

    records = scoreboard.get("records", [])
    if records:
        table_rows = []
        for rec in records:
            ratio_val = rec.get("outcome_ratio")
            ratio_str = f"{ratio_val:.1f}x normal" if ratio_val is not None else ("⏳ pending 7d" if rec["status"] == "followed" else "—")
            table_rows.append({
                "Week": rec["week_key"],
                "Rank": f"#{rec['rank']}",
                "Kind": rec["kind"],
                "Topic · Format": f"{rec['topic']} · {rec['format']}",
                "Status": rec["status"].upper(),
                "Outcome": ratio_str,
                "Wording": "AI" if rec["is_ai_text"] else "Template",
            })
        st.table(table_rows)

    st.divider()

    # 4. Past Weeks History
    if history:
        with st.expander("📚 Past Weeks Recommendation Sets"):
            hist_rows = []
            for h in history:
                hist_rows.append({
                    "Week": h["week_key"],
                    "Mode": h["mode"].title(),
                    "Generated (IST)": h["generated_at"],
                    "Suggestions": h["item_count"],
                })
            st.table(hist_rows)


# ── Alerts Bar (Rendered Above Tabs) ──────────────────────────────────

def _render_alerts_bar():
    engine = _get_engine()
    if engine is None:
        return

    view_state = load_view_state()
    dismissed_keys = set(view_state.get("dismissed_dedupe_keys", []))
    last_seen_str = view_state.get("last_seen_at")

    # Fetch DB alerts
    db_alerts = get_db_alerts(engine)

    # Compute view-time system alerts
    log_entries = _cached_log()
    last_ok = last_successful_run(log_entries) if log_entries else None
    system_alerts = evaluate_view_time_system_alerts(last_ok)

    all_alerts = system_alerts + db_alerts

    # Active alerts = not resolved in DB and not dismissed in view_state
    active_alerts = [
        a for a in all_alerts
        if not a.get("is_resolved", False) and a["dedupe_key"] not in dismissed_keys
    ]

    # Calculate unread count (alerts created after last_seen_at)
    unread_count = 0
    if last_seen_str:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen_str)
            for a in active_alerts:
                c_at = a.get("created_at")
                if c_at:
                    if c_at.tzinfo is None:
                        c_at = c_at.replace(tzinfo=UTC)
                    if last_seen_dt.tzinfo is None:
                        last_seen_dt = last_seen_dt.replace(tzinfo=UTC)
                    if c_at > last_seen_dt:
                        unread_count += 1
        except Exception:
            unread_count = len(active_alerts)
    else:
        unread_count = len(active_alerts)

    if active_alerts:
        st.markdown(f"### 🔔 Alerts ({len(active_alerts)} active · {unread_count} unread)")
        for a in active_alerts:
            sev = a.get("severity", "info").lower()
            key = a["dedupe_key"]
            title = a["title"]
            body = a["body"]

            col_alert, col_btn = st.columns([5, 1])
            with col_alert:
                if sev == "critical":
                    st.error(f"🚨 **[{sev.upper()}] {title}**\n\n{body}")
                elif sev == "warning":
                    st.warning(f"⚠️ **[{sev.upper()}] {title}**\n\n{body}")
                else:
                    st.info(f"ℹ️ **[{sev.upper()}] {title}**\n\n{body}")

            with col_btn:
                if st.button("✖ Dismiss", key=f"dismiss_btn_{key}"):
                    dismiss_alert(key)
                    st.rerun()
    else:
        st.caption("🔔 **Alerts**: All clear — no active alerts.")

    # History Expander
    dismissed_or_resolved = [
        a for a in all_alerts
        if a.get("is_resolved", False) or a["dedupe_key"] in dismissed_keys
    ]
    if dismissed_or_resolved:
        with st.expander(f"📜 Alert History ({len(dismissed_or_resolved)} dismissed / resolved)"):
            hist_rows = []
            for h in dismissed_or_resolved:
                status = "✅ Resolved" if h.get("is_resolved") else "👁️ Dismissed"
                time_val = h.get("created_at_ist") or _format_time_12h(h.get("created_at"))
                hist_rows.append({
                    "Time (IST)": time_val,
                    "Severity": h.get("severity", "").upper(),
                    "Title": h.get("title", ""),
                    "Status": status,
                    "Details": h.get("body", "")[:80] + ("…" if len(h.get("body", "")) > 80 else ""),
                })
            st.table(hist_rows)

    mark_alerts_seen()
    st.divider()


# ── Tab: Reports ───────────────────────────────────────────────────────

def _render_reports_tab():
    st.subheader("📑 Weekly Executive Reports")
    engine = _get_engine()
    if engine is None:
        st.info("No database available.")
        return

    weeks = get_available_report_weeks_query(engine)
    if not weeks:
        st.info("No weekly report data found.")
        return

    selected_week = st.selectbox("📅 Select Week (IST Monday–Sunday)", weeks, index=0)

    report = get_weekly_report_query(engine, selected_week)
    data = report["data"]
    summary = report["summary_text"]
    is_ai = report["is_ai_summary"]

    # Download buttons
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        st.download_button(
            label="📥 Download Report (.md)",
            data=report["markdown"],
            file_name=f"weekly_report_{selected_week}.md",
            mime="text/markdown",
            key=f"dl_md_{selected_week}",
        )
    with dcol2:
        st.download_button(
            label="🌐 Download Standalone Report (.html)",
            data=report["html"],
            file_name=f"weekly_report_{selected_week}.html",
            mime="text/html",
            key=f"dl_html_{selected_week}",
        )

    st.markdown("---")

    # Header and Status
    is_current = data.get("is_current_week", False)
    status_label = "🟡 In Progress (week ends Sunday 11:59 PM IST)" if is_current else "🟢 Closed Week"
    st.markdown(f"### Report for Week **{selected_week}** · `{status_label}`")
    st.caption(f"**Period (IST)**: {data['week_start_ist']} to {data['week_end_ist']}")

    # Executive Summary Card
    badge = "🤖 AI-Generated" if is_ai else "📝 Deterministic Template"
    st.info(f"**Executive Summary** ({badge}):\n\n{summary}")

    st.markdown("---")

    # Section 1: Publishing Activity
    st.markdown("### 🎬 1. Publishing Activity")
    col1, col2, col3 = st.columns(3)
    col1.metric("Reels Published", data["published_count"])
    col2.metric("Total Views", f"{data['total_views']:,}")
    col3.metric("Baseline Median Views", f"{data['baseline_views']:,}")

    if data["publications"]:
        p_rows = []
        for p in data["publications"]:
            p_rows.append({
                "ID": p["id"],
                "Published (IST)": p["published_at_ist"],
                "Topic": p["topic"],
                "Format": p["format"],
                "Hook": p["hook"],
                "Views": f"{p['views']:,}",
            })
        st.table(p_rows)
    else:
        st.info("No posts were published during this week.")

    st.markdown("---")

    # Section 2: Results vs Normal
    st.markdown("### 📊 2. Results vs Normal")
    tier = data.get("confidence_tier", "not enough data")
    if tier == "not enough data":
        st.warning(
            f"⚠️ **Baseline Confidence: Not Enough Data** ({data['usable_7d_posts']}/5 posts with 7d views). "
            "Comparisons against baseline are preliminary."
        )
    else:
        st.success(f"✅ **Baseline Confidence: {tier.title()}** ({data['usable_7d_posts']} posts with 7d views).")

    st.markdown("---")

    # Section 3: Follower Growth
    st.markdown("### 📈 3. Follower Growth")
    st.caption("Readings are **approximate (readings taken at irregular times)**.")
    s = data.get("follower_start")
    e = data.get("follower_end")
    if s and e:
        fg1, fg2, fg3 = st.columns(3)
        fg1.metric(f"Start: {s['time_ist']}", f"{s['count']:,} followers")
        fg2.metric(f"End: {e['time_ist']}", f"{e['count']:,} followers")
        delta = data.get("follower_delta", 0)
        fg3.metric("Net Change", f"{delta:+d}")
    else:
        st.info("No follower snapshots recorded for this period.")

    st.markdown("---")

    # Section 4: Recommendations Followed & Outcomes
    st.markdown("### 🎯 4. Recommendations Followed + Outcomes")
    followed = data.get("followed_recommendations", [])
    if followed:
        f_rows = []
        for f in followed:
            ratio = f.get("outcome_ratio")
            r_str = f"{ratio:.1f}x baseline" if ratio is not None else "⏳ 7d checkpoint pending"
            f_rows.append({
                "Rank": f"#{f['rank']}",
                "Topic": f["topic"],
                "Format": f["format"],
                "Followed by Reel": f"#{f['matched_pub_id']}",
                "7d Result": r_str,
            })
        st.table(f_rows)
    else:
        st.info("No recommendations were followed by publications during this week.")

    st.markdown("---")

    # Section 5: Audience Highlights
    st.markdown("### 👥 5. Audience Highlights")
    if data.get("unreadable_comments_flag"):
        st.warning("⚠️ **Comments exist but aren't readable yet (Meta app not Live)**")
    elif data.get("collected_comments_count", 0) == 0:
        st.info("No comments were collected during this week.")
    else:
        st.success(f"💬 {data['collected_comments_count']} comments received and analyzed this week.")

    st.markdown("---")

    # Section 6: Data Health & Next Week's Plan
    st.markdown("### 🏥 6. Data Health & Next Week's Plan")
    dh = data.get("data_health", {})
    dh1, dh2, dh3, dh4 = st.columns(4)
    dh1.metric("Total Snapshots", dh.get("total_snapshots", 0))
    dh2.metric("Complete", dh.get("complete", 0))
    dh3.metric("Delayed", dh.get("delayed", 0))
    dh4.metric("Unavailable", dh.get("unavailable", 0))

    st.markdown(f"#### 🧭 Next Week's Plan ({data.get('next_week_key')})")
    next_plan = data.get("next_plan", [])
    if next_plan:
        np_rows = []
        for np in next_plan:
            np_rows.append({
                "Rank": f"#{np['rank']}",
                "Topic": np["topic"],
                "Format": np["format"],
                "Hook": np["hook"],
                "Schedule": f"{np['weekday']} · {np['posting_block']}",
                "Action": np["action"],
            })
        st.table(np_rows)
    else:
        st.info(f"No recommendation set generated yet for {data.get('next_week_key')}.")


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

    # Render alerts bar at top of all tabs
    _render_alerts_bar()

    tab_posts, tab_account, tab_perf, tab_aud, tab_recs, tab_reports, tab_health = st.tabs([
        "📋 Posts", "📈 Account", "🎯 Performance", "👥 Audience", "💡 Recommendations", "📑 Reports", "🏥 Data Health"
    ])

    with tab_posts:
        _render_posts_tab()

    with tab_account:
        _render_account_tab()

    with tab_perf:
        _render_performance_tab()

    with tab_aud:
        _render_audience_tab()

    with tab_recs:
        _render_recommendations_tab()

    with tab_reports:
        _render_reports_tab()

    with tab_health:
        _render_health_tab()


if __name__ == "__page__":
    main()
else:
    main()


