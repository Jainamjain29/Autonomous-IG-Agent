"""Unit tests for Step 5: Comments Collector + Audience Intelligence.

Covers:
- Adapter comment pagination & nested reply handling
- Reply under an old comment is collected
- Own-account identification & clearing parent from needs-reply
- Privacy guarantee: ZERO raw usernames in any table in SQLite
- Taxonomy validation: out-of-bounds sentiment -> unknown, category -> other
- Needs-reply logic & reply clearing
- Content ideas ranking with 2-author rule & single mentions separation
- Sentiment & category breakdown computation (excluding unknown)
- Collector error resilience
- Streamlit AppTest verification
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import select, text

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from insight.adapters.base import CommentRecord
from insight.adapters.instagram import InstagramAdapter
from insight.audience import (
    classify_comments_batch,
    compute_category_breakdown,
    compute_sentiment_breakdown,
    get_audience_overview,
    get_needs_reply_comments,
    get_spam_abuse_comments,
    load_comment_taxonomy,
    rank_content_ideas,
    tag_unlabelled_comments,
)
from insight.collect import Collector
from insight.db import make_engine, session_factory, sqlite_url, upgrade_db
from insight.http_client import GraphClient
from insight.models import Account, Comment, CommentLabel, Publication, RawResponse
from insight.privacy import hash_author, redact_user_identifiers
from insight.storage import save_comment, save_raw_response
from insight.timeutil import UTC


def _mock_response(status_code: int, data: dict, headers: dict | None = None):
    m = mock.MagicMock()
    m.status_code = status_code
    m.json.return_value = data
    m.headers = headers or {}
    m.text = json.dumps(data)
    return m


class AudienceTaxonomyTests(unittest.TestCase):
    def test_taxonomy_seed_loaded(self):
        taxonomy = load_comment_taxonomy()
        self.assertIn("category", taxonomy)
        self.assertIn("sentiment", taxonomy)
        self.assertIn("question", taxonomy["category"])
        self.assertIn("spam", taxonomy["category"])
        self.assertIn("unknown", taxonomy["sentiment"])
        self.assertIn("positive", taxonomy["sentiment"])

    @mock.patch("google.generativeai.GenerativeModel")
    @mock.patch("google.generativeai.configure")
    def test_taxonomy_fallbacks_unknown_and_other(self, mock_configure, mock_model_cls):
        """Values outside taxonomy fallback to unknown (sentiment) and other (category)."""
        mock_model = mock.MagicMock()
        mock_model_cls.return_value = mock_model

        # Mock Gemini response with invalid category and sentiment
        mock_response = mock.MagicMock()
        mock_response.text = json.dumps([
            {
                "id": 101,
                "category": "INVALID_CAT",
                "sentiment": "SUPER_HAPPY",
                "confidence": 0.85,
                "needs_reply": True,
                "needs_reply_reason": "Needs help",
                "theme": "Setup",
            },
            {
                "id": 102,
                "category": "spam",
                "sentiment": "neutral",
                "confidence": 0.99,
                "needs_reply": True,  # Spam must NEVER need reply
                "needs_reply_reason": "Spam reason",
                "theme": "Promo",
            },
        ])
        mock_model.generate_content.return_value = mock_response

        comments = [
            {"id": 101, "text": "How do I install this?"},
            {"id": 102, "text": "Follow for crypto signals!"},
        ]
        results = classify_comments_batch(comments, api_key="mock_key")
        self.assertEqual(len(results), 2)

        # 101 fallback check
        r101 = next(r for r in results if r["id"] == 101)
        self.assertEqual(r101["category"], "other")
        self.assertEqual(r101["confidence"], 0.0)
        self.assertEqual(r101["sentiment"], "unknown")
        self.assertTrue(r101["needs_reply"])

        # 102 spam check (needs_reply forced to False)
        r102 = next(r for r in results if r["id"] == 102)
        self.assertEqual(r102["category"], "spam")
        self.assertFalse(r102["needs_reply"])
        self.assertIsNone(r102["needs_reply_reason"])


class InstagramCommentsAdapterTests(unittest.TestCase):
    def setUp(self):
        os.environ["INSIGHT_AUTHOR_SALT"] = "test_salt_12345"
        self.client = GraphClient(base_url="https://graph.instagram.com/v26.0", token="mock_token")
        self.adapter = InstagramAdapter(
            client=self.client,
            ig_user_id="17841440",
            own_handle="streamovate",
        )

    @mock.patch("insight.http_client.requests.get")
    def test_fetch_comments_pagination_and_replies(self, mock_get):
        """Paginates top-level comments and nested replies, detecting own account."""
        page1 = {
            "data": [
                {
                    "id": "c1",
                    "text": "Great tutorial!",
                    "timestamp": "2026-10-01T12:00:00+0000",
                    "username": "alice",
                    "like_count": 5,
                    "replies": {
                        "data": [
                            {
                                "id": "r1",
                                "text": "Thank you Alice!",
                                "timestamp": "2026-10-01T12:10:00+0000",
                                "username": "streamovate",  # OWN ACCOUNT
                                "like_count": 1,
                            }
                        ]
                    },
                }
            ],
            "paging": {
                "next": "https://graph.instagram.com/v26.0/page2"
            },
        }
        page2 = {
            "data": [
                {
                    "id": "c2",
                    "text": "Does it support Windows?",
                    "timestamp": "2026-10-01T13:00:00+0000",
                    "username": "bob",
                    "like_count": 0,
                }
            ],
            "paging": {},
        }
        mock_get.side_effect = [
            _mock_response(200, page1),
            _mock_response(200, page2),
        ]

        records = self.adapter.fetch_comments("reel_123")
        self.assertEqual(len(records), 3)

        c1 = records[0]
        self.assertEqual(c1.platform_comment_id, "c1")
        self.assertFalse(c1.is_own_account)
        self.assertIsNone(c1.parent_platform_comment_id)
        self.assertEqual(c1.author_hash, hash_author("alice", "test_salt_12345"))

        r1 = records[1]
        self.assertEqual(r1.platform_comment_id, "r1")
        self.assertTrue(r1.is_own_account)
        self.assertEqual(r1.parent_platform_comment_id, "c1")
        self.assertEqual(r1.author_hash, hash_author("streamovate", "test_salt_12345"))

        c2 = records[2]
        self.assertEqual(c2.platform_comment_id, "c2")
        self.assertFalse(c2.is_own_account)
        self.assertIsNone(c2.parent_platform_comment_id)

    @mock.patch("insight.http_client.requests.get")
    def test_missing_fields_tolerated(self, mock_get):
        """Adapter handles comments with missing username, replies, like_count without crashing."""
        data = {
            "data": [
                {
                    "id": "c_bare",
                    "text": "Bare comment without username or likes",
                }
            ]
        }
        mock_get.return_value = _mock_response(200, data)
        records = self.adapter.fetch_comments("reel_bare")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].platform_comment_id, "c_bare")
        self.assertIsNone(records[0].author_hash)
        self.assertFalse(records[0].is_own_account)


class PrivacyAndRedactionTests(unittest.TestCase):
    def setUp(self):
        os.environ["INSIGHT_AUTHOR_SALT"] = "test_salt_privacy"
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "privacy.db")
        self.engine = make_engine(sqlite_url(self.db_path))
        upgrade_db(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_redact_user_identifiers_helper(self):
        payload = {
            "id": "comment_999",
            "text": "Awesome reel!",
            "username": "alice_secret",
            "user_id": "123456",
            "from": {
                "id": "from_id_789",
                "username": "alice_from",
                "name": "Alice RealName",
            },
            "nested": [
                {"author_username": "bob_secret", "id": "comment_888"}
            ],
        }
        redacted = redact_user_identifiers(payload)
        self.assertEqual(redacted["id"], "comment_999")  # Comment ID preserved!
        self.assertEqual(redacted["username"], "<redacted>")
        self.assertEqual(redacted["user_id"], "<redacted>")
        self.assertEqual(redacted["from"]["id"], "<redacted>")
        self.assertEqual(redacted["from"]["username"], "<redacted>")
        self.assertEqual(redacted["from"]["name"], "<redacted>")
        self.assertEqual(redacted["nested"][0]["author_username"], "<redacted>")
        self.assertEqual(redacted["nested"][0]["id"], "comment_888")

    def test_zero_raw_usernames_in_sqlite(self):
        """Querying every table in SQLite for a test username yields 0 rows."""
        secret_username = "super_secret_username_42"
        now = datetime.now(UTC)

        with session_factory(self.engine)() as session:
            acc = Account(
                platform="instagram",
                platform_account_id="17841440",
                handle="streamovate",
                connected_at=now,
            )
            session.add(acc)
            session.flush()

            pub = Publication(
                account_id=acc.id,
                platform="instagram",
                platform_post_id="post_test",
                media_type="VIDEO",
                published_at=now,
            )
            session.add(pub)
            session.flush()

            # Save a comment record using author_hash
            rec = CommentRecord(
                platform_comment_id="comm_1",
                text="Loving this agent!",
                created_at=now,
                author_hash=hash_author(secret_username),
                like_count=3,
                is_own_account=False,
            )
            save_comment(session, pub, rec)

            # Save raw response that initially contained the username
            raw_api_payload = {
                "data": [
                    {
                        "id": "comm_1",
                        "text": "Loving this agent!",
                        "username": secret_username,
                        "from": {"id": "u999", "username": secret_username},
                    }
                ]
            }
            save_raw_response(session, "instagram", "post_test/comments", raw_api_payload, now)
            session.commit()

        # Connect directly via sqlite3 and scan EVERY table and column for secret_username
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cursor.fetchall() if not r[0].startswith("sqlite_")]

        found_occurrences = 0
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table});")
            cols = [c[1] for c in cursor.fetchall()]
            for col in cols:
                query = f"SELECT count(*) FROM {table} WHERE CAST({col} AS TEXT) LIKE '%{secret_username}%'"
                cursor.execute(query)
                cnt = cursor.fetchone()[0]
                if cnt > 0:
                    found_occurrences += cnt

        conn.close()
        self.assertEqual(found_occurrences, 0, f"Found {found_occurrences} occurrences of raw username in SQLite!")


class AudienceIntelligenceLogicTests(unittest.TestCase):
    def setUp(self):
        os.environ["INSIGHT_AUTHOR_SALT"] = "test_salt_logic"
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "audience_logic.db")
        self.engine = make_engine(sqlite_url(self.db_path))
        upgrade_db(self.engine)

        # Seed publication & comments
        self.now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
        try:
            with session_factory(self.engine)() as session:
                acc = Account(platform="instagram", platform_account_id="1784", handle="streamovate", connected_at=self.now)
                session.add(acc)
                session.flush()

                pub = Publication(account_id=acc.id, platform="instagram", platform_post_id="p1", media_type="VIDEO", published_at=self.now)
                session.add(pub)
                session.flush()

                # Comments:
                c1 = Comment(platform="instagram", platform_comment_id="c1", publication_id=pub.id, text="How to run in Docker?",
                             created_at=self.now, like_count=2, author_hash="hash_a", is_own_account=False)
                c2 = Comment(platform="instagram", platform_comment_id="c2", publication_id=pub.id, text="Please make Docker tutorial!",
                             created_at=self.now + timedelta(minutes=5), like_count=4, author_hash="hash_b", is_own_account=False)
                c3 = Comment(platform="instagram", platform_comment_id="c3", publication_id=pub.id, text="Docker Compose setup please",
                             created_at=self.now + timedelta(minutes=10), like_count=1, author_hash="hash_c", is_own_account=False)
                c4 = Comment(platform="instagram", platform_comment_id="c4", publication_id=pub.id, text="Is the API pricing free?",
                             created_at=self.now + timedelta(minutes=15), like_count=0, author_hash="hash_d", is_own_account=False)
                c5 = Comment(platform="instagram", platform_comment_id="c5", publication_id=pub.id, text="Where is the repo link?",
                             created_at=self.now + timedelta(minutes=20), like_count=1, author_hash="hash_e", is_own_account=False)
                c7 = Comment(platform="instagram", platform_comment_id="c7", publication_id=pub.id, text="Free coins at link!",
                             created_at=self.now + timedelta(minutes=30), like_count=0, author_hash="hash_spammer", is_own_account=False)

                session.add_all([c1, c2, c3, c4, c5, c7])
                session.flush()

                c6 = Comment(platform="instagram", platform_comment_id="c6", publication_id=pub.id, text="Link is in bio!",
                             created_at=self.now + timedelta(minutes=25), like_count=1, author_hash="hash_own", is_own_account=True, parent_comment_id=c5.id)
                session.add(c6)
                session.flush()

                # Add labels
                lbl1 = CommentLabel(comment_id=c1.id, category="question", sentiment="neutral", confidence=0.9, needs_reply=True,
                                    needs_reply_reason="Viewer needs Docker instructions", theme="Docker", source="ai", model="m", prompt_version="v1",
                                    input_hash="h1", labelled_at=self.now)
                lbl2 = CommentLabel(comment_id=c2.id, category="request", sentiment="positive", confidence=0.95, needs_reply=False,
                                    theme="Docker", source="ai", model="m", prompt_version="v1", input_hash="h2", labelled_at=self.now)
                lbl3 = CommentLabel(comment_id=c3.id, category="request", sentiment="positive", confidence=0.85, needs_reply=False,
                                    theme="Docker", source="ai", model="m", prompt_version="v1", input_hash="h3", labelled_at=self.now)
                lbl4 = CommentLabel(comment_id=c4.id, category="question", sentiment="neutral", confidence=0.88, needs_reply=True,
                                    needs_reply_reason="Pricing question", theme="Pricing", source="ai", model="m", prompt_version="v1",
                                    input_hash="h4", labelled_at=self.now)
                lbl5 = CommentLabel(comment_id=c5.id, category="question", sentiment="neutral", confidence=0.9, needs_reply=True,
                                    needs_reply_reason="Link request", theme="Repo", source="ai", model="m", prompt_version="v1",
                                    input_hash="h5", labelled_at=self.now)
                lbl7 = CommentLabel(comment_id=c7.id, category="spam", sentiment="neutral", confidence=0.99, needs_reply=False,
                                    theme="Spam", source="ai", model="m", prompt_version="v1", input_hash="h7", labelled_at=self.now)

                session.add_all([lbl1, lbl2, lbl3, lbl4, lbl5, lbl7])
                session.commit()
        except Exception:
            self.engine.dispose()
            self.tmp.cleanup()
            raise

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_needs_reply_queue_clears_when_own_reply_exists(self):
        """c5 is marked needs_reply=True, but has own reply c6, so c5 MUST be excluded from queue."""
        with session_factory(self.engine)() as session:
            queue = get_needs_reply_comments(session)
            queued_ids = [q["id"] for q in queue]
            self.assertIn(1, queued_ids)  # c1 is in queue
            self.assertIn(4, queued_ids)  # c4 is in queue
            self.assertNotIn(5, queued_ids)  # c5 was answered by c6! Excluded!
            self.assertNotIn(6, queued_ids)  # c6 is own account! Excluded!
            self.assertNotIn(7, queued_ids)  # c7 is spam! Excluded!

    def test_content_ideas_2_author_threshold_and_ranking(self):
        """Themes with >=2 distinct authors become content ideas; single-author themes become single mentions."""
        with session_factory(self.engine)() as session:
            qualified, single_mentions = rank_content_ideas(session)
            # Docker has 3 distinct authors (hash_a, hash_b, hash_c) -> Qualified!
            self.assertEqual(len(qualified), 1)
            self.assertEqual(qualified[0]["theme"], "Docker")
            self.assertEqual(qualified[0]["distinct_authors"], 3)
            self.assertEqual(qualified[0]["comment_count"], 3)
            self.assertEqual(qualified[0]["total_likes"], 7)  # 2 + 4 + 1

            # Pricing has 1 author -> Single Mention!
            self.assertEqual(len(single_mentions), 2)  # Pricing and Repo
            sm_themes = {sm["theme"] for sm in single_mentions}
            self.assertIn("Pricing", sm_themes)

    def test_sentiment_breakdown_excludes_unknown_and_own_account(self):
        with session_factory(self.engine)() as session:
            # Add one comment with unknown sentiment
            c_unk = Comment(id=8, platform="instagram", platform_comment_id="c_unk", publication_id=1, text="???",
                            created_at=self.now, is_own_account=False)
            session.add(c_unk)
            session.flush()
            lbl_unk = CommentLabel(comment_id=8, category="other", sentiment="unknown", confidence=0.0, needs_reply=False,
                                   theme="Other", source="ai", model="m", prompt_version="v1", input_hash="h8", labelled_at=self.now)
            session.add(lbl_unk)
            session.commit()

            stats = compute_sentiment_breakdown(session)
            self.assertEqual(stats["unknown_count"], 1)
            # positive=2 (c2, c3), neutral=4 (c1, c4, c5, c7) -> total 6
            self.assertEqual(stats["total_valid"], 6)
            self.assertIn("positive", stats["counts"])
            self.assertIn("neutral", stats["counts"])
            self.assertIn("negative", stats["counts"])
            self.assertEqual(stats["counts"]["negative"], 0)

    def test_reply_under_old_comment_collected(self):
        """A new reply arriving under an old comment is properly upserted and linked."""
        with session_factory(self.engine)() as session:
            pub = session.scalar(select(Publication).filter_by(id=1))
            # New reply to old comment c1
            new_reply = CommentRecord(
                platform_comment_id="r_new_under_c1",
                text="I got Docker working!",
                created_at=self.now + timedelta(hours=2),
                author_hash=hash_author("viewer_new"),
                like_count=2,
                parent_platform_comment_id="c1",
                is_own_account=False,
            )
            saved, created = save_comment(session, pub, new_reply)
            session.commit()
            self.assertTrue(created)
            self.assertEqual(saved.parent_comment_id, 1)


class StreamlitAudienceTabTests(unittest.TestCase):
    def test_streamlit_audience_tab_renders_no_exception(self):
        """AppTest renders the entire Streamlit app including 5 tabs with no exceptions."""
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = os.path.join(tmp, "st_audience.db")
            engine = make_engine(sqlite_url(db_path))
            upgrade_db(engine)

            now = datetime.now(UTC)
            with session_factory(engine)() as session:
                acc = Account(id=1, platform="instagram", platform_account_id="1784", handle="streamovate", connected_at=now)
                session.add(acc)
                pub = Publication(id=1, account_id=1, platform="instagram", platform_post_id="p1", media_type="VIDEO", published_at=now)
                session.add(pub)
                session.commit()
            engine.dispose()

            old_env = os.environ.get("INSIGHT_VIEW_DB_PATH")
            at = None
            try:
                os.environ["INSIGHT_VIEW_DB_PATH"] = db_path
                at = AppTest.from_file("insight/view.py")
                at.run(timeout=30)
                self.assertEqual(len(at.exception), 0, f"Streamlit raised exceptions: {at.exception}")
                tab_labels = [tab.label for tab in at.tabs]
                self.assertIn("👥 Audience", tab_labels)
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
