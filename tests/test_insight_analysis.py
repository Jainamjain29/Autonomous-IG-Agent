"""Tests for insight performance analysis, AI tagging, and Streamlit Performance tab.

All tests use mocked Gemini and temporary databases. No real external API calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, inspect, select, text

from insight import analysis
from insight.db import make_engine, make_readonly_engine, session_factory, sqlite_url, upgrade_db
from insight.models import Account, Base, MetricSnapshot, MetricValue, PostTag, Publication
from insight.tagging import (
    DEFAULT_MODEL_NAME,
    DEFAULT_PROMPT_VERSION,
    classify_caption,
    hash_input,
    load_taxonomy,
    tag_publication,
    tag_untagged_publications,
)
from insight.timeutil import UTC

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


class AnalysisMathTests(unittest.TestCase):
    def test_percentile_and_median(self):
        data = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(analysis.median(data), 30.0)
        self.assertEqual(analysis.percentile(data, 0.25), 20.0)
        self.assertEqual(analysis.percentile(data, 0.75), 40.0)
        self.assertIsNone(analysis.percentile([], 0.5))
        self.assertEqual(analysis.percentile([100.0], 0.5), 100.0)

    def test_confidence_tiers(self):
        self.assertEqual(analysis.get_confidence_tier(0), "not enough data")
        self.assertEqual(analysis.get_confidence_tier(4), "not enough data")
        self.assertEqual(analysis.get_confidence_tier(5), "early signal, low confidence")
        self.assertEqual(analysis.get_confidence_tier(9), "early signal, low confidence")
        self.assertEqual(analysis.get_confidence_tier(10), "full")
        self.assertEqual(analysis.get_confidence_tier(25), "full")

    def test_classify_performance(self):
        p25, p75 = 20.0, 40.0
        self.assertEqual(analysis.classify_performance(50.0, p25, p75), "Above Normal")
        self.assertEqual(analysis.classify_performance(30.0, p25, p75), "At Normal")
        self.assertEqual(analysis.classify_performance(10.0, p25, p75), "Below Normal")
        self.assertEqual(analysis.classify_performance(20.0, p25, p75), "At Normal")
        self.assertEqual(analysis.classify_performance(40.0, p25, p75), "At Normal")

    def test_compute_baseline(self):
        scores = [100.0 * i for i in range(1, 15)]  # 14 posts
        res = analysis.compute_baseline(scores)
        self.assertEqual(res["sample_size"], 14)
        self.assertEqual(res["tier"], "full")
        self.assertIsNotNone(res["median"])
        self.assertIsNotNone(res["p25"])
        self.assertIsNotNone(res["p75"])

        # Under 5 posts
        res_small = analysis.compute_baseline([10.0, 20.0, 30.0])
        self.assertEqual(res_small["tier"], "not enough data")

    def test_early_pace_flags(self):
        history = [100.0, 100.0, 100.0, 100.0, 100.0]  # median = 100
        # Under 5 posts history returns None
        self.assertIsNone(analysis.compute_early_pace_flags(250.0, [100.0, 100.0]))
        # > 2.0x median -> taking off
        self.assertEqual(analysis.compute_early_pace_flags(250.0, history), "taking off")
        # < 0.5x median -> slow start
        self.assertEqual(analysis.compute_early_pace_flags(40.0, history), "slow start")
        # in between -> normal
        self.assertEqual(analysis.compute_early_pace_flags(120.0, history), "normal")

    def test_dimension_helpers(self):
        # IST 08:30 is 03:00 UTC
        morning_utc = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
        self.assertEqual(analysis.get_posting_hour_block(morning_utc), "Morning (6:00 AM–11:59 AM)")
        self.assertEqual(analysis.get_weekday_name(morning_utc), "Thursday")

        # Caption length
        self.assertEqual(analysis.get_caption_length_bucket("Short"), "< 50 chars")
        self.assertEqual(analysis.get_caption_length_bucket("x" * 75), "50–150 chars")
        self.assertEqual(analysis.get_caption_length_bucket("x" * 200), "> 150 chars")

        # Hashtags
        self.assertEqual(analysis.get_hashtag_bucket("No tags here"), "0")
        self.assertEqual(analysis.get_hashtag_bucket("Hello #tech #ai"), "1–3")
        self.assertEqual(analysis.get_hashtag_bucket("#1 #2 #3 #4 #5"), "4–10")
        self.assertEqual(analysis.get_hashtag_bucket(" ".join(f"#{i}" for i in range(15))), "> 10")

    def test_group_comparison_suppression(self):
        posts = [
            {"main_score": 100.0, "grp": "A"},
            {"main_score": 120.0, "grp": "A"},
            # Only 2 posts in group A -> too few posts
            {"main_score": 200.0, "grp": "B"},
            {"main_score": 210.0, "grp": "B"},
            {"main_score": 220.0, "grp": "B"},
            # 3 posts in group B -> sufficient data
        ]
        res = analysis.group_comparison(posts, lambda p: p["grp"])
        res_by_name = {r["group"]: r for r in res}
        self.assertEqual(res_by_name["A"]["status"], "too few posts")
        self.assertIsNone(res_by_name["A"]["median_views"])
        self.assertEqual(res_by_name["B"]["status"], "sufficient data")
        self.assertEqual(res_by_name["B"]["median_views"], 210.0)

    def test_growth_association(self):
        # 3 follower snapshots over 20 days
        snaps = [
            {"collected_at": T0, "followers": 100},
            {"collected_at": T0 + timedelta(days=5), "followers": 150},
            {"collected_at": T0 + timedelta(days=20), "followers": 300},
        ]
        pubs = [T0 + timedelta(days=2)]
        res = analysis.analyze_growth_association(snaps, pubs)
        self.assertEqual(res["tier"], "sufficient data")
        self.assertEqual(res["disclaimer"], "association, not cause")
        self.assertEqual(len(res["intervals"]), 2)
        self.assertIsNotNone(res["post_day_median_rate_24h"])


class TaggingTests(unittest.TestCase):
    def test_load_taxonomy(self):
        tax = load_taxonomy()
        self.assertIn("topic", tax)
        self.assertIn("format", tax)
        self.assertIn("hook", tax)
        self.assertIn("ai_tools", tax["topic"])
        self.assertIn("explainer", tax["format"])
        self.assertIn("problem_solution", tax["hook"])

    def test_hash_input(self):
        h1 = hash_input("Hello World")
        h2 = hash_input("  Hello World  ")
        h3 = hash_input("Different")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 64)

    @mock.patch("insight.tagging.db.get_setting")
    @mock.patch("google.generativeai.GenerativeModel")
    @mock.patch("google.generativeai.configure")
    def test_classify_caption_valid(self, mock_configure, mock_model_cls, mock_setting):
        mock_setting.return_value = "fake_key"
        mock_model = mock.Mock()
        mock_model_cls.return_value = mock_model

        mock_resp = mock.Mock()
        mock_resp.text = json.dumps({
            "topic": {"value": "ai_tools", "confidence": 0.95},
            "format": {"value": "explainer", "confidence": 0.88},
            "hook": {"value": "problem_solution", "confidence": 0.9},
        })
        mock_model.generate_content.return_value = mock_resp

        res = classify_caption("Learn how this new AI tool works #ai")
        self.assertEqual(res["topic"]["value"], "ai_tools")
        self.assertEqual(res["topic"]["confidence"], 0.95)
        self.assertEqual(res["format"]["value"], "explainer")
        self.assertEqual(res["hook"]["value"], "problem_solution")

    @mock.patch("insight.tagging.db.get_setting")
    @mock.patch("google.generativeai.GenerativeModel")
    @mock.patch("google.generativeai.configure")
    def test_classify_caption_invalid_taxonomy_becomes_unknown(self, mock_configure, mock_model_cls, mock_setting):
        mock_setting.return_value = "fake_key"
        mock_model = mock.Mock()
        mock_model_cls.return_value = mock_model

        mock_resp = mock.Mock()
        mock_resp.text = json.dumps({
            "topic": {"value": "INVALID_TOPIC_NAME", "confidence": 0.99},
            "format": {"value": "how_to", "confidence": 0.8},
            "hook": {"value": "unknown_hook_value", "confidence": 0.7},
        })
        mock_model.generate_content.return_value = mock_resp

        res = classify_caption("Random text")
        self.assertEqual(res["topic"]["value"], "unknown")
        self.assertEqual(res["topic"]["confidence"], 0.0)
        self.assertEqual(res["format"]["value"], "how_to")
        self.assertEqual(res["hook"]["value"], "unknown")
        self.assertEqual(res["hook"]["confidence"], 0.0)

    @mock.patch("insight.tagging.classify_caption")
    def test_tag_publication_and_retag_rules(self, mock_classify):
        mock_classify.return_value = {
            "topic": {"value": "programming", "confidence": 0.9},
            "format": {"value": "how_to", "confidence": 0.85},
            "hook": {"value": "question", "confidence": 0.75},
        }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "tag_test.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            with session_factory(engine)() as session:
                acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="tester", connected_at=T0)
                session.add(acct)
                pub = Publication(
                    id=1, platform="instagram", platform_post_id="P1", account_id=1,
                    media_type="VIDEO", media_product_type="REELS", caption="Coding tips #python",
                    published_at=T0,
                )
                session.add(pub)
                session.commit()

                # First tag: succeeds
                tagged = tag_publication(session, pub)
                self.assertTrue(tagged)
                session.commit()

                tags = session.scalars(select(PostTag).filter_by(publication_id=1)).all()
                self.assertEqual(len(tags), 3)
                self.assertEqual(set(t.dimension for t in tags), {"topic", "format", "hook"})

                # Second tag with unchanged caption: skipped
                tagged_again = tag_publication(session, pub)
                self.assertFalse(tagged_again)

                # Third tag with altered caption: re-tags
                pub.caption = "Coding tips #python updated"
                session.commit()
                tagged_updated = tag_publication(session, pub)
                self.assertTrue(tagged_updated)
                session.commit()

                updated_tags = session.scalars(select(PostTag).filter_by(publication_id=1)).all()
                self.assertEqual(len(updated_tags), 3)
                self.assertEqual(updated_tags[0].input_hash, hash_input("Coding tips #python updated"))

            engine.dispose()


class Migration0002Tests(unittest.TestCase):
    def test_upgrade_creates_post_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "alembic_0002.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            tables = set(inspect(engine).get_table_names())
            self.assertIn("post_tags", tables)

            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                diff = compare_metadata(ctx, Base.metadata)
                self.assertEqual(diff, [], f"Schema diff after migration 0002: {diff}")
            engine.dispose()


class CollectorTaggingResilienceTests(unittest.TestCase):
    @mock.patch("insight.collect.db.get_setting")
    @mock.patch("insight.http_client.requests.get")
    @mock.patch("insight.tagging.classify_caption")
    def test_collector_continues_if_tagging_fails(self, mock_classify, mock_get, mock_db_setting):
        """If AI tagging raises an exception, collection finishes successfully without failing."""
        mock_classify.side_effect = RuntimeError("Gemini API connection error")
        mock_db_setting.side_effect = lambda k: {
            "GRAPH_HOST": "graph.instagram.com",
            "GRAPH_VERSION": "v26.0",
            "META_ACCESS_TOKEN": "mock_token",
            "IG_USER_ID": "17841440",
            "GEMINI_API_KEY": "fake_key",
        }.get(k)

        def _fake_get(url, params=None, **kwargs):
            m = mock.Mock(status_code=200, headers={})
            if "insights" in str(url):
                m.json = lambda: {"data": []}
            elif "media" in str(url):
                m.json = lambda: {
                    "data": [
                        {
                            "id": "reel_tag_fail",
                            "media_type": "VIDEO",
                            "media_product_type": "REELS",
                            "timestamp": T0.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                            "caption": "Will fail tagging",
                        }
                    ],
                    "paging": {},
                }
            else:
                m.json = lambda: {"id": "17841440", "username": "streamovate", "followers_count": 50}
            return m

        mock_get.side_effect = _fake_get

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "collector_resilience.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            from insight.collect import Collector
            c = Collector(dry_run=False, call_cap=20, engine=engine)
            stats = c.collect()

            self.assertEqual(stats["errors"], 0)
            self.assertEqual(stats["posts_synced"], 1)
            self.assertEqual(stats.get("tagged", 0), 0)
            engine.dispose()


class StreamlitPerformanceTabRenderTests(unittest.TestCase):
    def test_render_performance_tab_not_enough_data(self):
        """Render against a DB with only 2 publications: shows 'Not Enough Data' banner."""
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "few_posts.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            # Insert 2 publications with 7d snapshots
            with session_factory(engine)() as s:
                acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="tester", connected_at=T0)
                s.add(acct)
                p1 = Publication(id=1, platform="instagram", platform_post_id="P1", account_id=1,
                                 media_type="VIDEO", media_product_type="REELS", caption="P1", published_at=T0)
                p2 = Publication(id=2, platform="instagram", platform_post_id="P2", account_id=1,
                                 media_type="VIDEO", media_product_type="REELS", caption="P2", published_at=T0)
                s.add_all([p1, p2])
                s.flush()
                snap1 = MetricSnapshot(id=1, subject_type="publication", subject_id=1, publication_id=1,
                                       checkpoint="7d", period_key="7d", collected_at=T0 + timedelta(days=7),
                                       completeness="complete")
                snap2 = MetricSnapshot(id=2, subject_type="publication", subject_id=2, publication_id=2,
                                       checkpoint="7d", period_key="7d", collected_at=T0 + timedelta(days=7),
                                       completeness="complete")
                s.add_all([snap1, snap2])
                s.flush()
                s.add(MetricValue(snapshot_id=1, canonical_metric="views", value=150.0))
                s.add(MetricValue(snapshot_id=2, canonical_metric="views", value=250.0))
                s.commit()
            engine.dispose()

            old_env = os.environ.get("INSIGHT_VIEW_DB_PATH")
            at = None
            try:
                os.environ["INSIGHT_VIEW_DB_PATH"] = db_path
                at = AppTest.from_file("insight/view.py")
                at.run(timeout=30)
                self.assertEqual(len(at.exception), 0, f"AppTest raised: {at.exception}")
                tab_labels = [tab.label for tab in at.tabs]
                self.assertIn("🎯 Performance", tab_labels)
                info_texts = [info.value for info in at.info]
                self.assertTrue(any("Not Enough Data" in text for text in info_texts), f"Info text: {info_texts}")
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


if __name__ == "__main__":
    unittest.main()
