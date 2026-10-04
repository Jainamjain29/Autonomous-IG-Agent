"""Tests for weekly report generation, deterministic metrics, AI/template summary, dual guards, and Markdown/HTML exporters.

Run: python -m unittest tests/test_insight_report.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from insight.db import make_engine, session_factory, sqlite_url, upgrade_db
from insight.models import (
    Account,
    MetricSnapshot,
    MetricValue,
    PostTag,
    Publication,
    Recommendation,
    RecommendationSet,
    WeeklyReport,
)
from insight.report import (
    compute_weekly_report_data,
    generate_report_html,
    generate_report_markdown,
    generate_report_summary,
    get_available_weeks,
)
from insight.timeutil import UTC


class WeeklyReportTests(unittest.TestCase):
    """Unit tests for weekly report calculation and formatting."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp_dir.name, "test_report.db")
        self.engine = make_engine(sqlite_url(self.db_path))
        upgrade_db(self.engine)
        self.Session = session_factory(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp_dir.cleanup()

    def test_week_bounds_and_available_weeks(self):
        """Test available weeks discovery across publications and snapshots."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # 2026-W40
        with self.Session() as session:
            acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="test", connected_at=now)
            session.add(acct)
            session.flush()

            # Publication in W39
            w39_dt = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
            pub = Publication(id=1, platform="instagram", platform_post_id="P1", account_id=1,
                              media_type="VIDEO", media_product_type="REELS", caption="Reel W39",
                              published_at=w39_dt)
            session.add(pub)
            session.commit()

            weeks = get_available_weeks(session, now=now)
            self.assertIn("2026-W40", weeks)
            self.assertIn("2026-W39", weeks)
            self.assertEqual(weeks[0], "2026-W40")  # Newest first

    def test_compute_weekly_report_data_deterministic(self):
        """Test that report metrics are computed deterministically."""
        # IST Monday of W40: 2026-09-28 00:00:00 IST is 2026-09-27 18:30:00 UTC
        # Monday midday UTC: 2026-09-28 12:00:00 UTC
        w40_pub_dt = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)

        with self.Session() as session:
            acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="test", connected_at=w40_pub_dt)
            session.add(acct)
            session.flush()

            # Publication in W40
            pub = Publication(id=1, platform="instagram", platform_post_id="P1", account_id=1,
                              media_type="VIDEO", media_product_type="REELS", caption="How to test software",
                              published_at=w40_pub_dt)
            session.add(pub)
            session.flush()

            # Tags for pub
            session.add_all([
                PostTag(publication_id=1, dimension="topic", value="software_apps", confidence=0.9, model="test", prompt_version="v1", input_hash="h1", tagged_at=w40_pub_dt),
                PostTag(publication_id=1, dimension="format", value="how_to", confidence=0.85, model="test", prompt_version="v1", input_hash="h2", tagged_at=w40_pub_dt),
                PostTag(publication_id=1, dimension="hook", value="bold_claim", confidence=0.88, model="test", prompt_version="v1", input_hash="h3", tagged_at=w40_pub_dt),
            ])

            # Snapshots in W40
            s_views = MetricSnapshot(id=1, subject_type="publication", subject_id=1, publication_id=1,
                                     checkpoint="24h", period_key="24h", collected_at=w40_pub_dt + timedelta(hours=24),
                                     time_since_publish_seconds=86400, completeness="complete")
            session.add(s_views)
            session.flush()
            session.add_all([
                MetricValue(snapshot_id=1, canonical_metric="views", value=1500.0),
                MetricValue(snapshot_id=1, canonical_metric="comments", value=3.0),
            ])

            # Follower snapshots in W40 (start and end)
            f_start = MetricSnapshot(id=10, subject_type="account", subject_id=1, account_id=1, checkpoint="adhoc",
                                     period_key="adhoc_f1", collected_at=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
                                     completeness="complete")
            f_end = MetricSnapshot(id=11, subject_type="account", subject_id=1, account_id=1, checkpoint="adhoc",
                                   period_key="adhoc_f2", collected_at=datetime(2026, 10, 4, 18, 0, tzinfo=UTC),
                                   completeness="complete")
            session.add_all([f_start, f_end])
            session.flush()
            session.add_all([
                MetricValue(snapshot_id=10, canonical_metric="followers", value=1200.0),
                MetricValue(snapshot_id=11, canonical_metric="followers", value=1250.0),
            ])

            # Next week's recommendation set (W41)
            rec_set_w41 = RecommendationSet(id=1, week_key="2026-W41", mode="exploration", prompt_version="v1", generated_at=w40_pub_dt)
            session.add(rec_set_w41)
            session.flush()
            rec1 = Recommendation(
                id=1, set_id=1, rank=1, kind="exploration", topic="coding_tips", format="listicle",
                hook="question", weekday="Wednesday", posting_block="Morning (6:00 AM – 11:59 AM)",
                confidence="low", facts_json="{}", status="open", text="Test a coding listicle.", is_ai_text=False,
            )
            session.add(rec1)
            session.commit()

            data = compute_weekly_report_data(session, week_key="2026-W40")

            self.assertEqual(data["published_count"], 1)
            self.assertEqual(data["total_views"], 1500)
            self.assertEqual(data["follower_delta"], 50)
            self.assertEqual(data["follower_start"]["count"], 1200)
            self.assertEqual(data["follower_end"]["count"], 1250)
            self.assertTrue(data["unreadable_comments_flag"])  # raw comments metric is 3, but 0 collected comments
            self.assertEqual(len(data["next_plan"]), 1)
            self.assertEqual(data["next_plan"][0]["topic"], "Coding Tips")

    def test_low_data_tier_uses_template_without_gemini(self):
        """In low-data tier (<5 posts with 7d views), summary uses code template only and never calls Gemini."""
        report_data = {
            "week_key": "2026-W40",
            "is_current_week": False,
            "usable_7d_posts": 2,  # < 5
            "published_count": 1,
            "total_views": 1500,
            "follower_delta": 25,
            "followed_recommendations": [],
        }

        with self.Session() as session:
            with patch("insight.report.call_gemini_wording") as mock_gemini:
                summary, is_ai = generate_report_summary(session, report_data, save_to_db=False)
                mock_gemini.assert_not_called()
                self.assertFalse(is_ai)
                self.assertIn("2026-W40", summary)
                self.assertIn("1 Reel(s)", summary)
                self.assertIn("1,500 views", summary)
                self.assertIn("gained 25 followers", summary)
                self.assertIn("not enough data", summary)

    def test_summary_dual_guards_reject_bad_ai_text(self):
        """In evidence tier, AI text containing forbidden multipliers or taxonomy keys is rejected."""
        report_data = {
            "week_key": "2026-W40",
            "is_current_week": False,
            "usable_7d_posts": 10,  # Evidence tier
            "published_count": 1,  # Note: 1 post, so 2.0 is not in allowed numbers
            "total_views": 5000,
            "follower_delta": 50,
            "followed_recommendations": [],
        }

        with self.Session() as session:
            # Bad text 1: Uses number word multiplier "double" (2.0 not in facts)
            with patch("insight.report.call_gemini_wording", return_value="Views were double compared to last week."):
                summary, is_ai = generate_report_summary(session, report_data, save_to_db=False)
                self.assertFalse(is_ai)
                self.assertNotIn("double", summary)

            # Bad text 2: Leaks raw snake_case taxonomy key or backtick
            with patch("insight.report.call_gemini_wording", return_value="Great job on `software_apps` format."):
                summary, is_ai = generate_report_summary(session, report_data, save_to_db=False)
                self.assertFalse(is_ai)
                self.assertNotIn("`software_apps`", summary)

    def test_markdown_and_html_exporters(self):
        """Verify markdown and HTML exporters produce structured, valid output with all sections."""
        report_data = {
            "week_key": "2026-W40",
            "is_current_week": True,
            "week_start_ist": "Sep 28, 2026 at 12:00 AM IST",
            "week_end_ist": "Oct 04, 2026 at 11:59 PM IST",
            "published_count": 1,
            "total_views": 1500,
            "baseline_views": 1000,
            "usable_7d_posts": 3,
            "confidence_tier": "not enough data",
            "publications": [
                {
                    "id": 1,
                    "published_at_ist": "Sep 29, 2026 at 3:30 PM IST",
                    "caption": "Reel Caption",
                    "topic": "Software & Apps",
                    "format": "How-To",
                    "hook": "Bold Claim",
                    "views": 1500,
                }
            ],
            "follower_start": {"count": 1200, "time_ist": "Sep 28, 2026 at 11:30 AM IST"},
            "follower_end": {"count": 1250, "time_ist": "Oct 04, 2026 at 11:30 PM IST"},
            "follower_delta": 50,
            "followed_recommendations": [
                {
                    "rank": 1,
                    "topic": "Software & Apps",
                    "format": "How-To",
                    "matched_pub_id": 1,
                    "outcome_ratio": 1.5,
                }
            ],
            "collected_comments_count": 0,
            "unreadable_comments_flag": True,
            "data_health": {
                "total_snapshots": 12,
                "complete": 10,
                "delayed": 2,
                "unavailable": 0,
            },
            "next_week_key": "2026-W41",
            "next_plan": [
                {
                    "rank": 1,
                    "kind": "exploration",
                    "topic": "Coding Tips",
                    "format": "Listicle",
                    "hook": "Question",
                    "weekday": "Wednesday",
                    "posting_block": "Morning (6:00 AM – 11:59 AM)",
                    "action": "Post a coding listicle.",
                }
            ],
        }

        summary = "During 2026-W40, 1 Reel(s) were published totaling 1,500 views."

        # Markdown test
        md = generate_report_markdown(report_data, summary)
        self.assertIn("# 📊 Weekly Executive Report — 2026-W40", md)
        self.assertIn("Publishing Activity", md)
        self.assertIn("Follower Growth", md)
        self.assertIn("readings taken at irregular times", md.lower())
        self.assertIn("Comments exist but aren't readable yet (Meta app not Live)", md)
        self.assertIn("Next Week's Plan (2026-W41)", md)
        self.assertIn("Coding Tips", md)

        # HTML test
        html = generate_report_html(report_data, summary)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("Weekly Executive Report — 2026-W40", html)
        self.assertIn("Comments exist but aren't readable yet (Meta app not Live)", html)
        self.assertIn("readings taken at irregular times", html.lower())
        self.assertIn("</html>", html)

    def test_unreadable_comments_message_shown_instead_of_zero_comments(self):
        """Refinement (c): When comments_count > 0 but API returns none, report & audience show unreadable message, never '0 comments'."""
        report_data = {
            "week_key": "2026-W40",
            "is_current_week": False,
            "week_start_ist": "Sep 28, 2026 at 12:00 AM IST",
            "week_end_ist": "Oct 04, 2026 at 11:59 PM IST",
            "published_count": 1,
            "total_views": 1000,
            "baseline_views": 1000,
            "usable_7d_posts": 1,
            "confidence_tier": "not enough data",
            "publications": [],
            "follower_start": None,
            "follower_end": None,
            "follower_delta": 0,
            "followed_recommendations": [],
            "collected_comments_count": 0,
            "unreadable_comments_flag": True,  # Post has comments metric > 0, but API returned 0
            "data_health": {},
            "next_week_key": "2026-W41",
            "next_plan": [],
        }

        md = generate_report_markdown(report_data, summary_text="Summary")
        self.assertIn("Comments exist but aren't readable yet (Meta app not Live)", md)
        self.assertNotIn("0 comments", md.lower())

        html = generate_report_html(report_data, summary_text="Summary")
        self.assertIn("Comments exist but aren't readable yet (Meta app not Live)", html)
        self.assertNotIn("0 comments", html.lower())

    def test_follower_growth_shows_both_readings_times_and_approximate_label(self):
        """Refinement (d): Follower growth displays both readings with 12h IST times and approximate label."""
        report_data = {
            "week_key": "2026-W40",
            "is_current_week": False,
            "week_start_ist": "Sep 28, 2026 at 12:00 AM IST",
            "week_end_ist": "Oct 04, 2026 at 11:59 PM IST",
            "published_count": 1,
            "total_views": 1000,
            "baseline_views": 1000,
            "usable_7d_posts": 1,
            "confidence_tier": "not enough data",
            "publications": [],
            "follower_start": {"count": 1200, "time_ist": "Sep 28, 2026 at 11:30 AM IST"},
            "follower_end": {"count": 1250, "time_ist": "Oct 04, 2026 at 11:30 PM IST"},
            "follower_delta": 50,
            "followed_recommendations": [],
            "collected_comments_count": 0,
            "unreadable_comments_flag": False,
            "data_health": {},
            "next_week_key": "2026-W41",
            "next_plan": [],
        }

        md = generate_report_markdown(report_data, summary_text="Summary")
        self.assertIn("1,200", md)
        self.assertIn("Sep 28, 2026 at 11:30 AM IST", md)
        self.assertIn("1,250", md)
        self.assertIn("Oct 04, 2026 at 11:30 PM IST", md)
        self.assertIn("readings taken at irregular times", md.lower())

        html = generate_report_html(report_data, summary_text="Summary")
        self.assertIn("1,200", html)
        self.assertIn("Sep 28, 2026 at 11:30 AM IST", html)
        self.assertIn("1,250", html)
        self.assertIn("Oct 04, 2026 at 11:30 PM IST", html)
        self.assertIn("readings taken at irregular times", html.lower())

    def test_next_weeks_plan_reuses_step6_set_with_no_ai_call(self):
        """Refinement (e): Next week's plan reuses the Step 6 recommendation set with human labels and zero AI calls."""
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
        with self.Session() as session:
            acct = Account(id=1, platform="instagram", platform_account_id="A1", handle="test", connected_at=now)
            session.add(acct)
            session.flush()

            # Seed existing Step 6 recommendation set for next week (2026-W41)
            rec_set_w41 = RecommendationSet(
                id=1,
                week_key="2026-W41",
                mode="evidence",
                prompt_version="v1",
                generated_at=now,
            )
            session.add(rec_set_w41)
            session.flush()

            rec1 = Recommendation(
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
                status="open",
                text="Post a how-to on software applications.",
                is_ai_text=False,
            )
            session.add(rec1)
            session.commit()

            with patch("insight.report.call_gemini_wording") as mock_gemini:
                data = compute_weekly_report_data(session, week_key="2026-W40")
                mock_gemini.assert_not_called()
                self.assertEqual(len(data["next_plan"]), 1)
                plan_item = data["next_plan"][0]
                self.assertEqual(plan_item["rank"], 1)
                self.assertEqual(plan_item["topic"], "Software & apps")  # Humanized label
                self.assertEqual(plan_item["format"], "How-to")          # Humanized label
                self.assertEqual(plan_item["hook"], "Bold claim")        # Humanized label
                self.assertEqual(plan_item["action"], "Post a how-to on software applications.")


if __name__ == "__main__":
    unittest.main()

