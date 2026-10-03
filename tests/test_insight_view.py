"""Tests for insight queries, read-only enforcement, formatting, and log parsing.

All tests use temp DBs and temp log files. No Streamlit import needed.
Run from the repo root: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from insight.checkpoints import POST_CHECKPOINTS, evaluate_checkpoints
from insight.db import (
    SQLITE_BUSY_TIMEOUT_MS,
    init_db,
    make_engine,
    make_readonly_engine,
    session_factory,
    sqlite_url,
)
from insight.models import (
    Account,
    MetricSnapshot,
    MetricValue,
    Publication,
    RawResponse,
)
from insight.queries import (
    ALEMBIC_HEAD,
    COMPLETENESS_MARKERS,
    NOT_DUE_MARKER,
    check_alembic_version,
    check_has_publications,
    format_age,
    format_metric_value,
    get_daily_account_metrics,
    get_followers_over_time,
    get_overdue_checkpoints,
    get_posts_with_snapshots,
    get_raw_response_stats,
    get_snapshot_completeness_counts,
    last_successful_run,
    parse_collect_log,
    _format_time_12h,
)
from insight.timeutil import UTC

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def _seed_db(engine):
    """Insert a small test dataset: 1 account, 2 publications, several snapshots."""
    with session_factory(engine)() as session:
        acct = Account(id=1, platform="instagram", platform_account_id="A1",
                       handle="testuser", connected_at=T0)
        session.add(acct)
        session.flush()

        pub1 = Publication(id=1, platform="instagram", platform_post_id="P1",
                           account_id=1, media_type="VIDEO", media_product_type="REELS",
                           caption="First reel about tech #hashtag", permalink="https://ig.me/1",
                           published_at=T0)
        pub2 = Publication(id=2, platform="instagram", platform_post_id="P2",
                           account_id=1, media_type="VIDEO", media_product_type="REELS",
                           caption="Second reel with a very long caption that should be truncated at sixty characters definitely",
                           published_at=T0 - timedelta(days=3))
        session.add_all([pub1, pub2])
        session.flush()

        # 1h snapshot for pub1 (complete)
        snap1 = MetricSnapshot(id=1, subject_type="publication", subject_id=1,
                               publication_id=1, checkpoint="1h", period_key="1h",
                               collected_at=T0 + timedelta(hours=1),
                               time_since_publish_seconds=3600, completeness="complete")
        session.add(snap1)
        session.flush()
        session.add_all([
            MetricValue(snapshot_id=1, canonical_metric="views", value=100.0),
            MetricValue(snapshot_id=1, canonical_metric="reach", value=80.0),
            MetricValue(snapshot_id=1, canonical_metric="likes", value=10.0),
            MetricValue(snapshot_id=1, canonical_metric="comments", value=2.0),
            MetricValue(snapshot_id=1, canonical_metric="shares", value=1.0),
            MetricValue(snapshot_id=1, canonical_metric="saves", value=5.0),
            MetricValue(snapshot_id=1, canonical_metric="avg_watch_time_seconds", value=7.5),
            MetricValue(snapshot_id=1, canonical_metric="total_watch_time_seconds", value=7500.0),
        ])

        # 24h snapshot for pub1 (delayed)
        snap2 = MetricSnapshot(id=2, subject_type="publication", subject_id=1,
                               publication_id=1, checkpoint="24h", period_key="24h",
                               collected_at=T0 + timedelta(hours=30),
                               time_since_publish_seconds=108000, completeness="delayed")
        session.add(snap2)
        session.flush()
        session.add_all([
            MetricValue(snapshot_id=2, canonical_metric="views", value=500.0),
            MetricValue(snapshot_id=2, canonical_metric="reach", value=400.0),
            MetricValue(snapshot_id=2, canonical_metric="likes", value=50.0),
            MetricValue(snapshot_id=2, canonical_metric="comments", value=5.0),
            MetricValue(snapshot_id=2, canonical_metric="shares", value=3.0),
            MetricValue(snapshot_id=2, canonical_metric="saves", value=20.0),
            MetricValue(snapshot_id=2, canonical_metric="avg_watch_time_seconds", value=8.2),
            MetricValue(snapshot_id=2, canonical_metric="total_watch_time_seconds", value=12000.0),
        ])

        # 1h snapshot for pub2 (unavailable)
        snap3 = MetricSnapshot(id=3, subject_type="publication", subject_id=2,
                               publication_id=2, checkpoint="1h", period_key="1h",
                               collected_at=T0, time_since_publish_seconds=259200,
                               completeness="unavailable")
        session.add(snap3)
        session.flush()
        # No values for unavailable snapshot

        # Adhoc snapshot for pub2
        snap4 = MetricSnapshot(id=4, subject_type="publication", subject_id=2,
                               publication_id=2, checkpoint="adhoc",
                               period_key=(T0).isoformat(),
                               collected_at=T0, time_since_publish_seconds=259200,
                               completeness="complete")
        session.add(snap4)
        session.flush()
        session.add_all([
            MetricValue(snapshot_id=4, canonical_metric="views", value=2000.0),
            MetricValue(snapshot_id=4, canonical_metric="reach", value=1500.0),
            MetricValue(snapshot_id=4, canonical_metric="likes", value=200.0),
            MetricValue(snapshot_id=4, canonical_metric="avg_watch_time_seconds", value=None,
                        missing_reason="not in response"),
        ])

        # Daily account snapshot
        snap5 = MetricSnapshot(id=5, subject_type="account", subject_id=1,
                               account_id=1, checkpoint="daily",
                               period_key="2026-10-01", collected_at=T0 + timedelta(hours=1),
                               completeness="complete")
        session.add(snap5)
        session.flush()
        session.add_all([
            MetricValue(snapshot_id=5, canonical_metric="reach", value=50.0),
            MetricValue(snapshot_id=5, canonical_metric="views", value=200.0),
            MetricValue(snapshot_id=5, canonical_metric="profile_visits", value=10.0),
        ])

        # Adhoc account snapshot (followers)
        snap6 = MetricSnapshot(id=6, subject_type="account", subject_id=1,
                               account_id=1, checkpoint="adhoc",
                               period_key=T0.isoformat(), collected_at=T0,
                               completeness="complete")
        session.add(snap6)
        session.flush()
        session.add(MetricValue(snapshot_id=6, canonical_metric="followers", value=999.0))

        # Raw response
        raw = RawResponse(id=1, platform="instagram", endpoint="/me/insights",
                          fetched_at=T0, payload={"data": []})
        session.add(raw)
        session.commit()


class QueryFunctionTests(unittest.TestCase):
    """Test that query functions return correct shapes and values."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "test.db")
        cls.engine = init_db(make_engine(sqlite_url(cls.db_path)))
        _seed_db(cls.engine)
        cls.ro_engine = make_readonly_engine(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.ro_engine.dispose()
        cls.engine.dispose()
        cls.tmp.cleanup()

    def test_get_posts_returns_correct_count(self):
        posts = get_posts_with_snapshots(self.ro_engine)
        self.assertEqual(len(posts), 2)

    def test_posts_sorted_newest_first(self):
        posts = get_posts_with_snapshots(self.ro_engine)
        self.assertEqual(posts[0]["platform_post_id"], "P1")
        self.assertEqual(posts[1]["platform_post_id"], "P2")

    def test_post_snapshots_have_metrics(self):
        posts = get_posts_with_snapshots(self.ro_engine)
        p1 = posts[0]
        self.assertIn("1h", p1["snapshots"])
        self.assertIn("24h", p1["snapshots"])
        self.assertEqual(p1["snapshots"]["1h"]["metrics"]["views"]["value"], 100.0)
        self.assertEqual(p1["snapshots"]["24h"]["completeness"], "delayed")

    def test_unavailable_snapshot_has_no_metrics(self):
        posts = get_posts_with_snapshots(self.ro_engine)
        p2 = posts[1]
        self.assertIn("1h", p2["snapshots"])
        self.assertEqual(p2["snapshots"]["1h"]["completeness"], "unavailable")
        self.assertEqual(p2["snapshots"]["1h"]["metrics"], {})

    def test_adhoc_snapshot_present(self):
        posts = get_posts_with_snapshots(self.ro_engine)
        p2 = posts[1]
        self.assertIn("adhoc", p2["snapshots"])
        self.assertEqual(p2["snapshots"]["adhoc"]["metrics"]["views"]["value"], 2000.0)

    def test_daily_account_metrics(self):
        daily = get_daily_account_metrics(self.ro_engine)
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["date"], "2026-10-01")
        self.assertEqual(daily[0]["reach"], 50.0)
        self.assertEqual(daily[0]["views"], 200.0)
        self.assertEqual(daily[0]["profile_visits"], 10.0)

    def test_daily_metrics_never_contain_followers(self):
        daily = get_daily_account_metrics(self.ro_engine)
        for row in daily:
            self.assertNotIn("followers", row)

    def test_followers_over_time(self):
        followers = get_followers_over_time(self.ro_engine)
        self.assertEqual(len(followers), 1)
        self.assertEqual(followers[0]["followers"], 999.0)

    def test_snapshot_completeness_counts(self):
        counts = get_snapshot_completeness_counts(self.ro_engine)
        self.assertEqual(counts["complete"], 4)
        self.assertEqual(counts["delayed"], 1)
        self.assertEqual(counts["unavailable"], 1)

    def test_raw_response_stats(self):
        stats = get_raw_response_stats(self.ro_engine)
        self.assertEqual(stats["count"], 1)
        self.assertIsNotNone(stats["latest_fetched_at"])

    def test_account_info(self):
        from insight.queries import get_account_info
        info = get_account_info(self.ro_engine)
        self.assertEqual(info["handle"], "testuser")
        self.assertEqual(info["platform"], "instagram")


