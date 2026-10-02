"""Insight Agent logic: metric mapping, checkpoints, author hashing, fixtures, isolation.

Run from the repo root: python -m unittest discover -s tests -v
"""
import ast
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import insight
from insight import checkpoints as cp
from insight import fixtures
from insight.adapters.fake import FAKE_TOKEN, FakeAdapter
from insight.db import REAL_DB_PATH, make_engine, session_factory, sqlite_url
from insight.dictionary import MetricDictionary, load_seed
from insight.models import Comment, MetricSnapshot, MetricValue, Publication
from insight.privacy import SALT_ENV, hash_author, redact_secrets
from insight.timeutil import UTC

PUB = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.d = MetricDictionary("instagram")

    def test_full_mapping_with_scale(self):
        raw = {"views": 1000, "reach": 700, "likes": 50, "comments": 3, "shares": 4, "saved": 9,
               "total_interactions": 66, "ig_reels_avg_watch_time": 7500, "ig_reels_video_view_total_time": 7_500_000}
        m = self.d.map(raw, "reel")
        self.assertEqual(m.missing, {})
        self.assertEqual(m.unmapped, {})
        self.assertEqual(m.values["saves"], 9)
        self.assertAlmostEqual(m.values["avg_watch_time_seconds"], 7.5)
        self.assertAlmostEqual(m.values["total_watch_time_seconds"], 7500)

    def test_missing_null_and_non_numeric_are_missing_not_guessed(self):
        m = self.d.map({"views": 10, "reach": None, "likes": "n/a"}, "reel")
        self.assertEqual(m.values, {"views": 10})
        self.assertEqual(m.missing["reach"], "null in response")
        self.assertIn("non-numeric", m.missing["likes"])
        self.assertEqual(m.missing["saves"], "not in response")
        self.assertNotIn("total_interactions", m.values)  # never summed by us

    def test_unknown_platform_metric_is_unmapped(self):
        m = self.d.map({"views": 1, "plays": 5}, "reel")
        self.assertEqual(m.unmapped, {"plays": 5})
        self.assertNotIn("plays", m.values)

    def test_account_metrics(self):
        m = self.d.map({"followers_count": 100, "profile_views": 3}, "account")
        self.assertEqual(m.values, {"followers": 100, "profile_visits": 3})
        self.assertEqual(set(m.missing), {"reach", "views"})

    def test_youtube_todo_rows_are_not_used(self):
        rows = [r for r in load_seed() if r.platform == "youtube"]
        self.assertTrue(rows and all(r.status == "todo" for r in rows))
        self.assertEqual(MetricDictionary("youtube").canonical_metrics("video"), frozenset())

    def test_seed_covers_required_canonical_metrics(self):
        names = {r.canonical_name for r in load_seed() if r.platform == "instagram"}
        self.assertEqual(names, {"views", "reach", "likes", "comments", "shares", "saves", "total_interactions",
                                 "avg_watch_time_seconds", "total_watch_time_seconds", "followers", "profile_visits"})

    def test_fake_adapter_records_missing_and_strips_token(self):
        ds = fixtures.generate_dataset(n_reels=3)
        a = FakeAdapter(ds)
        a.drop_metrics = {"shares"}
        a.extra_metrics = {"brand_new_metric": 1}
        pub = SimpleNamespace(platform_post_id=ds.reels[0].post_id)
        r = a.fetch_post_metrics(pub)
        self.assertEqual(r.missing, {"shares": "not in response"})
        self.assertEqual(r.unmapped, {"brand_new_metric": 1})
        self.assertNotIn(FAKE_TOKEN, str(r.raw_payload))
        caps = a.capabilities()
        self.assertIn("avg_watch_time_seconds", caps.post_metrics)
        self.assertIn("followers", caps.account_metrics)

    def test_redact_secrets(self):
        out = redact_secrets({"access_token": "s", "x": ["https://a?b=1&access_token=s&c=2"], "n": 1})
        self.assertEqual(out, {"x": ["https://a?b=1&access_token=REDACTED&c=2"], "n": 1})


