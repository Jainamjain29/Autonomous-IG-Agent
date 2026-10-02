"""Tests for insight collector, InstagramAdapter, and Alembic migrations.

All tests use mocked HTTP and temporary databases. No real API calls.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, inspect, select

from insight.adapters.instagram import InstagramAdapter, is_reel
from insight.checkpoints import POST_CHECKPOINTS
from insight.collect import ACCOUNT_INSIGHTS_MAX_LOOKBACK_DAYS, Collector, acquire_lock
from insight.db import make_engine, sqlite_url, upgrade_db
from insight.http_client import GraphClient
from insight.models import Account, Base, MetricSnapshot, MetricValue, Publication
from insight.timeutil import UTC


def _mock_response(status=200, data=None, headers=None):
    r = mock.Mock(status_code=status)
    d = data if data is not None else {"data": []}
    r.json.return_value = d
    r.text = json.dumps(d)
    r.headers = headers or {}
    return r


class AlembicTests(unittest.TestCase):
    def test_alembic_upgrade_fresh_db(self):
        """upgrade_db() creates all 7 models tables + alembic_version on a blank database."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "fresh.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            tables = set(inspect(engine).get_table_names())
            expected = {
                "accounts",
                "publications",
                "metric_definitions",
                "raw_responses",
                "metric_snapshots",
                "metric_values",
                "comments",
                "alembic_version",
            }
            self.assertEqual(tables, expected)
            engine.dispose()

    def test_autogenerate_produces_no_changes(self):
        """Autogenerate against models after 'upgrade head' produces no schema diffs."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "autogen.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                diff = compare_metadata(ctx, Base.metadata)
                self.assertEqual(diff, [], f"Unexpected schema diff: {diff}")
            engine.dispose()


class MediaTypeMappingTests(unittest.TestCase):
    def test_media_type_mapping_with_probe_shape(self):
        """Test is_reel with exact payload shapes returned by the Instagram probe."""
        # Probe reel 1
        reel_1 = {"media_type": "VIDEO", "media_product_type": "REELS"}
        self.assertTrue(is_reel(reel_1["media_type"], reel_1["media_product_type"]))

        # Feed image
        image_post = {"media_type": "IMAGE", "media_product_type": "FEED"}
        self.assertFalse(is_reel(image_post["media_type"], image_post["media_product_type"]))

        # Story video
        story_post = {"media_type": "VIDEO", "media_product_type": "STORY"}
        self.assertFalse(is_reel(story_post["media_type"], story_post["media_product_type"]))

        # Legacy / fake adapter fixture format
        self.assertTrue(is_reel("reel", None))
        self.assertFalse(is_reel("image", None))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = GraphClient("https://graph.instagram.com/v26.0", "token123", call_cap=50)
        self.adapter = InstagramAdapter(self.client, "user_123")

    @mock.patch("insight.http_client.requests.get")
    def test_pagination_follows_next(self, mock_get):
        page1 = {
            "data": [
                {
                    "id": "post_1",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": "2026-10-02T10:00:00+0000",
                    "caption": "Post 1",
                    "permalink": "https://ig.me/1",
                }
            ],
            "paging": {"next": "https://graph.instagram.com/v26.0/user_123/media?after=curs1"},
        }
        page2 = {
            "data": [
                {
                    "id": "post_2",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": "2026-10-01T10:00:00+0000",
                    "caption": "Post 2",
                    "permalink": "https://ig.me/2",
                }
            ],
            "paging": {},
        }
        mock_get.side_effect = [_mock_response(200, page1), _mock_response(200, page2)]

        pubs = self.adapter.list_publications()
        self.assertEqual(len(pubs), 2)
        self.assertEqual(pubs[0].platform_post_id, "post_1")
        self.assertEqual(pubs[0].media_product_type, "REELS")
        self.assertEqual(pubs[1].platform_post_id, "post_2")
        self.assertEqual(mock_get.call_count, 2)

    @mock.patch("insight.http_client.requests.get")
    def test_fetch_account_metrics_excludes_followers(self, mock_get):
        """Daily account metrics return day-bounded metrics and omit followers."""
        mock_get.return_value = _mock_response(
            200,
            {
                "data": [
                    {"name": "profile_views", "total_value": {"value": 15}},
                    {"name": "reach", "total_value": {"value": 85}},
                    {"name": "views", "total_value": {"value": 120}},
                ]
            },
        )
        target = date(2026, 10, 1)
        res = self.adapter.fetch_account_metrics(target)
        self.assertIn("profile_visits", res.values)
        self.assertIn("reach", res.values)
        self.assertIn("views", res.values)
        self.assertNotIn("followers", res.values)
        self.assertNotIn("followers", res.missing)


class CollectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test_collector.db")
        self.engine = make_engine(sqlite_url(self.db_path))
        upgrade_db(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def _settings(self, key):
        s = {
            "GRAPH_HOST": "graph.instagram.com",
            "GRAPH_VERSION": "v26.0",
            "META_ACCESS_TOKEN": "mock_token",
            "IG_USER_ID": "17841440",
        }
        return s.get(key)

    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    def test_checkpoint_and_catchup_collection(self, mock_get, mock_db_setting):
        mock_db_setting.side_effect = self._settings

        now_fixed = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)

        # Post published 3 hours ago: 1h is missed (delayed), 24h is pending
        post_time = (now_fixed - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        media_page = {
            "data": [
                {
                    "id": "reel_101",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": post_time,
                    "caption": "Reel 101",
                    "permalink": "https://ig.me/r101",
                }
            ],
            "paging": {},
        }
        account_profile = {"id": "17841440", "username": "streamovate", "followers_count": 50}
        post_insights = {
            "data": [
                {"name": "views", "values": [{"value": 100}]},
                {"name": "reach", "values": [{"value": 80}]},
                {"name": "likes", "values": [{"value": 10}]},
                {"name": "comments", "values": [{"value": 2}]},
                {"name": "shares", "values": [{"value": 1}]},
                {"name": "saved", "values": [{"value": 3}]},
                {"name": "total_interactions", "values": [{"value": 16}]},
                {"name": "ig_reels_avg_watch_time", "values": [{"value": 5000}]},
                {"name": "ig_reels_video_view_total_time", "values": [{"value": 500000}]},
            ]
        }
        account_insights = {
            "data": [
                {"name": "profile_views", "total_value": {"value": 5}},
                {"name": "reach", "total_value": {"value": 30}},
                {"name": "views", "total_value": {"value": 75}},
            ]
        }

        # Sequence:
        # 1. account_profile
        # 2. list_publications
        # 3. post_insights (for adhoc backfill)
        # 4. post_insights (for 1h delayed)
        # 5. account_insights (yesterday daily)
        # 6. followers (adhoc)
        mock_get.side_effect = [
            _mock_response(200, account_profile),
            _mock_response(200, media_page),
            _mock_response(200, post_insights),
            _mock_response(200, post_insights),
            _mock_response(200, account_insights),
            _mock_response(200, account_profile),
        ]

        with mock.patch("insight.collect.datetime") as mock_dt:
            mock_dt.now.return_value = now_fixed
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            collector = Collector(dry_run=False, call_cap=50, engine=self.engine)
            stats = collector.collect()

        self.assertEqual(stats["posts_synced"], 1)
        self.assertGreaterEqual(stats["snap_new"], 3)

        # Inspect database
        with self.engine.connect() as conn:
            snaps = conn.execute(select(MetricSnapshot)).fetchall()
            checkpoints = {s.checkpoint for s in snaps}
            self.assertIn("1h", checkpoints)
            self.assertIn("daily", checkpoints)
            self.assertIn("adhoc", checkpoints)

            # Check post 1h snapshot is delayed
            snap_1h = [s for s in snaps if s.checkpoint == "1h"][0]
            self.assertEqual(snap_1h.completeness, "delayed")

            # Check daily snapshot contains no followers
            daily_snap = [s for s in snaps if s.checkpoint == "daily"][0]
            daily_vals = conn.execute(
                select(MetricValue).filter_by(snapshot_id=daily_snap.id)
            ).fetchall()
            daily_metric_names = {v.canonical_metric for v in daily_vals}
            self.assertIn("reach", daily_metric_names)
            self.assertIn("views", daily_metric_names)
            self.assertIn("profile_visits", daily_metric_names)
            self.assertNotIn("followers", daily_metric_names)

            # Check adhoc account snapshot contains followers
            adhoc_snap = [s for s in snaps if s.checkpoint == "adhoc" and s.subject_type == "account"][0]
            adhoc_vals = conn.execute(
                select(MetricValue).filter_by(snapshot_id=adhoc_snap.id)
            ).fetchall()
            adhoc_metric_names = {v.canonical_metric for v in adhoc_vals}
            self.assertIn("followers", adhoc_metric_names)

    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    def test_catchup_days_never_get_followers(self, mock_get, mock_db_setting):
        """When multiple missed daily snapshots are caught up, NONE receive followers."""
        mock_db_setting.side_effect = self._settings
        now_fixed = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)

        account_profile = {"id": "17841440", "username": "streamovate", "followers_count": 99}
        account_insights = {
            "data": [
                {"name": "profile_views", "total_value": {"value": 5}},
                {"name": "reach", "total_value": {"value": 30}},
                {"name": "views", "total_value": {"value": 75}},
            ]
        }

        # Seed an existing daily snapshot for 2026-10-01 (leaving 10-02, 10-03, 10-04 to be caught up)
        with self.engine.begin() as conn:
            conn.execute(
                Account.__table__.insert().values(
                    id=1, platform="instagram", platform_account_id="17841440",
                    handle="streamovate", connected_at=now_fixed
                )
            )
            conn.execute(
                MetricSnapshot.__table__.insert().values(
                    subject_type="account", subject_id=1, account_id=1,
                    checkpoint="daily", period_key="2026-10-01",
                    collected_at=now_fixed - timedelta(days=4), completeness="complete"
                )
            )

        # Expected calls:
        # 1. account_profile
        # 2. list_publications (empty)
        # 3. account_insights for 2026-10-02
        # 4. account_insights for 2026-10-03
        # 5. account_insights for 2026-10-04
        # 6. account_profile (followers adhoc)
        mock_get.side_effect = [
            _mock_response(200, account_profile),
            _mock_response(200, {"data": []}),
            _mock_response(200, account_insights),
            _mock_response(200, account_insights),
            _mock_response(200, account_insights),
            _mock_response(200, account_profile),
        ]

        with mock.patch("insight.collect.datetime") as mock_dt:
            mock_dt.now.return_value = now_fixed
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            c = Collector(dry_run=False, call_cap=50, engine=self.engine)
            stats = c.collect()

        self.assertEqual(stats["snap_new"], 4)  # 3 catch-up daily + 1 adhoc followers

        with self.engine.connect() as conn:
            daily_snaps = conn.execute(
                select(MetricSnapshot).filter_by(subject_type="account", checkpoint="daily")
            ).fetchall()
            self.assertEqual(len(daily_snaps), 4)  # 1 seeded + 3 caught up

            for snap in daily_snaps:
                vals = conn.execute(
                    select(MetricValue).filter_by(snapshot_id=snap.id)
                ).fetchall()
                names = {v.canonical_metric for v in vals}
                self.assertNotIn("followers", names, f"Snapshot {snap.period_key} unexpectedly has followers!")

    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    def test_idempotency_second_run_writes_nothing(self, mock_get, mock_db_setting):
        mock_db_setting.side_effect = self._settings
        now_fixed = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)

        account_profile = {"id": "17841440", "username": "streamovate", "followers_count": 50}
        post_time = (now_fixed - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        media_page = {
            "data": [
                {
                    "id": "reel_202",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": post_time,
                    "caption": "Reel 202",
                }
            ],
            "paging": {},
        }
        account_insights = {
            "data": [
                {"name": "profile_views", "total_value": {"value": 5}},
                {"name": "reach", "total_value": {"value": 30}},
                {"name": "views", "total_value": {"value": 75}},
            ]
        }

        # First run responses:
        # 1. account_profile
        # 2. media_page (reel_202 age 45m: <1h, no checkpoints due yet)
        # 3. account_insights (yesterday)
        # 4. account_profile (followers)
        # Second run responses:
        # 5. account_profile
        # 6. media_page
        mock_get.side_effect = [
            _mock_response(200, account_profile),
            _mock_response(200, media_page),
            _mock_response(200, account_insights),
            _mock_response(200, account_profile),
            # Second run responses
            _mock_response(200, account_profile),
            _mock_response(200, media_page),
        ]

        with mock.patch("insight.collect.datetime") as mock_dt:
            mock_dt.now.return_value = now_fixed
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            c1 = Collector(dry_run=False, call_cap=50, engine=self.engine)
            s1 = c1.collect()
            self.assertGreater(s1["snap_new"], 0)

            c2 = Collector(dry_run=False, call_cap=50, engine=self.engine)
            s2 = c2.collect()
            self.assertEqual(s2["snap_new"], 0)
            self.assertEqual(s2["snap_unavail"], 0)

    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    def test_dry_run_writes_nothing(self, mock_get, mock_db_setting):
        mock_db_setting.side_effect = self._settings
        account_profile = {"id": "17841440", "username": "streamovate", "followers_count": 50}
        account_insights = {
            "data": [
                {"name": "profile_views", "total_value": {"value": 5}},
                {"name": "reach", "total_value": {"value": 30}},
                {"name": "views", "total_value": {"value": 75}},
            ]
        }
        mock_get.side_effect = [
            _mock_response(200, account_profile),
            _mock_response(200, {"data": []}),
            _mock_response(200, account_insights),
            _mock_response(200, account_profile),
        ]
        collector = Collector(dry_run=True, call_cap=50, engine=self.engine)
        collector.collect()

        with self.engine.connect() as conn:
            pub_count = conn.execute(select(func.count(Publication.id))).scalar()
            snap_count = conn.execute(select(func.count(MetricSnapshot.id))).scalar()
            self.assertEqual(pub_count, 0)
            self.assertEqual(snap_count, 0)

    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    def test_usage_header_stops_early(self, mock_get, mock_db_setting):
        mock_db_setting.side_effect = self._settings
        account_profile = {"id": "17841440", "username": "streamovate", "followers_count": 50}
        throttled_headers = {"X-App-Usage": json.dumps({"call_count": 85, "total_time": 85})}

        mock_get.side_effect = [
            _mock_response(200, account_profile, headers=throttled_headers),
            _mock_response(200, {"data": []}),
        ]
        collector = Collector(dry_run=False, call_cap=50, engine=self.engine)
        stats = collector.collect()
        self.assertTrue(collector.client.usage_throttled)
        self.assertEqual(collector.client.max_usage_observed, 85.0)

    def test_lock_file_and_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "test.lock")
            with acquire_lock(lock_path):
                self.assertTrue(os.path.exists(lock_path))
            self.assertFalse(os.path.exists(lock_path))

            # Test stale lock is removed
            with open(lock_path, "w") as f:
                f.write("old_pid")
            old_time = time.time() - 30 * 60
            os.utime(lock_path, (old_time, old_time))

            with acquire_lock(lock_path):
                self.assertTrue(os.path.exists(lock_path))
            self.assertFalse(os.path.exists(lock_path))

    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    def test_first_run_old_post_respects_call_cap_and_resumes(self, mock_get, mock_db_setting):
        """Account with a 2-year-old post catches up daily snapshots within call cap, then resumes on next run."""
        mock_db_setting.side_effect = self._settings
        now_fixed = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
        days_old = 700
        old_pub_time = now_fixed - timedelta(days=days_old)

        account_profile = {"id": "17841440", "username": "streamovate", "followers_count": 50}
        media_page = {
            "data": [
                {
                    "id": "reel_old",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": old_pub_time.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                    "caption": "Old Reel",
                }
            ],
            "paging": {},
        }
        post_insights = {
            "data": [
                {"name": "views", "values": [{"value": 100}]},
                {"name": "reach", "values": [{"value": 80}]},
                {"name": "likes", "values": [{"value": 10}]},
                {"name": "comments", "values": [{"value": 2}]},
                {"name": "shares", "values": [{"value": 1}]},
                {"name": "saved", "values": [{"value": 3}]},
                {"name": "total_interactions", "values": [{"value": 16}]},
                {"name": "ig_reels_avg_watch_time", "values": [{"value": 5000}]},
                {"name": "ig_reels_video_view_total_time", "values": [{"value": 500000}]},
            ]
        }
        account_insights = {
            "data": [
                {"name": "profile_views", "total_value": {"value": 5}},
                {"name": "reach", "total_value": {"value": 30}},
                {"name": "views", "total_value": {"value": 75}},
            ]
        }

        # Seed account with connected_at 700 days ago
        with self.engine.begin() as conn:
            conn.execute(
                Account.__table__.insert().values(
                    id=1,
                    platform="instagram",
                    platform_account_id="17841440",
                    handle="streamovate",
                    connected_at=old_pub_time,
                )
            )

        # Call cap = 10
        # Run 1 call breakdown (10 calls total):
        # 1: account_profile
        # 2: list_publications
        # 3: post_insights (adhoc backfill for reel_old)
        # 4-9: 6 daily snapshots (calls_remaining = 7, 1 reserved for followers -> budget = 6)
        # 10: account_profile (adhoc followers)
        run1_responses = [
            _mock_response(200, account_profile),
            _mock_response(200, media_page),
            _mock_response(200, post_insights),
        ] + [_mock_response(200, account_insights) for _ in range(6)] + [
            _mock_response(200, account_profile),
        ]

        # Run 2 call breakdown (10 calls total):
        # 1: account_profile
        # 2: list_publications (reel_old already backfilled)
        # 3-10: 8 daily snapshots (followers already collected today -> budget = 8)
        run2_responses = [
            _mock_response(200, account_profile),
            _mock_response(200, media_page),
        ] + [_mock_response(200, account_insights) for _ in range(8)]

        mock_get.side_effect = run1_responses + run2_responses

        with mock.patch("insight.collect.datetime") as mock_dt:
            mock_dt.now.return_value = now_fixed
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            # --- RUN 1 ---
            c1 = Collector(dry_run=False, call_cap=10, engine=self.engine)
            s1 = c1.collect()

            # Does not exceed call cap
            self.assertEqual(s1["calls"], 10)
            self.assertLessEqual(s1["calls"], c1.call_cap)
            self.assertEqual(s1["errors"], 0)

            # 6 daily snapshots saved
            with self.engine.connect() as conn:
                r1_dailies = conn.execute(
                    select(MetricSnapshot).filter_by(
                        subject_type="account", checkpoint="daily"
                    ).order_by(MetricSnapshot.period_key.asc())
                ).fetchall()
                self.assertEqual(len(r1_dailies), 6)
                expected_r1_dates = [
                    (old_pub_time.date() + timedelta(days=i)).isoformat()
                    for i in range(6)
                ]
                self.assertEqual([d.period_key for d in r1_dailies], expected_r1_dates)

            # --- RUN 2 (resumes next run) ---
            c2 = Collector(dry_run=False, call_cap=10, engine=self.engine)
            s2 = c2.collect()

            # Does not exceed call cap
            self.assertEqual(s2["calls"], 10)
            self.assertLessEqual(s2["calls"], c2.call_cap)
            self.assertEqual(s2["errors"], 0)

            # Total daily snapshots is now 6 + 8 = 14, completely contiguous
            with self.engine.connect() as conn:
                r2_dailies = conn.execute(
                    select(MetricSnapshot).filter_by(
                        subject_type="account", checkpoint="daily"
                    ).order_by(MetricSnapshot.period_key.asc())
                ).fetchall()
                self.assertEqual(len(r2_dailies), 14)
                expected_all_dates = [
                    (old_pub_time.date() + timedelta(days=i)).isoformat()
                    for i in range(14)
                ]
                self.assertEqual([d.period_key for d in r2_dailies], expected_all_dates)

    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    def test_daily_account_catchup_rule_bounds(self, mock_get, mock_db_setting):
        """Verify start_bound is later of connected_at, earliest_pub, and today - 729."""
        mock_db_setting.side_effect = self._settings
        now_fixed = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)

        account_profile = {"id": "17841440", "username": "streamovate", "followers_count": 50}
        post_insights = {
            "data": [
                {"name": "views", "values": [{"value": 100}]},
                {"name": "reach", "values": [{"value": 80}]},
                {"name": "likes", "values": [{"value": 10}]},
                {"name": "comments", "values": [{"value": 2}]},
                {"name": "shares", "values": [{"value": 1}]},
                {"name": "saved", "values": [{"value": 3}]},
                {"name": "total_interactions", "values": [{"value": 16}]},
                {"name": "ig_reels_avg_watch_time", "values": [{"value": 5000}]},
                {"name": "ig_reels_video_view_total_time", "values": [{"value": 500000}]},
            ]
        }
        media_page = {
            "data": [
                {
                    "id": "reel_very_old",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "timestamp": (now_fixed - timedelta(days=1000)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                    "caption": "Very Old Reel",
                }
            ],
            "paging": {},
        }
        account_insights = {
            "data": [
                {"name": "profile_views", "total_value": {"value": 5}},
                {"name": "reach", "total_value": {"value": 30}},
                {"name": "views", "total_value": {"value": 75}},
            ]
        }

        # Case: post is 1000 days ago (> 729 limit), connected_at 1000 days ago.
        # Start bound must be today - 729 days = 2024-10-03 (exactly 729 days back).
        with self.engine.begin() as conn:
            conn.execute(
                Account.__table__.insert().values(
                    id=1,
                    platform="instagram",
                    platform_account_id="17841440",
                    handle="streamovate",
                    connected_at=now_fixed - timedelta(days=1000),
                )
            )

        # Call breakdown for call_cap=5:
        # 1: account_profile
        # 2: list_publications
        # 3: post_insights (adhoc backfill)
        # 4: account_insights (1 daily snapshot for 729 days back)
        # 5: account_profile (adhoc followers)
        mock_get.side_effect = [
            _mock_response(200, account_profile),
            _mock_response(200, media_page),
            _mock_response(200, post_insights),
            _mock_response(200, account_insights),
            _mock_response(200, account_profile),
        ]

        with mock.patch("insight.collect.datetime") as mock_dt:
            mock_dt.now.return_value = now_fixed
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            c = Collector(dry_run=False, call_cap=5, engine=self.engine)
            s = c.collect()

            self.assertEqual(s["calls"], 5)
            with self.engine.connect() as conn:
                daily_snap = conn.execute(
                    select(MetricSnapshot).filter_by(subject_type="account", checkpoint="daily")
                ).fetchone()
                expected_earliest = (now_fixed.date() - timedelta(days=729)).isoformat()
                self.assertEqual(daily_snap.period_key, expected_earliest)


if __name__ == "__main__":
    unittest.main()
