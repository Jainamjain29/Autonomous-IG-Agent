"""Expose one local video file to Meta over an ngrok tunnel (PUBLISH_MODE=ngrok).

Used by master_loop.publish_approved_video and scripts/publish_test.py:
    httpd = start_file_server(path)             # serves ONLY that file, on 127.0.0.1
    url = open_tunnel(httpd.server_address[1])  # needs NGROK_AUTHTOKEN
    video_url = f"{url}/{url_name(path)}"
    check_public_url(video_url)                 # HEAD: 200 + video/mp4
    ...
    stop(httpd)                                 # always, in finally

TODO: replace with S3 presigned URLs (PUBLISH_MODE=s3). See publish_fix_plan.md.
"""
import http.server
import mimetypes
import os
import shutil
import threading
import time
import urllib.parse

import requests
from pyngrok import conf, ngrok

import database as db

HEAD_TIMEOUT = 15
HEAD_ATTEMPTS = 3
HEAD_RETRY_DELAY = 2


class TunnelError(Exception):
    """The file server, the ngrok tunnel or the public URL check failed."""
    step = "tunnel"  # reported as the failing step, like IGPublishError.step


def _log(message):
    print(f"[tunnel] {message}")


def url_name(path):
    """The URL path segment the file is served under (its basename, percent-encoded)."""
    return urllib.parse.quote(os.path.basename(path))


def _content_type(path):
    if path.lower().endswith(".mp4"):
        return "video/mp4"  # don't trust the Windows registry for this one
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def start_file_server(path, port=0):
    """Serves ONLY `path` at /<basename> on 127.0.0.1 in a background thread.

    Every other path gets a 404. port=0 picks a free port; read it back from
    httpd.server_address[1]. Returns the server; shut it down with stop().
    """
    if not os.path.isfile(path):
        raise TunnelError(f"Video file not found: {path}")
    path = os.path.abspath(path)
    served = "/" + url_name(path)
    content_type = _content_type(path)

    class SingleFileHandler(http.server.BaseHTTPRequestHandler):
        def _send_headers(self):
            if urllib.parse.urlsplit(self.path).path != served:
                self.send_error(404)
                return False
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.end_headers()
            return True

        def do_HEAD(self):
            self._send_headers()

        def do_GET(self):
            if self._send_headers():
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)

        def log_message(self, fmt, *args):
            _log(f"{self.address_string()} {fmt % args}")

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), SingleFileHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def open_tunnel(port):
    """Opens an ngrok HTTP tunnel to localhost:port and returns its public https URL.

    Reads NGROK_AUTHTOKEN from .env (or the dashboard settings) and hands it to pyngrok
    in memory; the ngrok config file is not modified.
    """
    token = (db.get_setting("NGROK_AUTHTOKEN") or "").strip()
    if not token:
        raise TunnelError("NGROK_AUTHTOKEN is not set. Add it to .env "
                          "(get it from https://dashboard.ngrok.com/get-started/your-authtoken).")
    if token.startswith("<") or token.endswith(">"):
        raise TunnelError("NGROK_AUTHTOKEN in .env is wrapped in <angle brackets>. Remove them.")

    conf.get_default().auth_token = token
    try:
        public_url = ngrok.connect(port, "http").public_url
    except Exception as e:
        raise TunnelError(f"Could not open ngrok tunnel: {type(e).__name__}: {e}") from e
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[len("http://"):]
    return public_url


def check_public_url(url):
    """HEAD-checks the public URL: it must return 200 with Content-Type video/mp4.

    Retries a few times, since a fresh tunnel can take a moment to route.
    """
    last = None
    for attempt in range(1, HEAD_ATTEMPTS + 1):
        try:
            r = requests.head(url, timeout=HEAD_TIMEOUT, allow_redirects=True)
            content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if r.status_code == 200 and content_type == "video/mp4":
                _log(f"HEAD {url} -> 200 video/mp4, {r.headers.get('Content-Length', '?')} bytes")
                return
            last = f"HTTP {r.status_code}, Content-Type {content_type or '(none)'}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < HEAD_ATTEMPTS:
            _log(f"HEAD check attempt {attempt}/{HEAD_ATTEMPTS} failed ({last}); retrying")
            time.sleep(HEAD_RETRY_DELAY)
    raise TunnelError(f"Public URL check failed for {url}: expected 200 + video/mp4, got {last}")


def stop(httpd):
    """Kills ngrok and shuts the file server down (if any). Never raises."""
    try:
        ngrok.kill()
    except Exception as e:
        _log(f"WARNING: ngrok cleanup failed: {e}")
    if httpd is not None:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception as e:
            _log(f"WARNING: file server cleanup failed: {e}")