class CheckpointTests(unittest.TestCase):
    def states(self, now, collected=()):
        return {s.checkpoint: s.state for s in cp.evaluate_checkpoints(PUB, now, collected)}

    def test_grace_window(self):
        self.assertEqual(cp.grace_seconds("1h"), 15 * 60)        # 10% = 6 min -> min 15
        self.assertEqual(cp.grace_seconds("24h"), int(24 * 3600 * 0.1))

    def test_before_first_checkpoint_nothing_due(self):
        self.assertEqual(set(self.states(PUB + timedelta(minutes=30)).values()), {cp.PENDING})

    def test_due_within_grace(self):
        s = self.states(PUB + timedelta(hours=1, minutes=10))
        self.assertEqual(s["1h"], cp.DUE)
        self.assertEqual(s["24h"], cp.PENDING)

    def test_missed_but_recoverable_is_collected_late(self):
        s = self.states(PUB + timedelta(hours=30))  # 24h grace is 2.4h
        self.assertEqual(s["24h"], cp.MISSED)
        self.assertEqual(s["1h"], cp.UNRECOVERABLE)  # 24h already due

    def test_pc_off_for_three_days(self):
        now = PUB + timedelta(days=3)
        s = self.states(now, collected={"1h"})
        self.assertEqual(s, {"1h": cp.DONE, "24h": cp.UNRECOVERABLE, "48h": cp.MISSED,
                             "7d": cp.PENDING, "28d": cp.PENDING})
        due = cp.due_checkpoints(SimpleNamespace(published_at=PUB), now, {"1h"})
        self.assertEqual([d.checkpoint for d in due], ["24h", "48h"])
        self.assertEqual([d.should_collect for d in due], [False, True])

    def test_last_checkpoint_missed_is_still_collectable(self):
        s = self.states(PUB + timedelta(days=60), collected={"1h", "24h", "48h", "7d"})
        self.assertEqual(s["28d"], cp.MISSED)

    def test_all_done(self):
        s = self.states(PUB + timedelta(days=40), collected=set(cp.POST_CHECKPOINTS))
        self.assertEqual(set(s.values()), {cp.DONE})

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError):
            cp.evaluate_checkpoints(PUB, datetime(2026, 9, 2))


