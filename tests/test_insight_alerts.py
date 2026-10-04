"""Tests for alerts engine: token expiry, pace flags, checkpoint results, recommendations, needs reply, and view-time system alerts.

Run: python -m unittest tests/test_insight_alerts.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from insight.alerts import (
    auto_resolve_alerts,
    evaluate_all_collector_alerts,
    evaluate_checkpoint_result_alerts,
    evaluate_needs_reply_alerts,
    evaluate_pace_flag_alerts,
    evaluate_recommendation_alerts,
    evaluate_token_expiry,
    evaluate_view_time_system_alerts,
    parse_collector_log_recent_runs,
    upsert_alert,
)
from insight.db import make_engine, session_factory, sqlite_url, upgrade_db
from insight.models import (
    Account,
    Alert,
    Comment,
    CommentLabel,
    MetricSnapshot,
    MetricValue,
    Publication,
    Recommendation,
    RecommendationSet,
)
from insight.timeutil import UTC


class AlertsEngineTests(unittest.TestCase):
    """Unit tests for the alert evaluation rules."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp_dir.name, "test_alerts.db")
        self.engine = make_engine(sqlite_url(self.db_path))
        upgrade_db(self.engine)
        self.Session = session_factory(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp_dir.cleanup()

    def test_token_expiry_missing(self):
        """When META_TOKEN_EXPIRES_AT is missing, raise warning alert."""
        with self.Session() as session:
            old_val = os.environ.pop("META_TOKEN_EXPIRES_AT", None)
            try:
                now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
                alerts = evaluate_token_expiry(session, now=now)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0].kind, "token_expiring")
                self.assertEqual(alerts[0].severity, "warning")
                self.assertIn("missing", alerts[0].dedupe_key)
            finally:
                if old_val:
                    os.environ["META_TOKEN_EXPIRES_AT"] = old_val

    def test_token_expiry_stages_and_auto_resolve(self):
        """Test token expiry stages (<14d, <3d, expired) and auto-resolve (>14d)."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

        with self.Session() as session:
            # Stage 1: 10 days left -> warning (<14d)
            os.environ["META_TOKEN_EXPIRES_AT"] = (now + timedelta(days=10)).isoformat()
            a1 = evaluate_token_expiry(session, now=now)
            session.commit()
            self.assertEqual(len(a1), 1)
            self.assertEqual(a1[0].severity, "warning")
            self.assertEqual(a1[0].dedupe_key, "token_expiring:warning_14d")

            # Running again on same stage should dedupe, not create new
            a1_repeat = evaluate_token_expiry(session, now=now)
            session.commit()
            self.assertEqual(len(a1_repeat), 1)
            self.assertEqual(a1_repeat[0].id, a1[0].id)

            # Stage 2: 2 days left -> critical (<3d)
            os.environ["META_TOKEN_EXPIRES_AT"] = (now + timedelta(days=2)).isoformat()
            a2 = evaluate_token_expiry(session, now=now)
            session.commit()
            self.assertEqual(len(a2), 1)
            self.assertEqual(a2[0].severity, "critical")
            self.assertEqual(a2[0].dedupe_key, "token_expiring:critical_3d")

            # Stage 3: Expired -> critical (expired)
            os.environ["META_TOKEN_EXPIRES_AT"] = (now - timedelta(days=1)).isoformat()
            a3 = evaluate_token_expiry(session, now=now)
            session.commit()
            self.assertEqual(len(a3), 1)
            self.assertEqual(a3[0].severity, "critical")
            self.assertEqual(a3[0].dedupe_key, "token_expiring:expired")

            # Auto-resolve: token renewed to 30 days
            os.environ["META_TOKEN_EXPIRES_AT"] = (now + timedelta(days=30)).isoformat()
            a_renewed = evaluate_token_expiry(session, now=now)
            session.commit()
            self.assertEqual(len(a_renewed), 0)

            # Verify that previous active token_expiring alerts are resolved
            resolved_alerts = session.scalars(
                select(Alert).filter(Alert.kind == "token_expiring", Alert.resolved_at.isnot(None))
            ).all()
            self.assertTrue(len(resolved_alerts) >= 1)

    def test_pace_flag_alerts_require_5_posts(self):
        """Pace alerts (taking off / slow start) require >=5 posts with usable 7d views."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

        with self.Session() as session:
            acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="test", connected_at=now)
            session.add(acct)
            session.flush()

            # Seed 3 baseline posts (fewer than 5)
            for i in range(1, 4):
                pub = Publication(id=i, platform="instagram", platform_post_id=f"P{i}", account_id=1,
                                  media_type="VIDEO", media_product_type="REELS", caption=f"Reel {i}",
                                  published_at=now - timedelta(days=i * 2))
                session.add(pub)
                session.flush()
                # 24h snapshot
                s24 = MetricSnapshot(id=i*10, subject_type="publication", subject_id=i, publication_id=i,
                                     checkpoint="24h", period_key="24h", collected_at=now - timedelta(days=i*2 - 1),
                                     time_since_publish_seconds=86400, completeness="complete")
                # 7d snapshot
                s7 = MetricSnapshot(id=i*10 + 1, subject_type="publication", subject_id=i, publication_id=i,
                                    checkpoint="7d", period_key="7d", collected_at=now,
                                    time_since_publish_seconds=7*86400, completeness="complete")
                session.add_all([s24, s7])
                session.flush()
                session.add_all([
                    MetricValue(snapshot_id=s24.id, canonical_metric="views", value=1000.0),
                    MetricValue(snapshot_id=s7.id, canonical_metric="views", value=1000.0),
                ])

            # New post with 24h snapshot (high views, pace taking off)
            p_new = Publication(id=10, platform="instagram", platform_post_id="P10", account_id=1,
                                media_type="VIDEO", media_product_type="REELS", caption="New Reel",
                                published_at=now - timedelta(hours=24))
            session.add(p_new)
            session.flush()
            s_new_24 = MetricSnapshot(id=100, subject_type="publication", subject_id=10, publication_id=10,
                                      checkpoint="24h", period_key="24h", collected_at=now,
                                      time_since_publish_seconds=86400, completeness="complete")
            session.add(s_new_24)
            session.flush()
            session.add(MetricValue(snapshot_id=s_new_24.id, canonical_metric="views", value=5000.0))
            session.commit()

            # Pace evaluation with 3 baseline posts -> should return NO alerts (tier not enough data)
            alerts = evaluate_pace_flag_alerts(session, now=now)
            self.assertEqual(len(alerts), 0)

            # Add 2 more baseline posts (reaching 5)
            for i in range(4, 6):
                pub = Publication(id=i, platform="instagram", platform_post_id=f"P{i}", account_id=1,
                                  media_type="VIDEO", media_product_type="REELS", caption=f"Reel {i}",
                                  published_at=now - timedelta(days=i * 2))
                session.add(pub)
                session.flush()
                s24 = MetricSnapshot(id=i*10, subject_type="publication", subject_id=i, publication_id=i,
                                     checkpoint="24h", period_key="24h", collected_at=now - timedelta(days=i*2 - 1),
                                     time_since_publish_seconds=86400, completeness="complete")
                s7 = MetricSnapshot(id=i*10 + 1, subject_type="publication", subject_id=i, publication_id=i,
                                    checkpoint="7d", period_key="7d", collected_at=now,
                                    time_since_publish_seconds=7*86400, completeness="complete")
                session.add_all([s24, s7])
                session.flush()
                session.add_all([
                    MetricValue(snapshot_id=s24.id, canonical_metric="views", value=1000.0),
                    MetricValue(snapshot_id=s7.id, canonical_metric="views", value=1000.0),
                ])
            session.commit()

            # Now 5 historical posts with 7d views. 24h baseline median is 1000.
            # 24h views of 5000 is 5x > 1.5x -> taking off!
            alerts = evaluate_pace_flag_alerts(session, now=now)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].kind, "reel_pace_flag")
            self.assertEqual(alerts[0].severity, "info")
            self.assertIn("taking off", alerts[0].title.lower())

    def test_checkpoint_result_alert(self):
        """When 7d checkpoint is recorded, alert is raised."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

        with self.Session() as session:
            acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="test", connected_at=now)
            session.add(acct)
            session.flush()

            pub = Publication(id=1, platform="instagram", platform_post_id="P1", account_id=1,
                              media_type="VIDEO", media_product_type="REELS", caption="Reel 1",
                              published_at=now - timedelta(days=7))
            session.add(pub)
            session.flush()

            s7 = MetricSnapshot(id=1, subject_type="publication", subject_id=1, publication_id=1,
                                checkpoint="7d", period_key="7d", collected_at=now,
                                time_since_publish_seconds=7*86400, completeness="complete")
            session.add(s7)
            session.flush()
            session.add(MetricValue(snapshot_id=1, canonical_metric="views", value=2500.0))
            session.commit()

            alerts = evaluate_checkpoint_result_alerts(session, now=now)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].kind, "checkpoint_result_in")
            self.assertEqual(alerts[0].severity, "info")
            self.assertIn("7-day result", alerts[0].title.lower())

    def test_recommendation_alerts(self):
        """Weekly recommendations ready and recommendation outcome alerts."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

        with self.Session() as session:
            acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="test", connected_at=now)
            session.add(acct)
            session.flush()

            pub = Publication(id=2, platform="instagram", platform_post_id="P2", account_id=1,
                              media_type="VIDEO", media_product_type="REELS", caption="Reel 2",
                              published_at=now - timedelta(days=5))
            session.add(pub)
            session.flush()

            rec_set = RecommendationSet(
                id=1,
                week_key="2026-W40",
                mode="evidence",
                prompt_version="v1",
                generated_at=now,
            )
            session.add(rec_set)
            session.flush()

            rec = Recommendation(
                id=1,
                set_id=1,
                rank=1,
                kind="evidence",
                topic="software_apps",
                format="how_to",
                hook="bold_claim",
                weekday="Friday",
                posting_block="Afternoon (12:00 PM – 4:59 PM)",
                confidence="high",
                facts_json="{}",
                status="followed",
                matched_publication_id=2,
                outcome_ratio=1.4,
                text="Post a how-to on software.",
                is_ai_text=False,
            )
            session.add(rec)
            session.commit()

            alerts = evaluate_recommendation_alerts(session, now=now)
            kinds = {a.kind for a in alerts}
            self.assertIn("weekly_recommendations_ready", kinds)
            self.assertIn("recommendation_outcome_recorded", kinds)

    def test_needs_reply_alert(self):
        """Viewer question or request awaiting response generates alert."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

        with self.Session() as session:
            acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="test", connected_at=now)
            session.add(acct)
            session.flush()

            pub = Publication(id=1, platform="instagram", platform_post_id="P1", account_id=1,
                              media_type="VIDEO", media_product_type="REELS", caption="Reel",
                              published_at=now - timedelta(days=1))
            session.add(pub)
            session.flush()

            comment = Comment(id=1, platform="instagram", platform_comment_id="C1", publication_id=1,
                              text="Where can I find the code?",
                              created_at=now - timedelta(hours=2), is_own_account=False)
            session.add(comment)
            session.flush()

            label = CommentLabel(id=1, comment_id=1, category="question", sentiment="neutral",
                                 needs_reply=True, needs_reply_reason="Product question", theme="Code availability",
                                 prompt_version="v1", confidence=0.95, labelled_at=now, model="gemini-2.5-flash",
                                 input_hash="hash1")
            session.add(label)
            session.commit()

            alerts = evaluate_needs_reply_alerts(session, now=now)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].kind, "needs_reply")
            self.assertEqual(alerts[0].severity, "info")


class ViewTimeSystemAlertsTests(unittest.TestCase):
    """Tests for offline / view-time system alerts computed without DB writes."""

    def test_collector_not_running_24h(self):
        """When last successful run was >=24h ago, raise collector_not_running critical alert."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

        # Never run
        a_never = evaluate_view_time_system_alerts(last_run_dt=None, now=now)
        self.assertEqual(len(a_never), 1)
        self.assertEqual(a_never[0]["kind"], "collector_not_running")
        self.assertEqual(a_never[0]["severity"], "critical")

        # Run 25 hours ago
        last_ok = now - timedelta(hours=25)
        a_late = evaluate_view_time_system_alerts(last_run_dt=last_ok, now=now)
        self.assertEqual(len(a_late), 1)
        self.assertEqual(a_late[0]["kind"], "collector_not_running")

        # Run 2 hours ago -> clear
        last_ok_recent = now - timedelta(hours=2)
        a_recent = evaluate_view_time_system_alerts(last_run_dt=last_ok_recent, now=now)
        self.assertEqual(len(a_recent), 0)

    def test_collector_failing_3_consecutive_runs(self):
        """When 3 consecutive runs in collect.log are failed, raise collector_failing critical alert."""
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
            tf.write("2026-10-04 09:00:00 [collect] OK: synced=1\n")
            tf.write("2026-10-04 10:00:00 [collect] FAILED: error 1\n")
            tf.write("2026-10-04 11:00:00 [collect] FAILED: error 2\n")
            tf.write("2026-10-04 12:00:00 [collect] FAILED: error 3\n")
            log_path = tf.name

        try:
            now = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
            last_ok = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
            alerts = evaluate_view_time_system_alerts(last_run_dt=last_ok, log_path=log_path, now=now)
            kinds = {a["kind"] for a in alerts}
            self.assertIn("collector_failing", kinds)
            fail_alert = [a for a in alerts if a["kind"] == "collector_failing"][0]
            self.assertEqual(fail_alert["severity"], "critical")
            self.assertIn("The last 3 collector runs failed", fail_alert["body"])
        finally:
            if os.path.exists(log_path):
                os.remove(log_path)


if __name__ == "__main__":
    unittest.main()
