"""Offline tests for master_loop.publish_approved_video.

Heavy modules (agent_brain, assembly_line, pyngrok) are stubbed and the database is
pointed at a temp file before master_loop is imported, so nothing real is touched.
Run from the repo root: python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp()
import database as db
db.DB_PATH = os.path.join(_tmp_dir, "test.db")
db.init_db()

sys.modules["agent_brain"] = mock.MagicMock()
sys.modules["assembly_line"] = mock.MagicMock()
_pyngrok = types.ModuleType("pyngrok")
_pyngrok.ngrok = mock.MagicMock()
_pyngrok.conf = mock.MagicMock()
sys.modules["pyngrok"] = _pyngrok

import ig_service
import master_loop
import tunnel

ngrok = _pyngrok.ngrok


class PublishApprovedVideoTest(unittest.TestCase):
    def setUp(self):
        ngrok.reset_mock(return_value=True, side_effect=True)
        # tunnel may have been imported with the real pyngrok by another test module.
        for name, value in [("ngrok", ngrok), ("conf", _pyngrok.conf)]:
            p = mock.patch.object(tunnel, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.workspace = tempfile.mkdtemp(dir=_tmp_dir)
        self.video = os.path.join(self.workspace, "final_reel.mp4")
        self.plan = os.path.join(self.workspace, "current_video_plan.json")
        with open(self.video, "wb") as f:
            f.write(b"\0" * 1024)
        with open(self.plan, "w", encoding="utf-8") as f:
            json.dump({"instagram_caption": "Hello", "hashtags": ["#a", "#b"]}, f)

        for name, value in [("WORKSPACE", self.workspace), ("FINAL_REEL", self.video), ("PLAN_PATH", self.plan)]:
            p = mock.patch.object(master_loop, name, value)
            p.start()
            self.addCleanup(p.stop)

        self.settings = {"PUBLISH_MODE": "resumable", "META_ACCESS_TOKEN": "TOK", "IG_USER_ID": "1784",
                         "NGROK_AUTHTOKEN": "ngtok"}
        p = mock.patch.object(ig_service.db, "get_setting",
                              side_effect=lambda k: self.settings.get(k) or db.get_stored_setting(k))
        p.start()
        self.addCleanup(p.stop)

        db.save_setting("WORKFLOW_STATE", "PENDING_REVIEW")
        db.save_setting("LAST_ERROR", "stale error from a previous run")
        db.save_setting("LAST_ERROR_STEP", "stale")
        db.save_setting("LAST_MEDIA_ID", "OLD")

    def state(self):
        return db.get_stored_setting("WORKFLOW_STATE")

    def last_error(self):
        return db.get_stored_setting("LAST_ERROR")

    # --- state guard ---

    def test_refuses_to_publish_outside_pending_review(self):
        for state in ["IDLE", "PUBLISHING", "FAILED", "GENERATING_PLAN"]:
            db.save_setting("WORKFLOW_STATE", state)
            with mock.patch.object(ig_service, "publish_reel_local") as publish:
                with self.assertRaisesRegex(RuntimeError, f"only allowed from PENDING_REVIEW.*{state}"):
                    master_loop.publish_approved_video()
                publish.assert_not_called()
            self.assertEqual(self.state(), state)

    # --- resumable mode ---

    def test_resumable_success_ends_idle(self):
        with mock.patch.object(ig_service, "publish_reel_local", return_value="M1") as publish:
            self.assertEqual(master_loop.publish_approved_video(), "M1")
        publish.assert_called_once_with(self.video, "Hello\n\n#a #b")
        self.assertEqual(self.state(), "IDLE")
        self.assertEqual(self.last_error(), "")
        ngrok.kill.assert_called_once()
        ngrok.connect.assert_not_called()

    def test_resumable_failure_ends_failed_with_error_and_response(self):
        err = ig_service.IGPublishError("Container ERROR: Error: 2207026",
                                        response_json={"status_code": "ERROR", "status": "Error: 2207026"})
        with mock.patch.object(ig_service, "publish_reel_local", side_effect=err):
            with self.assertRaises(ig_service.IGPublishError):
                master_loop.publish_approved_video()
        self.assertEqual(self.state(), "FAILED")
        self.assertIn("IGPublishError: Container ERROR: Error: 2207026", self.last_error())
        self.assertIn('"status": "Error: 2207026"', self.last_error())
        ngrok.kill.assert_called_once()

    def test_missing_video_ends_failed(self):
        os.remove(self.video)
        with mock.patch.object(ig_service, "publish_reel_local") as publish:
            with self.assertRaises(FileNotFoundError):
                master_loop.publish_approved_video()
            publish.assert_not_called()
        self.assertEqual(self.state(), "FAILED")
        self.assertIn("FileNotFoundError", self.last_error())

    def test_interrupt_still_ends_failed(self):
        with mock.patch.object(ig_service, "publish_reel_local", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                master_loop.publish_approved_video()
        self.assertEqual(self.state(), "FAILED")
        self.assertIn("KeyboardInterrupt", self.last_error())

    def test_ngrok_kill_failure_does_not_mask_success(self):
        ngrok.kill.side_effect = RuntimeError("ngrok not running")
        with mock.patch.object(ig_service, "publish_reel_local", return_value="M1"):
            self.assertEqual(master_loop.publish_approved_video(), "M1")
        self.assertEqual(self.state(), "IDLE")

    # --- ngrok mode ---

    def ngrok_mode(self):
        self.settings["PUBLISH_MODE"] = "ngrok"
        ngrok.connect.return_value.public_url = "https://abc.ngrok.app"
        httpd = mock.MagicMock(server_address=("127.0.0.1", 5555))
        start = mock.patch.object(tunnel, "start_file_server", return_value=httpd)
        check = mock.patch.object(tunnel, "check_public_url")
        self.start_file_server, self.check_public_url = start.start(), check.start()
        self.addCleanup(start.stop)
        self.addCleanup(check.stop)
        return httpd

    def assert_torn_down(self, httpd):
        httpd.shutdown.assert_called_once()
        httpd.server_close.assert_called_once()
        ngrok.kill.assert_called_once()

    def test_ngrok_success_serves_only_the_reel_and_cleans_up(self):
        httpd = self.ngrok_mode()
        with mock.patch.object(ig_service, "publish_reel", return_value="M2") as publish:
            self.assertEqual(master_loop.publish_approved_video(), "M2")
        self.start_file_server.assert_called_once_with(self.video)
        ngrok.connect.assert_called_once_with(5555, "http")
        self.assertEqual(_pyngrok.conf.get_default.return_value.auth_token, "ngtok")
        self.check_public_url.assert_called_once_with("https://abc.ngrok.app/final_reel.mp4")
        publish.assert_called_once_with("https://abc.ngrok.app/final_reel.mp4", "Hello\n\n#a #b")
        self.assert_torn_down(httpd)
        self.assertEqual(self.state(), "IDLE")

    def test_ngrok_tunnel_failure_still_cleans_up(self):
        httpd = self.ngrok_mode()
        ngrok.connect.side_effect = RuntimeError("ngrok auth failed")
        with self.assertRaises(tunnel.TunnelError):
            master_loop.publish_approved_video()
        self.assert_torn_down(httpd)
        self.assertEqual(self.state(), "FAILED")
        self.assertEqual(db.get_stored_setting("LAST_ERROR_STEP"), "tunnel")
        self.assertIn("ngrok auth failed", self.last_error())

    def test_ngrok_missing_authtoken_ends_failed(self):
        httpd = self.ngrok_mode()
        del self.settings["NGROK_AUTHTOKEN"]
        with mock.patch.object(ig_service, "publish_reel") as publish:
            with self.assertRaises(tunnel.TunnelError):
                master_loop.publish_approved_video()
        publish.assert_not_called()
        ngrok.connect.assert_not_called()
        self.assert_torn_down(httpd)
        self.assertIn("NGROK_AUTHTOKEN is not set", self.last_error())

    def test_ngrok_head_check_failure_creates_no_container(self):
        httpd = self.ngrok_mode()
        self.check_public_url.side_effect = tunnel.TunnelError("Public URL check failed: HTTP 404")
        with mock.patch.object(ig_service, "publish_reel") as publish:
            with self.assertRaises(tunnel.TunnelError):
                master_loop.publish_approved_video()
        publish.assert_not_called()
        self.assert_torn_down(httpd)
        self.assertEqual(self.state(), "FAILED")

    def test_invalid_publish_mode_ends_failed(self):
        self.settings["PUBLISH_MODE"] = "bogus"
        with self.assertRaises(ig_service.IGPublishError):
            master_loop.publish_approved_video()
        self.assertEqual(self.state(), "FAILED")
        self.assertIn("PUBLISH_MODE", self.last_error())

    # --- failing step, media ID, duplicate-post guard ---

    def publish_with_graph(self, responses):
        """Runs a real publish_reel_local against a scripted Graph API."""
        script = list(responses)

        def fake_request(method, url, **kwargs):
            r = script.pop(0)
            if isinstance(r, Exception):
                raise r
            resp = mock.MagicMock(status_code=r[0], ok=200 <= r[0] < 300, text=json.dumps(r[1]))
            resp.json.return_value = r[1]
            return resp

        with mock.patch.object(ig_service.requests, "request", side_effect=fake_request), \
             mock.patch.object(ig_service.time, "sleep"), mock.patch.object(ig_service, "_log"):
            return master_loop.publish_approved_video()

    CREATED = (200, {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/C1"})
    UPLOADED = (200, {"success": True})
    FINISHED = (200, {"id": "C1", "status_code": "FINISHED"})
    BAD_REQUEST = (400, {"error": {"message": "Bad request"}})

    def test_records_failing_step(self):
        cases = {
            "container": [self.BAD_REQUEST],
            "upload": [self.CREATED, (200, {"debug_info": {"message": "ProcessingFailedError"}})],
            "poll": [self.CREATED, self.UPLOADED, (200, {"id": "C1", "status_code": "ERROR", "status": "Error: 2207026"})],
            "publish": [self.CREATED, self.UPLOADED, self.FINISHED, self.BAD_REQUEST],
        }
        for step, responses in cases.items():
            with self.subTest(step=step):
                db.save_setting("WORKFLOW_STATE", "PENDING_REVIEW")
                with self.assertRaises(ig_service.IGPublishError):
                    self.publish_with_graph(responses)
                self.assertEqual(self.state(), "FAILED")
                self.assertEqual(db.get_stored_setting("LAST_ERROR_STEP"), step)
                self.assertTrue(self.last_error().startswith(f"Failed at step: {step}\n"))
                self.assertEqual(master_loop.publish_may_have_succeeded(), step == "publish")
                self.assertEqual(db.get_stored_setting("LAST_MEDIA_ID"), "OLD")

    def test_network_error_at_publish_step_warns_of_possible_duplicate(self):
        with self.assertRaises(ig_service.IGPublishError):
            self.publish_with_graph([self.CREATED, self.UPLOADED, self.FINISHED,
                                     ig_service.requests.ReadTimeout("read timed out")])
        self.assertEqual(db.get_stored_setting("LAST_ERROR_STEP"), "publish")
        self.assertTrue(master_loop.publish_may_have_succeeded())

    def test_failure_before_graph_api_is_setup_step(self):
        os.remove(self.plan)
        with self.assertRaises(FileNotFoundError):
            master_loop.publish_approved_video()
        self.assertEqual(db.get_stored_setting("LAST_ERROR_STEP"), "setup")
        self.assertFalse(master_loop.publish_may_have_succeeded())

    def test_success_stores_media_id(self):
        self.assertEqual(self.publish_with_graph([self.CREATED, self.UPLOADED, self.FINISHED, (200, {"id": "M7"})]), "M7")
        self.assertEqual(db.get_stored_setting("LAST_MEDIA_ID"), "M7")
        self.assertEqual(db.get_stored_setting("LAST_ERROR_STEP"), "")
        self.assertEqual(self.state(), "IDLE")

    # --- FAILED view actions ---

    def test_back_to_review_then_retry_succeeds(self):
        with self.assertRaises(ig_service.IGPublishError):
            self.publish_with_graph([self.BAD_REQUEST])
        self.assertTrue(master_loop.can_return_to_review())
        master_loop.return_to_review()
        self.assertEqual(self.state(), "PENDING_REVIEW")
        self.assertEqual(self.publish_with_graph([self.CREATED, self.UPLOADED, self.FINISHED, (200, {"id": "M8"})]), "M8")
        self.assertEqual(self.state(), "IDLE")

    def test_back_to_review_needs_video_and_plan(self):
        for missing in ("video", "plan"):
            with self.subTest(missing=missing):
                self.setUp()
                db.save_setting("WORKFLOW_STATE", "FAILED")
                os.remove(self.video if missing == "video" else self.plan)
                self.assertFalse(master_loop.can_return_to_review())
                with self.assertRaises(FileNotFoundError):
                    master_loop.return_to_review()
                self.assertEqual(self.state(), "FAILED")

    def test_back_to_review_only_from_failed(self):
        db.save_setting("WORKFLOW_STATE", "IDLE")
        with self.assertRaises(RuntimeError):
            master_loop.return_to_review()
        self.assertEqual(self.state(), "IDLE")

    def test_reset_after_failure_clears_error(self):
        with self.assertRaises(ig_service.IGPublishError):
            self.publish_with_graph([self.CREATED, self.UPLOADED, self.FINISHED, self.BAD_REQUEST])
        master_loop.reset_after_failure()
        self.assertEqual(self.state(), "IDLE")
        self.assertEqual(self.last_error(), "")
        self.assertFalse(master_loop.publish_may_have_succeeded())

    # --- console output ---

    def test_publish_output_is_plain_ascii(self):
        """A cp1252 console (Windows default when piped) must not crash on publish logs."""
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(out):
            with mock.patch.object(ig_service, "publish_reel_local", return_value="M1"):
                master_loop.publish_approved_video()
            db.save_setting("WORKFLOW_STATE", "PENDING_REVIEW")
            ngrok.kill.side_effect = RuntimeError("x")
            with mock.patch.object(ig_service, "publish_reel_local", side_effect=ig_service.IGPublishError("boom")):
                with self.assertRaises(ig_service.IGPublishError):
                    master_loop.publish_approved_video()
            out.flush()
        with open(master_loop.__file__, encoding="utf-8") as f:
            self.assertTrue(f.read().isascii(), "master_loop.py should contain no emoji")


if __name__ == "__main__":
    unittest.main()