class HashingTests(unittest.TestCase):
    def test_deterministic_and_normalized(self):
        self.assertEqual(hash_author("@Alice ", "s"), hash_author("alice", "s"))
        self.assertEqual(len(hash_author("alice", "s")), 64)

    def test_salt_changes_hash(self):
        self.assertNotEqual(hash_author("alice", "s1"), hash_author("alice", "s2"))

    def test_hash_does_not_contain_username(self):
        self.assertNotIn("alice", hash_author("alice", "s"))

    def test_reads_salt_from_env(self):
        with mock.patch.dict(os.environ, {SALT_ENV: "envsalt"}):
            self.assertEqual(hash_author("bob"), hash_author("bob", "envsalt"))

    def test_missing_salt_raises(self):
        with mock.patch.dict(os.environ, {SALT_ENV: ""}), mock.patch("dotenv.load_dotenv"):
            with self.assertRaises(RuntimeError):
                hash_author("bob")


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.url = sqlite_url(os.path.join(cls.tmp.name, "sample.db"))
        cls.dataset = fixtures.generate_dataset()
        cls.counts = fixtures.load_sample_db(cls.url, cls.dataset)
        cls.engine = make_engine(cls.url)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.tmp.cleanup()

    def test_dataset_shape(self):
        reels = self.dataset.reels
        self.assertEqual(len(reels), 30)
        span = max(r.published_at for r in reels) - min(r.published_at for r in reels)
        self.assertGreater(span, timedelta(days=45))
        profiles = [r.profile for r in reels]
        self.assertEqual(profiles.count("viral"), 3)
        self.assertEqual(profiles.count("flop"), 2)
        normal = sorted(r.final_views for r in reels if r.profile == "normal")
        median = normal[len(normal) // 2]
        self.assertTrue(all(r.final_views > 3 * median for r in reels if r.profile == "viral"))

    def test_deterministic(self):
        again = fixtures.generate_dataset()
        self.assertEqual([r.final_views for r in again.reels], [r.final_views for r in self.dataset.reels])

    def test_loaded_counts_and_checkpoints(self):
        self.assertEqual(self.counts["publications"], 30)
        self.assertGreater(self.counts["comments"], 30)
        with session_factory(self.engine)() as s:
            for pub in s.scalars(select(Publication)):
                expected = {c for c, off in cp.POST_CHECKPOINTS.items()
                            if pub.published_at + timedelta(seconds=off) <= self.dataset.now}
                got = set(s.scalars(select(MetricSnapshot.checkpoint).filter_by(publication_id=pub.id)))
                self.assertEqual(got, expected, pub.platform_post_id)
            daily = s.scalar(select(func.count()).select_from(MetricSnapshot).filter_by(checkpoint="daily"))
            self.assertGreaterEqual(daily, 60)

    def test_special_cases_present(self):
        with session_factory(self.engine)() as s:
            states = dict(s.execute(select(MetricSnapshot.completeness, func.count())
                                    .group_by(MetricSnapshot.completeness)).all())
            self.assertEqual((states["delayed"], states["partial"], states["unavailable"]), (1, 1, 1))
            missing = s.scalars(select(MetricValue).filter(MetricValue.value.is_(None))).all()
            self.assertEqual([m.canonical_metric for m in missing], ["avg_watch_time_seconds"])

    def test_metrics_grow_over_checkpoints(self):
        with session_factory(self.engine)() as s:
            rows = s.execute(select(MetricSnapshot.publication_id, MetricSnapshot.time_since_publish_seconds,
                                    MetricValue.value)
                             .join(MetricValue).filter(MetricValue.canonical_metric == "views",
                                                       MetricSnapshot.subject_type == "publication")
                             .order_by(MetricSnapshot.publication_id, MetricSnapshot.time_since_publish_seconds)).all()
        by_pub = {}
        for pid, _, v in rows:
            by_pub.setdefault(pid, []).append(v)
        self.assertTrue(all(vals == sorted(vals) for vals in by_pub.values()))

    def test_comments_hashed_no_usernames(self):
        with session_factory(self.engine)() as s:
            comments = s.scalars(select(Comment)).all()
        self.assertTrue(all(len(c.author_hash) == 64 for c in comments))
        self.assertTrue(any(c.parent_comment_id for c in comments))
        usernames = {c.username for r in self.dataset.reels for c in r.comments}
        stored = " ".join(f"{c.text} {c.author_hash}" for c in comments)
        self.assertFalse(any(u in stored for u in usernames))

    def test_reload_is_idempotent(self):
        self.assertEqual(fixtures.load_sample_db(self.url, self.dataset), self.counts)

    def test_refuses_real_db(self):
        with self.assertRaises(RuntimeError):
            fixtures.load_sample_db(sqlite_url(REAL_DB_PATH))
        with mock.patch.dict(os.environ, {"INSIGHT_DB_URL": self.url}):
            with self.assertRaises(RuntimeError):
                fixtures.load_sample_db(self.url)


class IsolationTests(unittest.TestCase):
    FORBIDDEN = {"app", "master_loop", "agent_brain", "flow_automator", "assembly_line"}

    def test_insight_does_not_import_pipeline_modules(self):
        root = os.path.dirname(insight.__file__)
        for dirpath, _, files in os.walk(root):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    mods = []
                    if isinstance(node, ast.Import):
                        mods = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        mods = [node.module or ""]
                    for mod in mods:
                        self.assertNotIn(mod.split(".")[0], self.FORBIDDEN, f"{path} imports {mod}")


if __name__ == "__main__":
    unittest.main()
