"""Insight Agent storage: schema, uniqueness, idempotent writers, UTC handling.

Run from the repo root: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from insight import storage
from insight.adapters.base import CommentRecord, MetricResult, PublicationRecord
from insight.db import init_db, make_engine, session_factory
from insight.models import Comment, MetricDefinition, MetricSnapshot, MetricValue, Publication, RawResponse
from insight.timeutil import UTC

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def result(values=None, missing=None, at=T0):
    return MetricResult(endpoint="x/insights", fetched_at=at, raw_payload={"data": []},
                        values=values if values is not None else {"views": 10.0}, missing=missing or {})


class InsightDBTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = init_db(make_engine("sqlite://"))
        self.session = session_factory(self.engine)()
        self.account = storage.get_or_create_account(self.session, "instagram", "A1", "me", T0)
        self.pub = storage.upsert_publication(self.session, self.account, PublicationRecord(
            platform_post_id="P1", platform_account_id="A1", media_type="reel", published_at=T0))

    def tearDown(self):
        self.session.close()
        self.engine.dispose()


class SchemaTests(InsightDBTestCase):
    def test_all_tables_created(self):
        tables = set(inspect(self.engine).get_table_names())
        self.assertEqual(tables, {"accounts", "publications", "metric_definitions", "metric_snapshots",
                                  "metric_values", "raw_responses", "comments", "post_tags"})

    def test_seed_loaded_and_idempotent(self):
        count = self.session.query(MetricDefinition).count()
        self.assertGreater(count, 10)
        init_db(self.engine)
        self.assertEqual(self.session.query(MetricDefinition).count(), count)

    def test_snapshot_must_have_exactly_one_subject(self):
        self.session.add(MetricSnapshot(subject_type="publication", subject_id=self.pub.id,
                                        publication_id=self.pub.id, account_id=self.account.id,
                                        checkpoint="1h", period_key="1h", collected_at=T0, completeness="complete"))
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_snapshot_subject_must_match_fk(self):
        self.session.add(MetricSnapshot(subject_type="account", subject_id=self.account.id,
                                        publication_id=self.pub.id, checkpoint="adhoc", period_key="k",
                                        collected_at=T0, completeness="complete"))
        with self.assertRaises(IntegrityError):
            self.session.flush()


class UniquenessTests(InsightDBTestCase):
    def _raw_account_snapshot(self):
        return MetricSnapshot(subject_type="account", subject_id=self.account.id, account_id=self.account.id,
                              checkpoint="daily", period_key="2026-09-01", collected_at=T0, completeness="complete")

    def test_duplicate_account_snapshot_rejected_by_db(self):
        # publication_id is NULL here: the old (publication_id, account_id, checkpoint) key
        # would NOT have caught this, because NULLs are distinct in unique constraints.
        self.session.add(self._raw_account_snapshot())
        self.session.flush()
        self.session.add(self._raw_account_snapshot())
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_duplicate_publication_snapshot_rejected_by_db(self):
        for _ in range(2):
            self.session.add(MetricSnapshot(subject_type="publication", subject_id=self.pub.id,
                                            publication_id=self.pub.id, checkpoint="24h", period_key="24h",
                                            collected_at=T0, completeness="complete"))
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_save_snapshot_is_idempotent(self):
        snap, created = storage.save_snapshot(self.session, publication=self.pub, checkpoint="1h",
                                              collected_at=T0 + timedelta(hours=1), result=result())
        again, created_again = storage.save_snapshot(self.session, publication=self.pub, checkpoint="1h",
                                                     collected_at=T0 + timedelta(hours=1, minutes=5), result=result())
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(snap.id, again.id)
        self.assertEqual(self.session.query(MetricSnapshot).count(), 1)
        self.assertEqual(self.session.query(MetricValue).count(), 1)
        self.assertEqual(self.session.query(RawResponse).count(), 1)

    def test_daily_period_key_is_utc_date(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        # 02:00 IST on Sep 2 is still Sep 1 in UTC.
        snap, _ = storage.save_snapshot(self.session, account=self.account, checkpoint="daily",
                                        collected_at=datetime(2026, 9, 2, 2, 0, tzinfo=ist), result=result())
        self.assertEqual(snap.period_key, "2026-09-01")
        self.assertIsNone(snap.time_since_publish_seconds)
        _, created = storage.save_snapshot(self.session, account=self.account, checkpoint="daily",
                                           collected_at=datetime(2026, 9, 1, 23, 0, tzinfo=UTC), result=result())
        self.assertFalse(created)
        _, created = storage.save_snapshot(self.session, account=self.account, checkpoint="daily",
                                           collected_at=datetime(2026, 9, 2, 0, 1, tzinfo=UTC), result=result())
        self.assertTrue(created)

    def test_adhoc_period_key_is_timestamp(self):
        a, _ = storage.save_snapshot(self.session, publication=self.pub, checkpoint="adhoc",
                                     collected_at=T0 + timedelta(hours=3), result=result())
        _, created = storage.save_snapshot(self.session, publication=self.pub, checkpoint="adhoc",
                                           collected_at=T0 + timedelta(hours=4), result=result())
        self.assertEqual(a.period_key, (T0 + timedelta(hours=3)).isoformat())
        self.assertTrue(created)

    def test_post_checkpoint_rejected_for_account(self):
        with self.assertRaises(ValueError):
            storage.save_snapshot(self.session, account=self.account, checkpoint="24h", collected_at=T0)

    def test_duplicate_metric_value_rejected(self):
        snap, _ = storage.save_snapshot(self.session, publication=self.pub, checkpoint="1h",
                                        collected_at=T0, result=result())
        self.session.add(MetricValue(snapshot_id=snap.id, canonical_metric="views", value=99))
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_comment_dedup_and_parent_link(self):
        parent = CommentRecord("C1", "hi", T0, "h" * 64)
        reply = CommentRecord("C2", "re", T0 + timedelta(minutes=1), "g" * 64, parent_platform_comment_id="C1")
        c1, _ = storage.save_comment(self.session, self.pub, parent)
        c2, _ = storage.save_comment(self.session, self.pub, reply)
        _, created = storage.save_comment(self.session, self.pub, parent)
        self.assertFalse(created)
        self.assertEqual(c2.parent_comment_id, c1.id)
        self.assertEqual(self.session.query(Comment).count(), 2)

    def test_duplicate_publication_rejected_by_db(self):
        self.session.add(Publication(platform="instagram", platform_post_id="P1", account_id=self.account.id,
                                     media_type="reel", published_at=T0))
        with self.assertRaises(IntegrityError):
            self.session.flush()


class CompletenessTests(InsightDBTestCase):
    def test_missing_metrics_stored_as_null_with_reason(self):
        snap, _ = storage.save_snapshot(self.session, publication=self.pub, checkpoint="7d", collected_at=T0,
                                        result=result({"views": 5.0}, {"saves": "not in response"}))
        self.assertEqual(snap.completeness, "partial")
        missing = {v.canonical_metric: (v.value, v.missing_reason) for v in snap.values}
        self.assertEqual(missing["saves"], (None, "not in response"))

    def test_no_result_is_unavailable(self):
        snap, _ = storage.save_snapshot(self.session, publication=self.pub, checkpoint="1h", collected_at=T0)
        self.assertEqual(snap.completeness, "unavailable")
        self.assertEqual(snap.values, [])

    def test_delayed(self):
        snap, _ = storage.save_snapshot(self.session, publication=self.pub, checkpoint="24h",
                                        collected_at=T0 + timedelta(hours=30), result=result(), delayed=True)
        self.assertEqual(snap.completeness, "delayed")
        self.assertEqual(snap.time_since_publish_seconds, 30 * 3600)

    def test_raw_response_tokens_stripped(self):
        r = MetricResult("x", T0, {"access_token": "SECRET", "paging": {"next": "https://g/x?access_token=SECRET&a=1"}})
        r.values = {"views": 1.0}
        snap, _ = storage.save_snapshot(self.session, publication=self.pub, checkpoint="1h", collected_at=T0, result=r)
        self.assertNotIn("SECRET", str(snap.raw_response.payload))


class UTCTests(InsightDBTestCase):
    def test_aware_datetimes_round_trip_as_utc(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        local = datetime(2026, 9, 1, 15, 30, tzinfo=ist)
        snap, _ = storage.save_snapshot(self.session, publication=self.pub, checkpoint="adhoc",
                                        collected_at=local, result=result())
        self.session.commit()
        stored = self.session.execute(text("SELECT collected_at FROM metric_snapshots")).scalar()
        self.assertTrue(str(stored).startswith("2026-09-01 10:00:00"))

        fresh = session_factory(self.engine)()
        loaded = fresh.scalar(select(MetricSnapshot))
        fresh.close()
        self.assertEqual(loaded.collected_at.tzinfo, UTC)
        self.assertEqual(loaded.collected_at, local)
        self.assertEqual(loaded.collected_at.hour, 10)

    def test_naive_datetime_rejected(self):
        self.session.add(RawResponse(platform="instagram", endpoint="x", fetched_at=datetime(2026, 9, 1), payload={}))
        with self.assertRaises(Exception) as ctx:
            self.session.flush()
        self.assertIn("naive datetime", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
