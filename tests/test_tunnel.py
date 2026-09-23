"""Offline tests for tunnel.py: the single-file server runs for real on 127.0.0.1,
ngrok is mocked.

Run from the repo root: python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tunnel


class TunnelTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.video = os.path.join(self.dir, "my reel.mp4")
        with open(self.video, "wb") as f:
            f.write(b"\0" * 1024)
        with open(os.path.join(self.dir, "secret.txt"), "w") as f:
            f.write("do not serve")

        self.ngrok = mock.MagicMock()
        self.ngrok.connect.return_value.public_url = "https://abc.ngrok.app"
        self.conf = mock.MagicMock()
        self.settings = {"NGROK_AUTHTOKEN": "tok123"}
        for p in [
            mock.patch.object(tunnel, "ngrok", self.ngrok),
            mock.patch.object(tunnel, "conf", self.conf),
            mock.patch.object(tunnel.db, "get_setting", side_effect=lambda k: self.settings.get(k, "")),
            mock.patch.object(tunnel.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ]:
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)

    def serve(self, path=None):
        httpd = tunnel.start_file_server(path or self.video)
        self.addCleanup(tunnel.stop, httpd)
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

    def status(self, url, method="GET"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, None, b""

    # --- file server ---

    def test_serves_only_the_target_file(self):
        _, base = self.serve()
        status, ctype, body = self.status(f"{base}/my%20reel.mp4")
        self.assertEqual((status, ctype, len(body)), (200, "video/mp4", 1024))
        for other in ["/", "/secret.txt", "/my%20reel.mp4/../secret.txt", "/%2e%2e/secret.txt", "/MY%20REEL.MP4"]:
            with self.subTest(path=other):
                self.assertEqual(self.status(base + other)[0], 404)

    def test_head_returns_headers_without_body(self):
        _, base = self.serve()
        status, ctype, body = self.status(f"{base}/my%20reel.mp4", "HEAD")
        self.assertEqual((status, ctype, body), (200, "video/mp4", b""))

    def test_binds_to_localhost_only(self):
        httpd, _ = self.serve()
        self.assertEqual(httpd.server_address[0], "127.0.0.1")

    def test_missing_file_raises(self):
        with self.assertRaisesRegex(tunnel.TunnelError, "Video file not found"):
            tunnel.start_file_server(os.path.join(self.dir, "nope.mp4"))

    def test_stop_closes_server_and_kills_ngrok_even_if_kill_fails(self):
        httpd = tunnel.start_file_server(self.video)
        self.ngrok.kill.side_effect = RuntimeError("ngrok not running")
        tunnel.stop(httpd)
        self.ngrok.kill.assert_called_once()
        self.assertEqual(httpd.socket.fileno(), -1)
        tunnel.stop(None)  # no server: still fine

    # --- tunnel ---

    def test_open_tunnel_sets_authtoken_and_connects(self):
        self.assertEqual(tunnel.open_tunnel(5555), "https://abc.ngrok.app")
        self.assertEqual(self.conf.get_default.return_value.auth_token, "tok123")
        self.ngrok.connect.assert_called_once_with(5555, "http")

    def test_open_tunnel_upgrades_http_url_to_https(self):
        self.ngrok.connect.return_value.public_url = "http://abc.ngrok.app"
        self.assertEqual(tunnel.open_tunnel(5555), "https://abc.ngrok.app")

    def test_missing_authtoken_fails_clearly_without_connecting(self):
        self.settings.clear()
        with self.assertRaisesRegex(tunnel.TunnelError, "NGROK_AUTHTOKEN is not set. Add it to .env"):
            tunnel.open_tunnel(5555)
        self.ngrok.connect.assert_not_called()

    def test_bracketed_authtoken_is_rejected_without_echoing_it(self):
        self.settings["NGROK_AUTHTOKEN"] = "<tok123>"
        with self.assertRaises(tunnel.TunnelError) as ctx:
            tunnel.open_tunnel(5555)
        self.assertIn("angle brackets", str(ctx.exception))
        self.assertNotIn("tok123", str(ctx.exception))
        self.ngrok.connect.assert_not_called()

    def test_connect_failure_is_wrapped(self):
        self.ngrok.connect.side_effect = RuntimeError("ERR_NGROK_4018 authentication failed")
        with self.assertRaisesRegex(tunnel.TunnelError, "Could not open ngrok tunnel.*ERR_NGROK_4018") as ctx:
            tunnel.open_tunnel(5555)
        self.assertEqual(ctx.exception.step, "tunnel")

    # --- public URL check ---

    def test_check_public_url_passes_for_served_video(self):
        _, base = self.serve()
        tunnel.check_public_url(f"{base}/my%20reel.mp4")

    def test_check_public_url_fails_on_404_after_retries(self):
        _, base = self.serve()
        with mock.patch.object(tunnel.requests, "head", wraps=tunnel.requests.head) as head:
            with self.assertRaisesRegex(tunnel.TunnelError, "HTTP 404"):
                tunnel.check_public_url(f"{base}/other.mp4")
        self.assertEqual(head.call_count, tunnel.HEAD_ATTEMPTS)

    def test_check_public_url_fails_on_wrong_content_type(self):
        _, base = self.serve(os.path.join(self.dir, "secret.txt"))
        with self.assertRaisesRegex(tunnel.TunnelError, "Content-Type text/plain"):
            tunnel.check_public_url(f"{base}/secret.txt")

    def test_check_public_url_retries_then_succeeds(self):
        ok = mock.MagicMock(status_code=200, headers={"Content-Type": "video/mp4"})
        with mock.patch.object(tunnel.requests, "head",
                               side_effect=[tunnel.requests.ConnectionError("not yet"), ok]) as head:
            tunnel.check_public_url("https://abc.ngrok.app/x.mp4")
        self.assertEqual(head.call_count, 2)


if __name__ == "__main__":
    unittest.main()