class ReadOnlyEnforcementTests(unittest.TestCase):
    """Read-only engine must reject writes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "readonly_test.db")
        engine = init_db(make_engine(sqlite_url(cls.db_path)))
        engine.dispose()
        cls.ro_engine = make_readonly_engine(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.ro_engine.dispose()
        cls.tmp.cleanup()

    def test_write_attempt_fails_on_readonly(self):
        with self.assertRaises(Exception) as ctx:
            with self.ro_engine.connect() as conn:
                conn.execute(text("INSERT INTO accounts (platform, platform_account_id, handle, connected_at) VALUES ('ig', 'X', 'x', '2026-01-01')"))
                conn.commit()
        # SQLite read-only mode raises OperationalError
        self.assertTrue("readonly" in str(ctx.exception).lower() or
                        "attempt to write" in str(ctx.exception).lower())

    def test_missing_db_returns_none(self):
        engine = make_readonly_engine("/nonexistent/path/no.db")
        self.assertIsNone(engine)


class EmptyDBTests(unittest.TestCase):
    """Handle empty or missing DB gracefully."""

    def test_empty_db_no_publications(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "empty.db")
            engine = init_db(make_engine(sqlite_url(db_path)))
            engine.dispose()
            ro = make_readonly_engine(db_path)
            self.assertFalse(check_has_publications(ro))
            posts = get_posts_with_snapshots(ro)
            self.assertEqual(posts, [])
            ro.dispose()

    def test_check_db_exists_false(self):
        from insight.queries import check_db_exists
        self.assertFalse(check_db_exists("/nonexistent/insight.db"))


class AlembicVersionTests(unittest.TestCase):
    """Test alembic version checking."""

    def test_alembic_at_head(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "versioned.db")
            engine = make_engine(sqlite_url(db_path))
            from insight.db import upgrade_db
            upgrade_db(engine)
            ro = make_readonly_engine(db_path)
            at_head, version = check_alembic_version(ro)
            self.assertTrue(at_head)
            self.assertEqual(version, ALEMBIC_HEAD)
            ro.dispose()
            engine.dispose()

    def test_alembic_not_at_head(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "old.db")
            engine = make_engine(sqlite_url(db_path))
            from insight.db import upgrade_db
            upgrade_db(engine)
            # Tamper with alembic_version
            with engine.connect() as conn:
                conn.execute(text("UPDATE alembic_version SET version_num = 'old_version'"))
                conn.commit()
            ro = make_readonly_engine(db_path)
            at_head, version = check_alembic_version(ro)
            self.assertFalse(at_head)
            self.assertEqual(version, "old_version")
            ro.dispose()
            engine.dispose()

    def test_no_alembic_table(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "noalembic.db")
            engine = init_db(make_engine(sqlite_url(db_path)))
            # init_db does create_all but no alembic_version table
            ro = make_readonly_engine(db_path)
            at_head, version = check_alembic_version(ro)
            self.assertFalse(at_head)
            self.assertIsNone(version)
            ro.dispose()
            engine.dispose()


class FormattingTests(unittest.TestCase):
    """Test IST + 12-hour formatting, age formatting, metric display."""

    def test_utc_to_ist_12h(self):
        # 10:00 UTC = 3:30 PM IST
        dt = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
        result = _format_time_12h(dt)
        self.assertIn("3:30 PM", result)
        self.assertIn("Oct 01", result)

    def test_midnight_utc_formats_as_530am_ist(self):
        dt = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
        result = _format_time_12h(dt)
        self.assertIn("5:30 AM", result)

    def test_none_formats_as_dash(self):
        self.assertEqual(_format_time_12h(None), "—")

    def test_format_age_seconds(self):
        self.assertEqual(format_age(30), "30s")
        self.assertEqual(format_age(90), "1m")
        self.assertEqual(format_age(3660), "1h 1m")
        self.assertEqual(format_age(90000), "1d 1h")
        self.assertEqual(format_age(None), "—")

    def test_missing_metric_value_shows_dash(self):
        self.assertEqual(format_metric_value(None, "views"), "—")

    def test_watch_time_has_seconds_suffix(self):
        result = format_metric_value(7.5, "avg_watch_time_seconds")
        self.assertEqual(result, "7.5s")
        result2 = format_metric_value(7500.0, "total_watch_time_seconds")
        self.assertEqual(result2, "7500.0s")

    def test_integer_values_formatted_without_decimal(self):
        self.assertEqual(format_metric_value(100.0, "views"), "100")
        self.assertEqual(format_metric_value(1234.0, "likes"), "1,234")

    def test_completeness_markers_defined(self):
        self.assertEqual(COMPLETENESS_MARKERS["complete"], "✅")
        self.assertEqual(COMPLETENESS_MARKERS["delayed"], "⏰")
        self.assertEqual(COMPLETENESS_MARKERS["unavailable"], "❌")
        self.assertEqual(NOT_DUE_MARKER, "⏳")


class LogParsingTests(unittest.TestCase):
    """Test log summary parsing with real log line format."""

    # These are exact copies of real data/logs/collect.log lines (no tokens)
    REAL_LOG_LINES = [
        "[2026-10-02 23:44:29,490] INFO: [collect] OK: synced=2 snap_new=0 snap_unavail=0 errors=0 calls=2/200 elapsed=1.09s\n",
        "[2026-10-03 01:00:59,510] INFO: [collect] OK: synced=2 snap_new=0 snap_unavail=0 errors=0 calls=2/200 elapsed=1.12s\n",
        "[2026-10-03 21:08:37,968] INFO: [collect] OK: synced=2 snap_new=3 snap_unavail=0 errors=0 calls=5/200 elapsed=3.72s\n",
    ]

    def test_parse_real_log_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
            f.writelines(self.REAL_LOG_LINES)
            f.flush()
            path = f.name

        try:
            entries = parse_collect_log(path)
            self.assertEqual(len(entries), 3)

            # First entry
            self.assertEqual(entries[0]["status"], "OK")
            self.assertEqual(entries[0]["synced"], 2)
            self.assertEqual(entries[0]["snap_new"], 0)
            self.assertEqual(entries[0]["snap_unavail"], 0)
            self.assertEqual(entries[0]["errors"], 0)
            self.assertEqual(entries[0]["calls"], "2/200")
            self.assertEqual(entries[0]["elapsed"], "1.09s")

            # Third entry has snap_new=3
            self.assertEqual(entries[2]["snap_new"], 3)
            self.assertEqual(entries[2]["calls"], "5/200")

            # Timestamps are UTC-aware
            self.assertIsNotNone(entries[0]["time"].tzinfo)
            self.assertEqual(entries[0]["time"].year, 2026)
            self.assertEqual(entries[0]["time"].month, 10)
            self.assertEqual(entries[0]["time"].day, 2)
            self.assertEqual(entries[0]["time"].hour, 23)
            self.assertEqual(entries[0]["time"].minute, 44)
        finally:
            os.unlink(path)

    def test_parse_error_lines(self):
        lines = [
            "[2026-10-02 23:44:29,490] INFO: [collect] OK: synced=2 snap_new=0 snap_unavail=0 errors=0 calls=2/200 elapsed=1.09s\n",
            "[2026-10-03 01:05:00,000] ERROR: [collect] FATAL: Missing META_ACCESS_TOKEN\n",
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            path = f.name
        try:
            entries = parse_collect_log(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[1]["status"], "FAILED")
            self.assertEqual(entries[1]["errors"], 1)
        finally:
            os.unlink(path)

    def test_last_20_entries_returned(self):
        lines = [
            f"[2026-10-0{1 + h // 24} {h % 24:02d}:00:00,000] INFO: [collect] OK: synced=1 snap_new=0 snap_unavail=0 errors=0 calls=1/200 elapsed=0.5s\n"
            for h in range(25)
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            path = f.name
        try:
            entries = parse_collect_log(path)
            self.assertEqual(len(entries), 20)
        finally:
            os.unlink(path)

    def test_missing_log_returns_empty(self):
        entries = parse_collect_log("/nonexistent/collect.log")
        self.assertEqual(entries, [])

    def test_last_successful_run(self):
        entries = [
            {"time": datetime(2026, 10, 1, 10, 0, tzinfo=UTC), "status": "OK"},
            {"time": datetime(2026, 10, 1, 11, 0, tzinfo=UTC), "status": "FAILED"},
        ]
        result = last_successful_run(entries)
        self.assertEqual(result, datetime(2026, 10, 1, 10, 0, tzinfo=UTC))

    def test_last_successful_run_none(self):
        entries = [{"time": datetime(2026, 10, 1, 11, 0, tzinfo=UTC), "status": "FAILED"}]
        self.assertIsNone(last_successful_run(entries))


class OverdueCheckpointTests(unittest.TestCase):
    """Test overdue checkpoint detection using the existing checkpoint helpers."""

    def test_overdue_detection(self):
        """A post older than 1h with no 1h snapshot → overdue."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "overdue.db")
            engine = init_db(make_engine(sqlite_url(db_path)))
            now = datetime.now(UTC)
            # Insert a post published 3 hours ago, no snapshots
            with session_factory(engine)() as session:
                acct = Account(platform="instagram", platform_account_id="A1",
                               handle="test", connected_at=now)
                session.add(acct)
                session.flush()
                pub = Publication(platform="instagram", platform_post_id="X1",
                                  account_id=acct.id, media_type="VIDEO",
                                  media_product_type="REELS",
                                  published_at=now - timedelta(hours=3))
                session.add(pub)
                session.commit()
            ro = make_readonly_engine(db_path)
            overdue = get_overdue_checkpoints(ro)
            # 1h should be overdue (past grace)
            checkpoints_found = {o["checkpoint"] for o in overdue}
            self.assertIn("1h", checkpoints_found)
            ro.dispose()
            engine.dispose()

    def test_no_overdue_when_all_collected(self):
        """Post with all reachable checkpoints collected → no overdue."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "allgood.db")
            engine = init_db(make_engine(sqlite_url(db_path)))
            now = datetime.now(UTC)
            with session_factory(engine)() as session:
                acct = Account(platform="instagram", platform_account_id="A1",
                               handle="test", connected_at=now)
                session.add(acct)
                session.flush()
                pub = Publication(platform="instagram", platform_post_id="X1",
                                  account_id=acct.id, media_type="VIDEO",
                                  published_at=now - timedelta(hours=2))
                session.add(pub)
                session.flush()
                # Add 1h checkpoint
                snap = MetricSnapshot(subject_type="publication", subject_id=pub.id,
                                      publication_id=pub.id, checkpoint="1h",
                                      period_key="1h",
                                      collected_at=now - timedelta(hours=1),
                                      time_since_publish_seconds=3600,
                                      completeness="complete")
                session.add(snap)
                session.commit()
            ro = make_readonly_engine(db_path)
            overdue = get_overdue_checkpoints(ro)
            # 1h is collected, 24h is not due yet → no overdue
            self.assertEqual(overdue, [])
            ro.dispose()
            engine.dispose()


class ConcurrencyTests(unittest.TestCase):
    """Test that the busy timeout works: a read-only connection open during a write
    does not make the write fail."""

    def test_readonly_during_write_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "concurrent.db")
            write_engine = init_db(make_engine(sqlite_url(db_path)))
            ro_engine = make_readonly_engine(db_path)

            # Open a long-lived read-only connection
            ro_conn = ro_engine.connect()
            ro_result = ro_conn.execute(text("SELECT COUNT(*) FROM publications"))
            _ = ro_result.scalar()  # Force query execution

            # Write while the read connection is open
            write_error = []
            def _do_write():
                try:
                    with write_engine.connect() as wc:
                        wc.execute(text(
                            "INSERT INTO accounts (platform, platform_account_id, handle, connected_at) "
                            "VALUES ('ig', 'W1', 'writer', '2026-01-01 00:00:00')"
                        ))
                        wc.commit()
                except Exception as e:
                    write_error.append(e)

            writer = threading.Thread(target=_do_write)
            writer.start()
            writer.join(timeout=15)  # Should finish well within the 10s busy timeout

            ro_conn.close()

            # Verify: no error writing, and the row is there
            self.assertEqual(write_error, [], f"Write failed: {write_error}")
            with write_engine.connect() as conn:
                count = conn.execute(text("SELECT COUNT(*) FROM accounts WHERE handle='writer'")).scalar()
                self.assertEqual(count, 1)

            ro_engine.dispose()
            write_engine.dispose()

    def test_busy_timeout_is_set(self):
        """Verify the busy_timeout PRAGMA is actually applied."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "timeout.db")
            engine = init_db(make_engine(sqlite_url(db_path)))
            with engine.connect() as conn:
                timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
                self.assertEqual(timeout, SQLITE_BUSY_TIMEOUT_MS)
            engine.dispose()


