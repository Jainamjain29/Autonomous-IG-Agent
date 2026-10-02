"""Tests for insight/probe.py -- mocked HTTP, no real API calls.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests as _requests_lib

from insight.http_client import (
    MAX_RETRIES,
    CallCapReached,
    GraphClient,
)
from insight.probe import (
    CALL_CAP,
    Finding,
    ProbeRunner,
    _extract_value,
)
from insight.privacy import redact_secrets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resp(status=200, data=None, headers=None):
    """Build a mock ``requests.Response``."""
    r = mock.Mock(status_code=status)
    d = data if data is not None else {"data": []}
    r.json.return_value = d
    r.text = json.dumps(d)
    r.headers = headers or {}
    return r


def _err(status=400, msg="bad metric", headers=None):
    return _resp(status, {"error": {"message": msg}}, headers=headers)


def _insights(metrics: dict):
    """Successful ``/{id}/insights`` with *metrics* ``{name: value}``."""
    items = [{"name": k, "period": "lifetime", "values": [{"value": v}]}
             for k, v in metrics.items()]
    return _resp(200, {"data": items})


def _total_value_insights(metrics: dict):
    """Account insights using the ``total_value`` response shape."""
    items = [{"name": k, "period": "day", "total_value": {"value": v}}
             for k, v in metrics.items()]
    return _resp(200, {"data": items})


_SETTINGS = {
    "GRAPH_HOST": "graph.instagram.com",
    "GRAPH_VERSION": "v22.0",
    "META_ACCESS_TOKEN": "EAABtest_token_123",
    "IG_USER_ID": "12345",
}


def _setting(key):
    return _SETTINGS.get(key, "")


_ACCOUNT_OK = _resp(200, {"id": "12345", "username": "tester",
                           "media_count": 5, "followers_count": 100})

_MEDIA_2_REELS = _resp(200, {"data": [
    {"id": "R1", "media_product_type": "REELS", "media_type": "VIDEO",
     "timestamp": "2026-10-01T12:00:00+0000"},
    {"id": "R2", "media_product_type": "REELS", "media_type": "VIDEO",
     "timestamp": "2026-09-30T12:00:00+0000"},
]})

_REEL_ALL_OK = _insights({
    "views": 500, "reach": 300, "likes": 20, "comments": 5,
    "shares": 3, "saved": 7, "total_interactions": 35,
    "ig_reels_avg_watch_time": 8000,
    "ig_reels_video_view_total_time": 4_000_000,
})

_ACCT_INSIGHTS_OK = _total_value_insights({
    "profile_views": 10, "reach": 50, "views": 200,
})


# =========================================================================
# GraphClient: retry and call-cap tests
# =========================================================================

class RetryTests(unittest.TestCase):

    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    def test_5xx_retried_up_to_3_times(self, mock_get, mock_sleep):
        mock_get.return_value = _err(500, "Internal Server Error")
        c = GraphClient("https://g/v1", "tok")
        status, _ = c.get("x")
        self.assertEqual(mock_get.call_count, MAX_RETRIES + 1)  # 4
        self.assertEqual(c.call_count, MAX_RETRIES + 1)
        self.assertEqual(status, 500)
        self.assertEqual(mock_sleep.call_count, MAX_RETRIES)

    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    def test_4xx_not_retried(self, mock_get, mock_sleep):
        mock_get.return_value = _err(400)
        c = GraphClient("https://g/v1", "tok")
        status, _ = c.get("x")
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(c.call_count, 1)
        self.assertEqual(status, 400)
        mock_sleep.assert_not_called()

    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    def test_timeout_retried_then_succeeds(self, mock_get, mock_sleep):
        mock_get.side_effect = [_requests_lib.Timeout("t/o"), _resp()]
        c = GraphClient("https://g/v1", "tok")
        status, _ = c.get("x")
        self.assertEqual(c.call_count, 2)
        self.assertEqual(status, 200)

    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    def test_5xx_then_success(self, mock_get, mock_sleep):
        mock_get.side_effect = [_err(500), _err(500), _resp()]
        c = GraphClient("https://g/v1", "tok")
        status, _ = c.get("x")
        self.assertEqual(status, 200)
        self.assertEqual(c.call_count, 3)


class CallCapTests(unittest.TestCase):

    @mock.patch("insight.http_client.requests.get")
    def test_cap_stops_at_limit(self, mock_get):
        mock_get.return_value = _resp()
        c = GraphClient("https://g/v1", "tok", call_cap=CALL_CAP)
        for i in range(CALL_CAP):
            c.get(f"p{i}")
        self.assertEqual(c.call_count, CALL_CAP)
        with self.assertRaises(CallCapReached):
            c.get("overflow")
        # call_count stays at CALL_CAP -- the overflow attempt was blocked
        self.assertEqual(c.call_count, CALL_CAP)

    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    def test_retries_count_toward_cap(self, mock_get, mock_sleep):
        """Every HTTP attempt -- including retries -- counts toward the cap."""
        mock_get.return_value = _err(500)
        c = GraphClient("https://g/v1", "tok", call_cap=CALL_CAP)
        # Each get() burns 4 attempts (1 + 3 retries). 10 * 4 = 40.
        for i in range(10):
            c.get(f"p{i}")
        self.assertEqual(c.call_count, CALL_CAP)
        with self.assertRaises(CallCapReached):
            c.get("overflow")


# =========================================================================
# Batch-then-single fallback
# =========================================================================

class FallbackTests(unittest.TestCase):

    @mock.patch.object(ProbeRunner, "_save_responses")
    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    @mock.patch("insight.probe.db.get_setting", side_effect=_setting)
    def test_batch_success_skips_single_calls(self, _s, mock_get, _sl, _sv):
        mock_get.side_effect = [
            _ACCOUNT_OK, _MEDIA_2_REELS, _REEL_ALL_OK, _ACCT_INSIGHTS_OK,
        ]
        runner = ProbeRunner()
        runner.run()
        # 4 API calls total: info + list + reel batch + account batch
        self.assertEqual(runner.client.call_count, 4)
        self.assertTrue(
            all(f.status == "OK" for f in runner.findings),
            [f"{f.canonical_name}={f.status}" for f in runner.findings])

    @mock.patch.object(ProbeRunner, "_save_responses")
    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    @mock.patch("insight.probe.db.get_setting", side_effect=_setting)
    def test_batch_fail_triggers_single_fallback(self, _s, mock_get, _sl, _sv):
        singles = [
            _insights({"views": 100}),
            _insights({"reach": 80}),
            _insights({"likes": 10}),
            _insights({"comments": 2}),
            _insights({"shares": 1}),
            _insights({"saved": 4}),
            _insights({"total_interactions": 17}),
            _err(400, "(#100) Invalid metric ig_reels_avg_watch_time"),
            _err(400, "(#100) Invalid metric ig_reels_video_view_total_time"),
        ]
        mock_get.side_effect = [
            _ACCOUNT_OK,
            _MEDIA_2_REELS,
            _err(400, "(#100) Param metric[0] must be one of ..."),
            *singles,
            _ACCT_INSIGHTS_OK,
        ]
        runner = ProbeRunner()
        runner.run()
        # 2 (info+list) + 1 (batch) + 9 (individual) + 1 (account batch)
        self.assertEqual(runner.client.call_count, 13)
        invalid = [f for f in runner.findings if f.status == "INVALID"]
        self.assertEqual(len(invalid), 2)
        self.assertTrue(all("Invalid metric" in f.error for f in invalid))

    @mock.patch.object(ProbeRunner, "_save_responses")
    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    @mock.patch("insight.probe.db.get_setting", side_effect=_setting)
    def test_single_call_invalid_records_error_msg(self, _s, mock_get, _sl, _sv):
        mock_get.side_effect = [
            _ACCOUNT_OK,
            _MEDIA_2_REELS,
            _err(400, "batch fail"),             # batch fails
            _err(400, "(#100) 'views' not valid for this media type"),
            *[_insights({m: 1}) for m in
              ["reach", "likes", "comments", "shares", "saved",
               "total_interactions", "ig_reels_avg_watch_time",
               "ig_reels_video_view_total_time"]],
            _ACCT_INSIGHTS_OK,
        ]
        runner = ProbeRunner()
        runner.run()
        inv = [f for f in runner.findings
               if f.status == "INVALID" and f.applies_to == "reel"]
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0].canonical_name, "views")
        self.assertIn("not valid", inv[0].error)


# =========================================================================
# Token stripping (reuses insight.privacy.redact_secrets)
# =========================================================================

class TokenTests(unittest.TestCase):

    def test_paging_url_tokens_stripped(self):
        """Realistic paging.next URL has access_token stripped."""
        payload = {
            "data": [{"name": "reach", "values": [{"value": 42}]}],
            "paging": {
                "previous": (
                    "https://graph.instagram.com/v22.0/123/insights"
                    "?metric=reach&access_token=EAABwzLong_Token_Value"
                    "&pretty=0&since=1727740800&until=1727827200"
                ),
                "next": (
                    "https://graph.instagram.com/v22.0/123/insights"
                    "?access_token=EAABwzLong_Token_Value&metric=reach"
                    "&pretty=0&since=1727913600&until=1728000000"
                ),
            },
        }
        r = redact_secrets(payload)
        blob = json.dumps(r)
        self.assertNotIn("EAABwzLong_Token_Value", blob)
        self.assertIn("access_token=REDACTED", r["paging"]["next"])
        self.assertIn("access_token=REDACTED", r["paging"]["previous"])
        # Data is preserved
        self.assertEqual(r["data"][0]["values"][0]["value"], 42)

    def test_saved_files_have_tokens_stripped(self):
        """End-to-end: saved JSON files have no tokens in paging URLs."""
        runner = ProbeRunner.__new__(ProbeRunner)
        runner.client = mock.Mock()
        runner.client.raw_responses = [
            ("paging_test", {
                "status_code": 200, "path": "12345/insights",
                "data": {"paging": {
                    "next": "https://g.com?access_token=SECRET_XYZ&x=1",
                }},
            }),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            probe_dir = runner._save_responses(base_dir=tmp)
            files = os.listdir(probe_dir)
            self.assertEqual(len(files), 1)
            with open(os.path.join(probe_dir, files[0])) as f:
                saved = json.load(f)
        blob = json.dumps(saved)
        self.assertNotIn("SECRET_XYZ", blob)
        self.assertIn("REDACTED", blob)


# =========================================================================
# Value interpretation: OK vs NO_DATA
# =========================================================================

class ValueTests(unittest.TestCase):

    def _runner(self):
        r = ProbeRunner.__new__(ProbeRunner)
        r.findings = []
        r.undiscovered = {}
        return r

    def test_zero_is_ok(self):
        """A metric returning 0 is OK, not NO_DATA."""
        r = self._runner()
        m = SimpleNamespace(canonical_name="likes", platform_metric_name="likes")
        r._parse_insights(
            {"data": [{"name": "likes", "period": "lifetime",
                       "values": [{"value": 0}]}]}, [m], "reel")
        self.assertEqual(r.findings[0].status, "OK")
        self.assertEqual(r.findings[0].sample_value, 0)

    def test_null_is_no_data(self):
        """NO_DATA means accepted by the API but no value present."""
        r = self._runner()
        m = SimpleNamespace(canonical_name="likes", platform_metric_name="likes")
        r._parse_insights(
            {"data": [{"name": "likes", "period": "lifetime",
                       "values": [{"value": None}]}]}, [m], "reel")
        self.assertEqual(r.findings[0].status, "NO_DATA")

    def test_missing_metric_is_no_data(self):
        r = self._runner()
        m = SimpleNamespace(canonical_name="likes", platform_metric_name="likes")
        r._parse_insights({"data": []}, [m], "reel")
        self.assertEqual(r.findings[0].status, "NO_DATA")

    def test_undiscovered_metrics_reported(self):
        r = self._runner()
        m = SimpleNamespace(canonical_name="views", platform_metric_name="views")
        r._parse_insights({"data": [
            {"name": "views", "values": [{"value": 100}]},
            {"name": "brand_new", "values": [{"value": 42}]},
        ]}, [m], "reel")
        self.assertIn("brand_new", r.undiscovered)
        self.assertEqual(r.undiscovered["brand_new"], 42)


# =========================================================================
# Integration-level tests
# =========================================================================

class IntegrationTests(unittest.TestCase):

    @mock.patch.object(ProbeRunner, "_save_responses")
    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    @mock.patch("insight.probe.db.get_setting", side_effect=_setting)
    def test_followers_from_user_field(self, _s, mock_get, _sl, _sv):
        mock_get.side_effect = [
            _resp(200, {"id": "12345", "username": "t",
                        "media_count": 0, "followers_count": 999}),
            _resp(200, {"data": []}),          # no media
            _ACCT_INSIGHTS_OK,
        ]
        runner = ProbeRunner()
        runner.run()
        fc = [f for f in runner.findings if f.canonical_name == "followers"]
        self.assertEqual(len(fc), 1)
        self.assertEqual(fc[0].status, "OK")
        self.assertEqual(fc[0].sample_value, 999)
        self.assertEqual(fc[0].platform_name, "followers_count")

    @mock.patch.object(ProbeRunner, "_save_responses")
    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    @mock.patch("insight.probe.db.get_setting", side_effect=_setting)
    def test_one_reel_does_not_fail(self, _s, mock_get, _sl, _sv):
        mock_get.side_effect = [
            _ACCOUNT_OK,
            _resp(200, {"data": [
                {"id": "R1", "media_product_type": "REELS",
                 "media_type": "VIDEO",
                 "timestamp": "2026-10-01T12:00:00+0000"},
                {"id": "I1", "media_product_type": "FEED",
                 "media_type": "IMAGE",
                 "timestamp": "2026-09-30T12:00:00+0000"},
            ]}),
            _REEL_ALL_OK,
            _ACCT_INSIGHTS_OK,
        ]
        runner = ProbeRunner()
        runner.run()                       # must not raise
        self.assertEqual(runner.reel_count, 1)

    @mock.patch.object(ProbeRunner, "_save_responses")
    @mock.patch("insight.http_client.time.sleep")
    @mock.patch("insight.http_client.requests.get")
    @mock.patch("insight.probe.db.get_setting", side_effect=_setting)
    def test_zero_reels_does_not_fail(self, _s, mock_get, _sl, _sv):
        mock_get.side_effect = [
            _ACCOUNT_OK,
            _resp(200, {"data": [
                {"id": "I1", "media_product_type": "FEED",
                 "media_type": "IMAGE",
                 "timestamp": "2026-10-01T12:00:00+0000"},
            ]}),
            _ACCT_INSIGHTS_OK,
        ]
        runner = ProbeRunner()
        runner.run()
        self.assertEqual(runner.reel_count, 0)
        reel_f = [f for f in runner.findings if f.applies_to == "reel"]
        self.assertTrue(all(f.status == "NOT_TRIED" for f in reel_f))
        self.assertTrue(all("no reels" in f.error for f in reel_f))


if __name__ == "__main__":
    unittest.main()
