"""Unit tests for Step 6: recommendations, evaluation, number-guard, and follow-through tracking.

All tests use temporary SQLite databases and mocked Gemini calls. No external API calls.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from sqlalchemy import select

from insight.db import make_engine, session_factory, sqlite_url, upgrade_db
from insight.models import (
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
)
from insight.recommend import (
    IST,
    count_usable_7d_posts,
    generate_all_post_feedback,
    generate_evidence_recommendations,
    generate_exploration_recommendations,
    generate_post_feedback,
    generate_weekly_recommendation_set,
    get_ist_monday,
    get_ist_week_key,
    get_recommendation_mode,
    reconcile_follow_through,
    validate_ai_quality,
    validate_ai_text,
)
from insight.timeutil import UTC


class NumberGuardTests(unittest.TestCase):
    def test_number_guard_accepts_valid_numbers_and_multipliers(self):
        facts = {
            "views": 200,
            "baseline_views": 100,
            "ratio": 2.0,
            "post_count": 3,
            "checkpoint_days": 7,
            "topic": "ai_tools",
            "format": "explainer",
        }
        # Exact numbers and allowed multiplier 'twice' (2.0)
        valid_text = "This explainer reel reached 200 views at 7 days, which is twice your baseline across 3 posts."
        ok, msg = validate_ai_text(valid_text, facts)
        self.assertTrue(ok, msg)

    def test_number_guard_rejects_invented_numbers(self):
        facts = {"views": 100, "checkpoint_days": 7}
        # 500 is not in facts
        invalid_text = "This post gained 500 views in 7 days."
        ok, msg = validate_ai_text(invalid_text, facts)
        self.assertFalse(ok)
        self.assertIn("500", msg)

    def test_number_guard_rejects_invented_words_and_multipliers(self):
        facts = {"views": 100, "ratio": 1.1, "post_count": 2}
        # 'triple' (3.0) and 'four' (4) not in facts
        invalid_text = "Your engagement was triple normal across four posts."
        ok, msg = validate_ai_text(invalid_text, facts)
        self.assertFalse(ok)

    def test_number_words_like_twice_double_rejected_when_not_in_facts(self):
        facts = {"views": 100, "baseline": 80}
        bad_twice = "Your post got twice the expected baseline views."
        ok, msg = validate_ai_text(bad_twice, facts)
        self.assertFalse(ok)
        self.assertIn("twice", msg)

        bad_double = "Engagement was double normal."
        ok, msg = validate_ai_text(bad_double, facts)
        self.assertFalse(ok)
        self.assertIn("double", msg)

    def test_number_guard_rejects_causal_language(self):
        facts = {"views": 100, "topic": "ai_tools"}
        # 'caused' is forbidden
        invalid_text = "The AI topic caused 100 views."
        ok, msg = validate_ai_text(invalid_text, facts)
        self.assertFalse(ok)
        self.assertIn("causal", msg)


class QualityGuardTests(unittest.TestCase):
    """Test that validate_ai_quality rejects internal terms, backticks, underscores, and snake_case."""

    def test_quality_guard_accepts_clean_creator_text(self):
        clean = "Try a how-to on software and apps, opening with a bold claim, posted Friday afternoon."
        ok, msg = validate_ai_quality(clean)
        self.assertTrue(ok, msg)

    def test_quality_guard_rejects_rank_leak_and_checkpoint_and_underscores(self):
        # Bad sentence 1 from real output
        bad1 = (
            "You've achieved a great rank 2! To build on this positive momentum and work towards the "
            "10 posts required for evidence within 7 checkpoint days, consider creating another 'how_to' "
            "video associated with 'software_apps' and a 'bold_claim' hook for a Friday Afternoon (12:00 - 15:00) post."
        )
        ok, msg = validate_ai_quality(bad1)
        self.assertFalse(ok)

    def test_quality_guard_rejects_backticks_and_raw_snake_case_keys(self):
        # Bad sentence 2 from real output
        bad2 = (
            "For your next video, continue exploring the `tech_news` topic using a `myth_busting` format and a `story` hook. "
            "This will help gather the 10 posts required for evidence, as you currently have 0 past posts for each of these "
            "elements and 1 total post in the last 7 days."
        )
        ok, msg = validate_ai_quality(bad2)
        self.assertFalse(ok)
        self.assertIn("backtick", msg.lower())

    def test_quality_guard_rejects_internal_keys_and_rank_and_checkpoint(self):
        # Bad sentence 3 from real output
        bad3 = (
            "Next, craft an `exploration` video on `ai_tools` in a `top_list` format, using a `surprising_stat` hook, "
            "as you have `0` past posts associated with that topic, format, or hook. This new content is associated with "
            "reaching a rank `4` and can help grow your `total_7d_posts` from `1` for the `7` day checkpoint, advancing "
            "you towards the `10` posts required for evidence."
        )
        ok, msg = validate_ai_quality(bad3)
        self.assertFalse(ok)

    def test_quality_guard_rejects_mode_and_rank_and_snake_case(self):
        # Bad sentence 4 from real output
        bad4 = (
            "For your next video, create an 'exploration' mode 'programming' 'news_update' with a 'problem_solution' hook, "
            "as it's your rank 1 recommendation and has 0 past posts associated with it for the topic, format, or hook. "
            "This choice is a great step towards your 10 required posts for evidence within 7 checkpoint days, building on "
            "your 1 total post in the last 7 days."
        )
        ok, msg = validate_ai_quality(bad4)
        self.assertFalse(ok)


class SharedTimeBlocksTests(unittest.TestCase):
    def test_recommend_and_analysis_use_same_time_blocks_definition(self):
        """Recommend and analysis must share the exact same POSTING_BLOCKS definition."""
        from insight import analysis, recommend
        self.assertIs(recommend.POSTING_BLOCKS, analysis.POSTING_BLOCKS)
        self.assertEqual(
            analysis.POSTING_BLOCKS,
            [
                "Morning (6:00 AM–11:59 AM)",
                "Afternoon (12:00 PM–4:59 PM)",
                "Evening (5:00 PM–9:59 PM)",
                "Night (10:00 PM–5:59 AM)",
            ],
        )

    def test_posting_hour_block_12h_mapping(self):
        """get_posting_hour_block maps UTC times to IST 12-hour block strings."""
        from insight import analysis
        # 03:00 UTC = 08:30 IST -> Morning (6:00 AM–11:59 AM)
        self.assertEqual(analysis.get_posting_hour_block(datetime(2026, 10, 1, 3, 0, tzinfo=UTC)), "Morning (6:00 AM–11:59 AM)")
        # 08:00 UTC = 13:30 IST -> Afternoon (12:00 PM–4:59 PM)
        self.assertEqual(analysis.get_posting_hour_block(datetime(2026, 10, 1, 8, 0, tzinfo=UTC)), "Afternoon (12:00 PM–4:59 PM)")
        # 13:00 UTC = 18:30 IST -> Evening (5:00 PM–9:59 PM)
        self.assertEqual(analysis.get_posting_hour_block(datetime(2026, 10, 1, 13, 0, tzinfo=UTC)), "Evening (5:00 PM–9:59 PM)")
        # 18:00 UTC = 23:30 IST -> Night (10:00 PM–5:59 AM)
        self.assertEqual(analysis.get_posting_hour_block(datetime(2026, 10, 1, 18, 0, tzinfo=UTC)), "Night (10:00 PM–5:59 AM)")


class ISTWeekBoundaryTests(unittest.TestCase):
    def test_sunday_night_vs_monday_morning_boundary(self):
        # Sunday 2026-10-04 23:59:59 IST is UTC 2026-10-04 18:29:59
        sun_ist = datetime(2026, 10, 4, 18, 29, 59, tzinfo=timezone.utc)
        week_sun = get_ist_week_key(sun_ist)
        # Week 40 in 2026: Oct 4 is Sunday of W40
        self.assertEqual(week_sun, "2026-W40")

        # Monday 2026-10-05 00:00:01 IST is UTC 2026-10-04 18:30:01
        mon_ist = datetime(2026, 10, 4, 18, 30, 1, tzinfo=timezone.utc)
        week_mon = get_ist_week_key(mon_ist)
        # Week 41 begins on Monday Oct 5
        self.assertEqual(week_mon, "2026-W41")


class RecommendationEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "rec_test.db")
        self.engine = make_engine(sqlite_url(self.db_path))
        upgrade_db(self.engine)
        self.now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def _create_publication_with_7d(self, session, pub_id: int, views: float, topic: str, fmt: str, hook: str, pub_dt: datetime):
        acc = session.scalar(select(Account).filter_by(id=1))
        if not acc:
            acc = Account(id=1, platform="instagram", platform_account_id="17841440", handle="streamovate", connected_at=pub_dt)
            session.add(acc)
            session.flush()

        pub = Publication(
            id=pub_id,
            account_id=acc.id,
            platform="instagram",
            platform_post_id=f"post_{pub_id}",
            media_type="VIDEO",
            media_product_type="REELS",
            published_at=pub_dt,
            caption=f"Reel {pub_id} about {topic}",
        )
        session.add(pub)
        session.flush()

        # Add post tags
        session.add(PostTag(publication_id=pub.id, dimension="topic", value=topic, confidence=0.9, model="m", prompt_version="v1", input_hash=f"h_{pub_id}", tagged_at=pub_dt))
        session.add(PostTag(publication_id=pub.id, dimension="format", value=fmt, confidence=0.9, model="m", prompt_version="v1", input_hash=f"h_{pub_id}", tagged_at=pub_dt))
        session.add(PostTag(publication_id=pub.id, dimension="hook", value=hook, confidence=0.9, model="m", prompt_version="v1", input_hash=f"h_{pub_id}", tagged_at=pub_dt))
        session.flush()

        # 7d snapshot with views
        snap_dt = pub_dt + timedelta(days=7)
        snap = MetricSnapshot(
            publication_id=pub.id,
            subject_type="publication",
            subject_id=pub.id,
            checkpoint="7d",
            period_key="7d",
            collected_at=snap_dt,
            completeness="complete",
            time_since_publish_seconds=7 * 86400,
        )
        session.add(snap)
        session.flush()

        session.add(MetricValue(snapshot_id=snap.id, canonical_metric="views", value=views))
        session.add(MetricValue(snapshot_id=snap.id, canonical_metric="reach", value=max(10, views * 0.8)))
        session.add(MetricValue(snapshot_id=snap.id, canonical_metric="total_interactions", value=max(2, views * 0.1)))
        session.flush()
        return pub

    def test_mode_switch_at_10_posts(self):
        """Mode is exploration for <10 posts with 7d views; switches to evidence at 10."""
        with session_factory(self.engine)() as session:
            # 9 posts -> exploration
            for i in range(1, 10):
                self._create_publication_with_7d(session, i, 100 + i * 10, "ai_tools", "explainer", "question", self.now - timedelta(days=15 + i))
            session.commit()

            self.assertEqual(count_usable_7d_posts(session), 9)
            self.assertEqual(get_recommendation_mode(session), "exploration")

            # 10th post -> evidence
            self._create_publication_with_7d(session, 10, 150, "programming", "how_to", "story", self.now - timedelta(days=25))
            session.commit()

            self.assertEqual(count_usable_7d_posts(session), 10)
            self.assertEqual(get_recommendation_mode(session), "evidence")

    def test_exploration_mode_diversity_and_determinism(self):
        """Exploration recommendations have distinct topics and are deterministic for a week."""
        with session_factory(self.engine)() as session:
            # 2 past posts with 'ai_tools'
            self._create_publication_with_7d(session, 1, 100, "ai_tools", "explainer", "question", self.now - timedelta(days=10))
            self._create_publication_with_7d(session, 2, 120, "ai_tools", "how_to", "bold_claim", self.now - timedelta(days=12))
            session.commit()

            week_key = "2026-W40"
            recs1 = generate_exploration_recommendations(session, week_key)
            recs2 = generate_exploration_recommendations(session, week_key)

            # Determinism check: identical results for same week_key
            self.assertEqual(len(recs1), len(recs2))
            for r1, r2 in zip(recs1, recs2):
                self.assertEqual(r1["topic"], r2["topic"])
                self.assertEqual(r1["format"], r2["format"])
                self.assertEqual(r1["hook"], r2["hook"])

            # Diversity check: all topics distinct
            topics = [r["topic"] for r in recs1]
            self.assertEqual(len(topics), len(set(topics)))
            self.assertGreaterEqual(len(topics), 3)

            # Untried first: 'ai_tools' has count 2, other topics have count 0,
            # so untried topics appear before ai_tools or ai_tools is ranked lower
            untried_topics = [t for t in topics if t != "ai_tools"]
            self.assertGreater(len(untried_topics), 0)

    def test_evidence_mode_selection_and_symmetric_avoid_thresholds(self):
        """Evidence mode recommends groups with >=1.2x baseline and >=3 posts; avoids <=0.8x baseline."""
        with session_factory(self.engine)() as session:
            # Create 12 posts with median ~100
            # 3 posts on programming with 150 views (1.5x baseline -> recommend!)
            for i in range(1, 4):
                self._create_publication_with_7d(session, i, 150, "programming", "how_to", "demo_visual", self.now - timedelta(days=15 + i))

            # 3 posts on tech_news with 60 views (0.6x baseline -> avoid!)
            for i in range(4, 7):
                self._create_publication_with_7d(session, i, 60, "tech_news", "news_update", "bold_claim", self.now - timedelta(days=20 + i))

            # 6 neutral posts on software_apps with 100 views (1.0x baseline -> neutral)
            for i in range(7, 13):
                self._create_publication_with_7d(session, i, 100, "software_apps", "explainer", "question", self.now - timedelta(days=25 + i))

            session.commit()

            recs, avoid = generate_evidence_recommendations(session, "2026-W40")

            # Programming (1.5x) should be recommended
            rec_topics = [r["topic"] for r in recs if r["kind"] == "evidence"]
            self.assertIn("programming", rec_topics)

            # Tech news (0.6x) should be in avoid notes
            avoid_topics = [a["value"] for a in avoid if a["dimension"] == "topic"]
            self.assertIn("tech_news", avoid_topics)

            # Software apps (1.0x neutral) should neither be recommended nor avoided
            self.assertNotIn("software_apps", avoid_topics)

    def test_audience_ideas_rank_first_in_evidence_mode(self):
        """Step 5 audience ideas with >=2 distinct authors rank #1 in evidence mode."""
        with session_factory(self.engine)() as session:
            # 10 posts for evidence mode
            for i in range(1, 11):
                self._create_publication_with_7d(session, i, 100, "ai_tools", "explainer", "question", self.now - timedelta(days=15 + i))

            # Add comments forming an audience idea with 2 authors
            now = datetime.now(UTC)
            c1 = Comment(id=1, platform="instagram", platform_comment_id="c1", publication_id=1, text="How to setup Docker?", created_at=now, author_hash="hash_a", is_own_account=False)
            c2 = Comment(id=2, platform="instagram", platform_comment_id="c2", publication_id=1, text="Docker tutorial please", created_at=now, author_hash="hash_b", is_own_account=False)
            session.add_all([c1, c2])
            session.flush()

            l1 = CommentLabel(comment_id=1, category="request", sentiment="positive", confidence=0.9, needs_reply=True, theme="Docker Setup", source="ai", model="m", prompt_version="v1", input_hash="h1", labelled_at=now)
            l2 = CommentLabel(comment_id=2, category="request", sentiment="positive", confidence=0.9, needs_reply=True, theme="Docker Setup", source="ai", model="m", prompt_version="v1", input_hash="h2", labelled_at=now)
            session.add_all([l1, l2])
            session.commit()

            recs, _ = generate_evidence_recommendations(session, "2026-W40")
            self.assertGreaterEqual(len(recs), 1)
            # Rank 1 must be audience idea
            r1 = recs[0]
            self.assertEqual(r1["rank"], 1)
            self.assertEqual(r1["kind"], "audience_idea")
            self.assertEqual(r1["facts_json"]["theme"], "Docker Setup")

    def test_post_feedback_in_not_enough_data_tier_calls_no_gemini_and_has_no_judgements(self):
        """In <5 posts tier, post feedback NEVER calls Gemini and contains zero judgement words."""
        with session_factory(self.engine)() as session:
            pub = self._create_publication_with_7d(session, 1, 150, "ai_tools", "explainer", "question", self.now - timedelta(days=8))
            session.commit()

            with mock.patch("insight.recommend.call_gemini_wording") as mock_gemini:
                fb = generate_post_feedback(session, pub, now=self.now)
                self.assertIsNotNone(fb)
                # Gemini must NOT have been called
                mock_gemini.assert_not_called()
                self.assertFalse(fb.is_ai_text)

                # Zero judgement words check
                lower_text = fb.text.lower()
                for bad_word in ("great", "poor", "flop", "successful", "underperformed", "good", "bad", "amazing", "terrible"):
                    self.assertNotIn(bad_word, lower_text)

    def test_one_to_one_matching_and_outcome_recording(self):
        """A post satisfies at most one recommendation (highest rank wins). Outcomes recorded on 7d."""
        with session_factory(self.engine)() as session:
            set_time = self.now - timedelta(days=2)
            rec_set = RecommendationSet(week_key="2026-W40", mode="exploration", generated_at=set_time, prompt_version="v1")
            session.add(rec_set)
            session.flush()

            # Two recommendations matching 'ai_tools' + 'explainer': Rank 1 and Rank 2
            r1 = Recommendation(set_id=rec_set.id, rank=1, kind="exploration", topic="ai_tools", format="explainer", hook="question", posting_block="Evening", weekday="Wednesday", facts_json="{}", text="R1", is_ai_text=False, confidence="low", status="open")
            r2 = Recommendation(set_id=rec_set.id, rank=2, kind="exploration", topic="ai_tools", format="explainer", hook="bold_claim", posting_block="Night", weekday="Thursday", facts_json="{}", text="R2", is_ai_text=False, confidence="low", status="open")
            session.add_all([r1, r2])
            session.commit()

            # Now create one new publication published after set_time with ai_tools + explainer
            pub1 = self._create_publication_with_7d(session, 1, 150, "ai_tools", "explainer", "question", set_time + timedelta(hours=5))
            session.commit()

            matches = reconcile_follow_through(session, now=self.now)
            self.assertEqual(matches, 1)

            # r1 (rank 1) should be matched
            session.refresh(r1)
            session.refresh(r2)
            self.assertEqual(r1.status, "followed")
            self.assertEqual(r1.matched_publication_id, pub1.id)
            # r2 should remain open (one-to-one matching)
            self.assertEqual(r2.status, "open")
            self.assertIsNone(r2.matched_publication_id)

            # Outcome ratio check (baseline is 150 since 1 post with 150 views -> 1.0)
            self.assertIsNotNone(r1.outcome_ratio)
            self.assertEqual(r1.outcome_ratio, 1.0)

    def test_expired_recommendations_after_14_days(self):
        """Recommendations older than 14 days without match are marked expired."""
        with session_factory(self.engine)() as session:
            old_time = self.now - timedelta(days=15)
            rec_set = RecommendationSet(week_key="2026-W38", mode="exploration", generated_at=old_time, prompt_version="v1")
            session.add(rec_set)
            session.flush()

            r = Recommendation(set_id=rec_set.id, rank=1, kind="exploration", topic="ai_tools", format="explainer", hook="question", posting_block="Evening", weekday="Wednesday", facts_json="{}", text="R", is_ai_text=False, confidence="low", status="open")
            session.add(r)
            session.commit()

            reconcile_follow_through(session, now=self.now)
            session.refresh(r)
            self.assertEqual(r.status, "expired")


