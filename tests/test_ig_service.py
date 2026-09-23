"""Offline tests for ig_service: the Graph API, settings and clock are all faked.

Run from the repo root: python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ig_service as ig

TOKEN = "SECRETTOK"
UPLOAD_URI = "https://rupload.facebook.com/ig-api-upload/v26.0/C1"


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def ok(payload):
    return FakeResponse(200, payload)


CREATED = ok({"id": "C1", "uri": UPLOAD_URI})
UPLOADED = ok({"success": True})
IN_PROGRESS = ok({"id": "C1", "status_code": "IN_PROGRESS", "status": "In Progress"})
FINISHED = ok({"id": "C1", "status_code": "FINISHED", "status": "Finished"})
PUBLISHED = ok({"id": "M1"})
SERVER_ERROR = FakeResponse(500, {"error": {"message": "An unexpected error has occurred", "code": 2}})


class IGServiceTest(unittest.TestCase):
    def setUp(self):
        self.settings = {"META_ACCESS_TOKEN": TOKEN, "IG_USER_ID": "1784"}
        self.calls = []
        self.clock = 0.0
        self.script = []

        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(b"\0" * 2048)
        tmp.close()
        self.video = tmp.name
        self.addCleanup(os.remove, self.video)

        for target, fake in [
            (mock.patch.object(ig.db, "get_setting"), lambda k: self.settings.get(k, "")),
            (mock.patch.object(ig.requests, "request"), self._fake_request),
            (mock.patch.object(ig.time, "sleep"), self._fake_sleep),
            (mock.patch.object(ig.time, "monotonic"), lambda: self.clock),
        ]:
            target.start().side_effect = fake
            self.addCleanup(target.stop)
        # Keep test output quiet.
        log = mock.patch.object(ig, "_log")
        log.start()
        self.addCleanup(log.stop)

    def _fake_request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def _fake_sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    sleeps = None

    def run_script(self, script, fn):
        self.script = list(script)
        self.sleeps = []
        return fn()

    def assert_raises_ig(self, fragment, script, fn):
        with self.assertRaises(ig.IGPublishError) as ctx:
            self.run_script(script, fn)
        err = ctx.exception
        self.assertIn(fragment, str(err))
        self.assertNotIn(TOKEN, str(err))
        self.assertNotIn(TOKEN, json.dumps(err.response_json or {}))
        return err

    def polls(self):
        return [c for c in self.calls if c[0] == "GET"]

    # --- resumable publish ---

    def test_full_resumable_publish(self):
        media_id = self.run_script([CREATED, UPLOADED, IN_PROGRESS, FINISHED, PUBLISHED],
                                   lambda: ig.publish_reel_local(self.video, "cap"))
        self.assertEqual(media_id, "M1")

        _, url, kw = self.calls[0]
        self.assertEqual(url, "https://graph.facebook.com/v26.0/1784/media")
        self.assertEqual(kw["data"]["media_type"], "REELS")
        self.assertEqual(kw["data"]["upload_type"], "resumable")
        self.assertNotIn("video_url", kw["data"])

        _, url, kw = self.calls[1]
        self.assertEqual(url, UPLOAD_URI)
        self.assertEqual(kw["headers"], {"Authorization": f"OAuth {TOKEN}", "offset": "0", "file_size": "2048"})
        self.assertEqual(self.calls[2][2]["params"]["fields"], "status_code,status")
        self.assertTrue(self.calls[4][1].endswith("/1784/media_publish"))
        self.assertEqual(self.calls[4][2]["data"]["creation_id"], "C1")

    def test_timeouts_upload_uses_10_120_everything_else_30(self):
        self.run_script([CREATED, UPLOADED, FINISHED, PUBLISHED], lambda: ig.publish_reel_local(self.video, "cap"))
        timeouts = [(url, kw["timeout"]) for _, url, kw in self.calls]
        self.assertEqual(timeouts[1], (UPLOAD_URI, (10, 120)))
        for url, timeout in timeouts[:1] + timeouts[2:]:
            self.assertEqual(timeout, 30, url)

    def test_dry_run_skips_media_publish(self):
        container_id = self.run_script([CREATED, UPLOADED, FINISHED],
                                       lambda: ig.publish_reel_local(self.video, "cap", publish=False))
        self.assertEqual(container_id, "C1")
        self.assertFalse(any("media_publish" in url for _, url, _ in self.calls))

    def test_missing_file(self):
        self.assert_raises_ig("not found", [], lambda: ig.publish_reel_local("nope.mp4", "cap"))
        self.assertEqual(self.calls, [])

    def test_http_400_raises_with_meta_message(self):
        err = self.assert_raises_ig(
            "HTTP 400: Invalid OAuth access token",
            [FakeResponse(400, {"error": {"message": "Invalid OAuth access token", "code": 190}})],
            lambda: ig.publish_reel_local(self.video, "cap"))
        self.assertEqual(err.status_code, 400)
        self.assertFalse(err.transient)

    def test_non_json_502(self):
        err = self.assert_raises_ig("HTTP 502", [FakeResponse(502, None)],
                                    lambda: ig.publish_reel_local(self.video, "cap"))
        self.assertTrue(err.transient)

    def test_network_error_redacts_token(self):
        self.assert_raises_ig("access_token=***",
                              [requests.ConnectionError(f"failed for ...?access_token={TOKEN}")],
                              lambda: ig.publish_reel_local(self.video, "cap"))

    def test_upload_failure(self):
        self.assert_raises_ig(
            "Upload failed: ProcessingFailedError",
            [CREATED, ok({"debug_info": {"retriable": False, "message": "ProcessingFailedError"}})],
            lambda: ig.publish_reel_local(self.video, "cap"))

    def test_missing_uri_builds_documented_upload_url(self):
        self.settings["GRAPH_HOST"] = "graph.instagram.com"
        self.run_script([ok({"id": "C3"}), UPLOADED, FINISHED, PUBLISHED],
                        lambda: ig.publish_reel_local(self.video, "cap"))
        self.assertEqual(self.calls[0][1], "https://graph.instagram.com/v26.0/1784/media")
        self.assertEqual(self.calls[1][1], "https://rupload.facebook.com/ig-api-upload/v26.0/C3")

    def test_errors_are_tagged_with_failing_step(self):
        bad = FakeResponse(400, {"error": {"message": "Bad request"}})
        cases = {
            "container": [bad],
            "upload": [CREATED, ok({"debug_info": {"message": "ProcessingFailedError"}})],
            "poll": [CREATED, UPLOADED, ok({"id": "C1", "status_code": "ERROR", "status": "Error: 2207026"})],
            "publish": [CREATED, UPLOADED, FINISHED, bad],
        }
        for step, script in cases.items():
            with self.subTest(step=step):
                err = self.assert_raises_ig("", script, lambda: ig.publish_reel_local(self.video, "cap"))
                self.assertEqual(err.step, step)

    def test_ngrok_path_errors_are_tagged_with_failing_step(self):
        bad = FakeResponse(400, {"error": {"message": "Bad request"}})
        for step, script in {"container": [bad], "poll": [ok({"id": "C2"}), SERVER_ERROR] + [SERVER_ERROR] * 3,
                             "publish": [ok({"id": "C2"}), FINISHED, bad]}.items():
            with self.subTest(step=step):
                err = self.assert_raises_ig("", script, lambda: ig.publish_reel("https://x/v.mp4", "cap"))
                self.assertEqual(err.step, step)

    def test_validation_errors_have_no_step(self):
        err = self.assert_raises_ig("not found", [], lambda: ig.publish_reel_local("nope.mp4", "cap"))
        self.assertIsNone(err.step)

    # --- polling ---

    def test_container_error_raises_immediately(self):
        err = self.assert_raises_ig(
            "Container ERROR: Error: 2207026",
            [CREATED, UPLOADED, ok({"id": "C1", "status_code": "ERROR", "status": "Error: 2207026"})],
            lambda: ig.publish_reel_local(self.video, "cap"))
        self.assertEqual(err.response_json["status_code"], "ERROR")

    def test_container_expired_raises_immediately(self):
        self.assert_raises_ig("Container EXPIRED", [ok({"id": "C1", "status_code": "EXPIRED"})],
                              lambda: ig.wait_for_container("C1"))

    def test_poll_timeout_after_10_minutes(self):
        self.assert_raises_ig("Timed out after 600s", [IN_PROGRESS] * 100, lambda: ig.wait_for_container("C1"))
        self.assertEqual(len(self.polls()), 61)  # t = 0, 10, ..., 600
        self.assertEqual(self.clock, 600)

    def test_poll_retries_transient_failures_with_backoff(self):
        script = [requests.ConnectionError("reset"), requests.Timeout("read timed out"), SERVER_ERROR, FINISHED]
        result = self.run_script(script, lambda: ig.wait_for_container("C1"))
        self.assertEqual(result["status_code"], "FINISHED")
        self.assertEqual(self.sleeps, [10, 20, 40])

    def test_poll_gives_up_after_4th_consecutive_transient_failure(self):
        err = self.assert_raises_ig("after 4 consecutive failures", [SERVER_ERROR] * 4,
                                    lambda: ig.wait_for_container("C1"))
        self.assertEqual(len(self.polls()), 4)
        self.assertEqual(err.status_code, 500)

    def test_poll_failure_counter_resets_after_success(self):
        script = [SERVER_ERROR] * 3 + [IN_PROGRESS] + [SERVER_ERROR] * 3 + [FINISHED]
        result = self.run_script(script, lambda: ig.wait_for_container("C1"))
        self.assertEqual(result["status_code"], "FINISHED")
        self.assertEqual(self.sleeps, [10, 20, 40, 10, 10, 20, 40])

    def test_poll_4xx_is_not_retried(self):
        self.assert_raises_ig("HTTP 400", [FakeResponse(400, {"error": {"message": "Unsupported get request"}})],
                              lambda: ig.wait_for_container("C1"))
        self.assertEqual(len(self.polls()), 1)

    def test_poll_error_field_on_200_is_not_retried(self):
        self.assert_raises_ig("HTTP 200: Bad thing", [ok({"error": {"message": "Bad thing"}})],
                              lambda: ig.wait_for_container("C1"))
        self.assertEqual(len(self.polls()), 1)

    def test_poll_retry_respects_deadline(self):
        # One 10s poll left before the 600s deadline; a 10s backoff would exceed it.
        self.assert_raises_ig("Timed out after 600s waiting for container C1 (last error: GET /C1 -> HTTP 500", [IN_PROGRESS] * 60 + [SERVER_ERROR],
                              lambda: ig.wait_for_container("C1"))

    # --- ngrok path ---

    def test_ngrok_publish_uses_video_url(self):
        media_id = self.run_script([ok({"id": "C2"}), FINISHED, PUBLISHED],
                                   lambda: ig.publish_reel("https://x.ngrok.app/final_reel.mp4", "cap"))
        self.assertEqual(media_id, "M1")
        data = self.calls[0][2]["data"]
        self.assertEqual(data["video_url"], "https://x.ngrok.app/final_reel.mp4")
        self.assertNotIn("upload_type", data)

    # --- credentials, config, analytics ---

    def test_verify_credentials_facebook_host(self):
        info = self.run_script([
            ok({"id": "1784", "username": "myacct"}),
            ok({"data": {"is_valid": True, "expires_at": 0, "scopes": ["instagram_basic", "instagram_content_publish"]}}),
        ], ig.verify_credentials)
        self.assertEqual(info["username"], "myacct")
        self.assertEqual(info["scopes"], ["instagram_basic", "instagram_content_publish"])
        self.assertEqual(self.calls[1][1], "https://graph.facebook.com/v26.0/debug_token")

    def test_verify_credentials_invalid_token(self):
        self.assert_raises_ig("not valid", [ok({"id": "1784", "username": "u"}), ok({"data": {"is_valid": False}})],
                              ig.verify_credentials)

    def test_verify_credentials_instagram_host_skips_debug_token(self):
        self.settings["GRAPH_HOST"] = "graph.instagram.com"
        info = self.run_script([ok({"id": "1784", "username": "myacct"})], ig.verify_credentials)
        self.assertEqual(info["username"], "myacct")
        self.assertEqual(len(self.calls), 1)

    def test_missing_credentials(self):
        self.settings.clear()
        self.assert_raises_ig("Missing META_ACCESS_TOKEN", [], ig.verify_credentials)

    def test_publish_mode(self):
        self.assertEqual(ig.get_publish_mode(), "resumable")
        self.settings["PUBLISH_MODE"] = "NGROK"
        self.assertEqual(ig.get_publish_mode(), "ngrok")
        self.settings["PUBLISH_MODE"] = "bogus"
        with self.assertRaises(ig.IGPublishError):
            ig.get_publish_mode()

    def test_analytics_returns_none_on_error(self):
        self.assertIsNone(self.run_script([SERVER_ERROR], ig.get_recent_analytics))

    def test_analytics_tolerates_missing_counts(self):
        posts = self.run_script([ok({"data": [{"id": "1", "media_type": "REELS"}]})], ig.get_recent_analytics)
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
