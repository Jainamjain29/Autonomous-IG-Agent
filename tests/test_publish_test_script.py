"""Offline tests for scripts/publish_test.py, driven end to end against a fake Graph API.

Run from the repo root: python -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ig_service as ig
import tunnel

_spec = importlib.util.spec_from_file_location("publish_test", os.path.join(ROOT, "scripts", "publish_test.py"))
publish_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_test)

CREATED = (200, {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/C1"})
CREATED_FROM_URL = (200, {"id": "C1"})
UPLOADED = (200, {"success": True})
FINISHED = (200, {"id": "C1", "status_code": "FINISHED"})
USER = (200, {"id": "1784", "username": "myacct"})
TOKEN_OK = (200, {"data": {"is_valid": True, "expires_at": 0, "scopes": ["instagram_content_publish"]}})
BAD_REQUEST = (400, {"error": {"message": "Bad request"}})


class ScriptTestCase(unittest.TestCase):
    """Fakes the Graph API and ngrok; settings come from self.settings."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(b"\0" * 2048)
        tmp.close()
        self.video = tmp.name
        self.addCleanup(os.remove, self.video)

        self.settings = {"META_ACCESS_TOKEN": "TOK", "IG_USER_ID": "1784", "PUBLISH_MODE": "resumable",
                         "NGROK_AUTHTOKEN": "ngtok"}
        self.calls = []
        self.kwargs = []
        self.ngrok = mock.MagicMock()
        self.ngrok.connect.return_value.public_url = "https://abc.ngrok.app"
        self.conf = mock.MagicMock()
        for p in [
            mock.patch.object(ig.db, "get_setting", side_effect=lambda k: self.settings.get(k, "")),
            mock.patch.object(ig.requests, "request", side_effect=self._fake_request),
            mock.patch.object(ig.time, "sleep"),
            mock.patch.object(ig, "_log"),
            mock.patch.object(tunnel, "ngrok", self.ngrok),
            mock.patch.object(tunnel, "conf", self.conf),
            mock.patch.object(tunnel.time, "sleep"),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _fake_request(self, method, url, **kwargs):
        self.calls.append((method, url))
        self.kwargs.append(kwargs)
        r = self.script.pop(0)
        if callable(r):
            r = r()
        if isinstance(r, BaseException):
            raise r
        resp = mock.MagicMock(status_code=r[0], ok=200 <= r[0] < 300, text=json.dumps(r[1]))
        resp.json.return_value = r[1]
        return resp

    def run_cli(self, script, *args):
        self.script = list(script)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = publish_test.main([self.video, "My caption", *args])
        return code, out.getvalue(), err.getvalue()


class ResumableModeTest(ScriptTestCase):
    def test_dry_run_verifies_uploads_polls_but_never_publishes(self):
        code, out, _ = self.run_cli([USER, TOKEN_OK, CREATED, UPLOADED, FINISHED], "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("PUBLISH_MODE=resumable", out)
        self.assertIn("DRY RUN OK: container C1", out)
        urls = [url for _, url in self.calls]
        self.assertTrue(urls[0].endswith("/1784"))
        self.assertTrue(urls[1].endswith("/debug_token"))
        self.assertTrue(urls[2].endswith("/1784/media"))
        self.assertTrue(urls[3].startswith("https://rupload.facebook.com/"))
        self.assertTrue(urls[4].endswith("/C1"))
        self.assertFalse(any("media_publish" in u for u in urls))
        self.ngrok.connect.assert_not_called()
        self.ngrok.kill.assert_not_called()

    def test_live_run_publishes(self):
        code, out, _ = self.run_cli([USER, TOKEN_OK, CREATED, UPLOADED, FINISHED, (200, {"id": "M1"})])
        self.assertEqual(code, 0)
        self.assertIn("PUBLISHED: IG media ID M1", out)
        self.assertTrue(self.calls[-1][1].endswith("/1784/media_publish"))

    def test_resumable_on_instagram_host_tells_user_to_use_ngrok(self):
        self.settings["GRAPH_HOST"] = "graph.instagram.com"
        code, _, err = self.run_cli([USER], "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("not supported on graph.instagram.com", err)
        self.assertIn("Set PUBLISH_MODE=ngrok", err)
        self.assertEqual(len(self.calls), 1)  # only the credential check
        self.ngrok.connect.assert_not_called()

    def test_invalid_publish_mode_exit_1(self):
        self.settings["PUBLISH_MODE"] = "bogus"
        code, _, err = self.run_cli([], "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("PUBLISH_MODE must be one of", err)
        self.assertEqual(self.calls, [])

    def test_bad_credentials_exit_1_before_any_upload(self):
        code, _, err = self.run_cli([(400, {"error": {"message": "Invalid OAuth access token"}})], "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step verify", err)
        self.assertIn("Invalid OAuth access token", err)
        self.assertEqual(len(self.calls), 1)

    def test_poll_error_exit_1_with_response(self):
        code, _, err = self.run_cli(
            [USER, TOKEN_OK, CREATED, UPLOADED, (200, {"id": "C1", "status_code": "ERROR", "status": "Error: 2207026"})],
            "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step poll", err)
        self.assertIn('"status": "Error: 2207026"', err)
        self.assertNotIn("may have succeeded", err)

    def test_publish_step_failure_warns_of_possible_duplicate(self):
        code, _, err = self.run_cli([USER, TOKEN_OK, CREATED, UPLOADED, FINISHED, BAD_REQUEST])
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step publish", err)
        self.assertIn("Publish may have succeeded", err)

    def test_interrupt_during_publish_warns_and_exits_130(self):
        code, _, err = self.run_cli([USER, TOKEN_OK, CREATED, UPLOADED, FINISHED, KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertIn("Interrupted during step publish", err)
        self.assertIn("Publish may have succeeded", err)

    def test_missing_video_exit_1(self):
        self.script = [USER, TOKEN_OK]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = publish_test.main(["nope.mp4", "cap", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("Video file not found", err.getvalue())

    def test_requires_video_and_caption(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            publish_test.main(["only_video.mp4"])
        self.assertEqual(ctx.exception.code, 2)


class NgrokModeTest(ScriptTestCase):
    """PUBLISH_MODE=ngrok on graph.instagram.com. The single-file server runs for real;
    ngrok is mocked and HEAD requests to the fake public URL are routed to that server."""

    def setUp(self):
        super().setUp()
        self.settings.update({"PUBLISH_MODE": "ngrok", "GRAPH_HOST": "graph.instagram.com"})
        self.servers = []
        real_start = tunnel.start_file_server
        real_head = tunnel.requests.head

        def start(path, port=0):
            httpd = real_start(path, port)
            self.servers.append(httpd)
            return httpd

        def head(url, **kwargs):
            port = self.ngrok.connect.call_args[0][0]
            return real_head(url.replace("https://abc.ngrok.app", f"http://127.0.0.1:{port}"), **kwargs)

        for p in [mock.patch.object(tunnel, "start_file_server", side_effect=start),
                  mock.patch.object(tunnel.requests, "head", side_effect=head)]:
            p.start()
            self.addCleanup(p.stop)
        self.public_url = "https://abc.ngrok.app/" + os.path.basename(self.video)

    def assert_torn_down(self):
        self.ngrok.kill.assert_called_once()
        for httpd in self.servers:
            self.assertEqual(httpd.socket.fileno(), -1)

    def test_dry_run_creates_container_with_video_url_and_never_publishes(self):
        code, out, _ = self.run_cli([USER, CREATED_FROM_URL, FINISHED], "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("PUBLISH_MODE=ngrok", out)
        self.assertIn(f"Public URL: {self.public_url}", out)
        self.assertIn("-> 200 video/mp4, 2048 bytes", out)
        self.assertIn("DRY RUN OK: container C1", out)
        self.assertEqual(self.conf.get_default.return_value.auth_token, "ngtok")
        urls = [url for _, url in self.calls]
        self.assertEqual(urls[1], "https://graph.instagram.com/v26.0/1784/media")
        self.assertEqual(self.kwargs[1]["data"]["video_url"], self.public_url)
        self.assertNotIn("upload_type", self.kwargs[1]["data"])
        self.assertFalse(any("rupload" in u or "media_publish" in u for u in urls))
        self.assertEqual(len(self.servers), 1)
        self.assert_torn_down()

    def test_live_run_publishes(self):
        code, out, _ = self.run_cli([USER, CREATED_FROM_URL, FINISHED, (200, {"id": "M1"})])
        self.assertEqual(code, 0, out)
        self.assertIn("PUBLISHED: IG media ID M1", out)
        self.assertTrue(self.calls[-1][1].endswith("/1784/media_publish"))
        self.assert_torn_down()

    def test_serves_only_the_target_file_while_meta_fetches(self):
        neighbour = os.path.join(os.path.dirname(self.video), "neighbour_secret.txt")
        with open(neighbour, "w") as f:
            f.write("private")
        self.addCleanup(os.remove, neighbour)
        seen = {}

        def probe_then_finish():
            seen["target"] = tunnel.requests.head(self.public_url).status_code
            seen["neighbour"] = tunnel.requests.head("https://abc.ngrok.app/neighbour_secret.txt").status_code
            seen["root"] = tunnel.requests.head("https://abc.ngrok.app/").status_code
            return FINISHED

        code, out, _ = self.run_cli([USER, CREATED_FROM_URL, probe_then_finish], "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(seen, {"target": 200, "neighbour": 404, "root": 404})

    def test_missing_authtoken_fails_before_container(self):
        del self.settings["NGROK_AUTHTOKEN"]
        code, _, err = self.run_cli([USER], "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step tunnel: NGROK_AUTHTOKEN is not set. Add it to .env", err)
        self.assertEqual(len(self.calls), 1)
        self.ngrok.connect.assert_not_called()
        self.assert_torn_down()

    def test_head_check_failure_creates_no_container(self):
        self.ngrok.connect.return_value.public_url = "https://abc.ngrok.app/wrong-prefix"
        code, _, err = self.run_cli([USER], "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step tunnel: Public URL check failed", err)
        self.assertIn("HTTP 404", err)
        self.assertEqual(len(self.calls), 1)
        self.assert_torn_down()

    def test_container_error_still_tears_down(self):
        code, _, err = self.run_cli(
            [USER, CREATED_FROM_URL, (200, {"id": "C1", "status_code": "ERROR", "status": "Error: 2207076"})],
            "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step poll", err)
        self.assert_torn_down()

    def test_interrupt_during_poll_tears_down_and_exits_130(self):
        code, _, err = self.run_cli([USER, CREATED_FROM_URL, KeyboardInterrupt()], "--dry-run")
        self.assertEqual(code, 130)
        self.assertIn("Interrupted during step poll", err)
        self.assert_torn_down()

    def test_missing_video_fails_before_tunnel(self):
        self.script = [USER]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = publish_test.main(["nope.mp4", "cap", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("Video file not found", err.getvalue())
        self.ngrok.connect.assert_not_called()
        self.assertEqual(self.servers, [])


if __name__ == "__main__":
    unittest.main()