class StreamlitRecommendationsTabTests(unittest.TestCase):
    def test_recommendations_tab_renders_no_exceptions(self):
        """AppTest verifies recommendations tab renders cleanly without exceptions."""
        from streamlit.testing.v1 import AppTest
        old_env = os.environ.get("INSIGHT_VIEW_DB_PATH")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "view_test.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
            with session_factory(engine)() as session:
                acc = Account(id=1, platform="instagram", platform_account_id="17841440", handle="streamovate", connected_at=now)
                session.add(acc)
                pub = Publication(id=1, account_id=acc.id, platform="instagram", platform_post_id="post_1", media_type="VIDEO", media_product_type="REELS", published_at=now, caption="Test reel")
                session.add(pub)
                session.flush()
                rec_set = generate_weekly_recommendation_set(session, now=now)
                session.commit()
            engine.dispose()

            at = None
            try:
                os.environ["INSIGHT_VIEW_DB_PATH"] = db_path
                at = AppTest.from_file("insight/view.py")
                at.run(timeout=30)
                self.assertEqual(len(at.exception), 0, f"AppTest raised exceptions: {at.exception}")

                tab_labels = [tab.label for tab in at.tabs]
                self.assertIn("💡 Recommendations", tab_labels)
            finally:
                if at is not None and "ro_engine" in at.session_state:
                    try:
                        at.session_state["ro_engine"].dispose()
                    except Exception:
                        pass
                if old_env is not None:
                    os.environ["INSIGHT_VIEW_DB_PATH"] = old_env
                elif "INSIGHT_VIEW_DB_PATH" in os.environ:
                    del os.environ["INSIGHT_VIEW_DB_PATH"]


if __name__ == "__main__":
    unittest.main()