class DryRunAndDisplayTests(unittest.TestCase):
    """Verify display-specific formatting rules."""

    def test_checkpoint_order(self):
        from insight.queries import CHECKPOINT_ORDER
        self.assertEqual(CHECKPOINT_ORDER, ["1h", "24h", "48h", "7d", "28d"])

    def test_post_metrics_list(self):
        from insight.queries import POST_METRICS
        self.assertIn("views", POST_METRICS)
        self.assertIn("avg_watch_time_seconds", POST_METRICS)
        self.assertEqual(len(POST_METRICS), 8)

    def test_format_metric_zero_is_not_dash(self):
        """Zero is a valid value, should show '0' not '—'."""
        self.assertEqual(format_metric_value(0.0, "likes"), "0")
        self.assertEqual(format_metric_value(0.0, "avg_watch_time_seconds"), "0.0s")


class StreamlitRenderTests(unittest.TestCase):
    """Test that view.py renders without exception using Streamlit AppTest."""

    def test_render_with_fixture_data(self):
        """Render against a temp DB with fixture data: no exception, renders tabs."""
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "render_test.db")
            engine = make_engine(sqlite_url(db_path))
            from insight.db import upgrade_db
            upgrade_db(engine)
            _seed_db(engine)
            engine.dispose()

            old_env = os.environ.get("INSIGHT_VIEW_DB_PATH")
            at = None
            try:
                os.environ["INSIGHT_VIEW_DB_PATH"] = db_path
                at = AppTest.from_file("insight/view.py")
                at.run(timeout=30)
                self.assertEqual(len(at.exception), 0, f"AppTest raised exceptions: {at.exception}")
                tab_labels = [tab.label for tab in at.tabs]
                self.assertIn("📋 Posts", tab_labels)
                self.assertIn("📈 Account", tab_labels)
                self.assertIn("🎯 Performance", tab_labels)
                self.assertIn("👥 Audience", tab_labels)
                self.assertIn("💡 Recommendations", tab_labels)
                self.assertIn("🏥 Data Health", tab_labels)
            finally:
                if at is not None:
                    try:
                        ro = at.session_state.get("ro_engine")
                        if ro is not None:
                            ro.dispose()
                    except Exception:
                        pass
                if old_env is not None:
                    os.environ["INSIGHT_VIEW_DB_PATH"] = old_env
                else:
                    os.environ.pop("INSIGHT_VIEW_DB_PATH", None)

    def test_render_with_missing_db(self):
        """Render against a non-existent DB path: does not crash, shows 'No data yet'."""
        from streamlit.testing.v1 import AppTest

        old_env = os.environ.get("INSIGHT_VIEW_DB_PATH")
        try:
            os.environ["INSIGHT_VIEW_DB_PATH"] = "/nonexistent/test/insight.db"
            at = AppTest.from_file("insight/view.py")
            at.run(timeout=30)
            self.assertEqual(len(at.exception), 0, f"AppTest crashed on missing DB: {at.exception}")
            warning_texts = [w.value for w in at.warning]
            self.assertTrue(
                any("No data yet" in w for w in warning_texts),
                f"Expected 'No data yet' warning, got: {warning_texts}",
            )
        finally:
            if old_env is not None:
                os.environ["INSIGHT_VIEW_DB_PATH"] = old_env
            else:
                os.environ.pop("INSIGHT_VIEW_DB_PATH", None)


if __name__ == "__main__":
    unittest.main()
