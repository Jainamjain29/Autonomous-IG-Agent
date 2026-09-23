"""Offline tests for scripts/publish_inbox.py against a fake Graph API, a mocked ngrok
and a real temp inbox folder. Answers to prompts are scripted.

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

_spec = importlib.util.spec_from_file_location("publish_inbox", os.path.join(ROOT, "scripts", "publish_inbox.py"))
publish_inbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_inbox)

USER = (200, {"id": "1784", "username": "myacct"})
TOKEN_OK = (200, {"data": {"is_valid": True, "expires_at": 0, "scopes": ["instagram_content_publish"]}})


def created(cid):
    return 200, {"id": cid, "uri": f"https://rupload.facebook.com/ig-api-upload/v26.0/{cid}"}


UPLOADED = (200, {"success": True})
FINISHED = (200, {"status_code": "FINISHED"})
POLL_ERROR = (200, {"status_code": "ERROR", "status": "Error: 2207026"})
BAD_REQUEST = (400, {"error": {"message": "Bad request"}})


def published(mid):
    return 200, {"id": mid}


def resumable_ok(cid, mid):
    return [created(cid), UPLOADED, FINISHED, published(mid)]


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self.inbox = os.path.join(tempfile.mkdtemp(), "inbox")
        self.settings = {"META_ACCESS_TOKEN": "TOK", "IG_USER_ID": "1784", "PUBLISH_MODE": "resumable",
                         "NGROK_AUTHTOKEN": "ngtok"}
        self.calls, self.kwargs, self.prompts = [], [], []
        self.ngrok = mock.MagicMock()
        self.ngrok.connect.return_value.public_url = "https://abc.ngrok.app"
        for p in [
            mock.patch.object(ig.db, "get_setting", side_effect=lambda k: self.settings.get(k, "")),
            mock.patch.object(ig.requests, "request", side_effect=self._fake_request),
            mock.patch.object(ig.time, "sleep"),
            mock.patch.object(ig, "_log"),
            mock.patch.object(tunnel, "ngrok", self.ngrok),
            mock.patch.object(tunnel, "conf", mock.MagicMock()),
            mock.patch.object(tunnel.time, "sleep"),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def add_video(self, name, caption=None, size=2048):
        os.makedirs(self.inbox, exist_ok=True)
        path = os.path.join(self.inbox, name)
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        if caption is not None:
            with open(os.path.splitext(path)[0] + ".txt", "w", encoding="utf-8") as f:
                f.write(caption)
        return path

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

    def run_cli(self, answers, script=(), *args):
        answers, self.script = list(answers), list(script)

        def fake_input(prompt):
            self.prompts.append(prompt)
            if not answers:
                raise EOFError
            return answers.pop(0)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = publish_inbox.main(["--inbox", self.inbox, *args], input_fn=fake_input)
        self.assertEqual(self.script, [], "not every scripted Graph API response was used")
        return code, out.getvalue(), err.getvalue()

    def media_publishes(self):
        return [kw["data"]["creation_id"] for (_, url), kw in zip(self.calls, self.kwargs)
                if url.endswith("/media_publish")]

    def inbox_files(self, sub=""):
        return sorted(os.listdir(os.path.join(self.inbox, sub)))

    def record(self, sub, stem):
        with open(os.path.join(self.inbox, sub, stem + ".json"), encoding="utf-8") as f:
            return json.load(f)


class ReviewTest(InboxTestCase):
    def test_creates_folders_and_handles_empty_inbox(self):
        code, out, _ = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("Inbox is empty", out)
        self.assertEqual(self.inbox_files(), ["failed", "posted"])
        self.assertEqual(self.calls, [])

    def test_lists_size_and_sidecar_caption_without_prompting_for_it(self):
        self.add_video("a.mp4", "Caption A\n#tag", size=3 * 1024 * 1024)
        code, out, _ = self.run_cli(["n"])
        self.assertEqual(code, 0)
        self.assertIn("1. a.mp4  (3.0 MB)", out)
        self.assertIn("Caption A", out)
        self.assertIn("#tag", out)
        self.assertEqual(self.prompts, ["Publish 1/1 a.mp4? [y/n/q] "])

    def test_missing_caption_is_prompted_and_saved(self):
        self.add_video("a.mp4")
        code, _, _ = self.run_cli(["Typed caption", "n"])
        self.assertEqual(code, 0)
        self.assertEqual(self.prompts[0], "Caption for a.mp4 (empty = skip): ")
        self.assertEqual(publish_inbox.read_caption(os.path.join(self.inbox, "a.mp4")), "Typed caption")

    def test_empty_caption_skips_video(self):
        self.add_video("a.mp4")
        self.add_video("b.mp4", "B")
        code, out, _ = self.run_cli(["   ", "n"])
        self.assertIn("Skipping a.mp4: no caption.", out)
        self.assertNotIn("Publish 1/2", "".join(self.prompts))
        self.assertIn("Publish 1/1 b.mp4? [y/n/q] ", self.prompts)
        self.assertFalse(os.path.exists(os.path.join(self.inbox, "a.txt")))
        self.assertIn("a.mp4", self.inbox_files())

    def test_blank_sidecar_counts_as_missing(self):
        self.add_video("a.mp4", "  \n")
        self.run_cli([""])
        self.assertEqual(self.prompts, ["Caption for a.mp4 (empty = skip): "])

    def test_sidecar_with_bom_is_read_cleanly(self):
        path = self.add_video("a.mp4")
        with open(os.path.join(self.inbox, "a.txt"), "w", encoding="utf-8-sig") as f:
            f.write("Hello")
        self.assertEqual(publish_inbox.read_caption(path), "Hello")

    def test_only_videos_directly_in_inbox_are_listed(self):
        self.add_video("a.mp4", "A")
        os.makedirs(os.path.join(self.inbox, "posted"))
        with open(os.path.join(self.inbox, "posted", "old.mp4"), "wb") as f:
            f.write(b"\0")
        with open(os.path.join(self.inbox, "notes.txt"), "w") as f:
            f.write("x")
        self.assertEqual([os.path.basename(v) for v in publish_inbox.find_videos(self.inbox)], ["a.mp4"])

    def test_nothing_publishes_without_explicit_y(self):
        self.add_video("a.mp4", "A")
        self.add_video("b.mp4", "B")
        code, out, _ = self.run_cli(["n", "N"])
        self.assertEqual(code, 0)
        self.assertIn("No videos approved", out)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.inbox_files(), ["a.mp4", "a.txt", "b.mp4", "b.txt", "failed", "posted"])

    def test_invalid_answer_reprompts_and_blank_is_not_approval(self):
        self.add_video("a.mp4", "A")
        code, out, _ = self.run_cli(["maybe", "", "n"])
        self.assertEqual(len(self.prompts), 3)
        self.assertEqual(out.count("Please answer y, n or q."), 2)
        self.assertEqual(self.calls, [])

    def test_closed_stdin_is_treated_as_q(self):
        self.add_video("a.mp4", "A")
        code, out, _ = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("Stopped reviewing", out)
        self.assertEqual(self.calls, [])

    def test_q_stops_review_but_publishes_earlier_y(self):
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            self.add_video(name, name.upper())
        code, _, _ = self.run_cli(["y", "q"], [USER, TOKEN_OK, *resumable_ok("C1", "M1")])
        self.assertEqual(code, 0)
        self.assertEqual(len([p for p in self.prompts if p.startswith("Publish")]), 2)  # c never asked
        self.assertEqual(self.media_publishes(), ["C1"])
        self.assertEqual(self.inbox_files(), ["b.mp4", "b.txt", "c.mp4", "c.txt", "failed", "posted"])


class ResumablePublishTest(InboxTestCase):
    def test_approved_video_is_published_and_moved_to_posted(self):
        self.add_video("a.mp4", "Caption A")
        self.add_video("b.mp4", "Caption B")
        code, out, _ = self.run_cli(["n", "y"], [USER, TOKEN_OK, *resumable_ok("C2", "M2")])
        self.assertEqual(code, 0)
        self.assertEqual(self.kwargs[2]["data"]["caption"], "Caption B")
        self.assertIn("PUBLISHED: IG media ID M2", out)
        self.assertEqual(self.inbox_files("posted"), ["b.json", "b.mp4", "b.txt"])
        rec = self.record("posted", "b")
        self.assertEqual((rec["media_id"], rec["caption"], rec["video"]), ("M2", "Caption B", "b.mp4"))
        self.assertIn("T", rec["published_at"])
        self.assertIn("a.mp4", self.inbox_files())  # the "n" one stays

    def test_one_failure_does_not_stop_the_batch(self):
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            self.add_video(name, name.upper())
        code, out, err = self.run_cli(
            ["y", "y", "y"],
            [USER, TOKEN_OK,
             created("C1"), UPLOADED, POLL_ERROR,
             *resumable_ok("C2", "M2"),
             created("C3"), UPLOADED, FINISHED, BAD_REQUEST])
        self.assertEqual(code, 1)
        self.assertEqual(self.inbox_files("posted"), ["b.json", "b.mp4", "b.txt"])
        self.assertEqual(self.inbox_files("failed"), ["a.json", "a.mp4", "a.txt", "c.json", "c.mp4", "c.txt"])
        a = self.record("failed", "a")
        self.assertEqual(a["step"], "poll")
        self.assertIn("Error: 2207026", a["error"])
        self.assertFalse(a["may_have_published"])
        c = self.record("failed", "c")
        self.assertEqual(c["step"], "publish")
        self.assertTrue(c["may_have_published"])
        self.assertEqual(c["response"]["error"]["message"], "Bad request")
        self.assertIn("Publish may have succeeded", err)
        self.assertIn("FAILED  a.mp4  (step poll)", out)
        self.assertIn("OK      b.mp4  (media M2)", out)

    def test_unexpected_exception_is_contained_too(self):
        self.add_video("a.mp4", "A")
        self.add_video("b.mp4", "B")
        with mock.patch.object(ig, "publish_reel_local", side_effect=[ValueError("weird"), "M2"]):
            code, _, _ = self.run_cli(["y", "y"], [USER, TOKEN_OK])
        self.assertEqual(code, 1)
        self.assertEqual(self.record("failed", "a")["error"], "ValueError: weird")
        self.assertEqual(self.record("posted", "b")["media_id"], "M2")

    def test_dry_run_never_publishes_and_moves_nothing(self):
        self.add_video("a.mp4", "A")
        self.add_video("b.mp4", "B")
        code, out, _ = self.run_cli(["y", "y"], [USER, TOKEN_OK,
                                                  created("C1"), UPLOADED, FINISHED,
                                                  created("C2"), UPLOADED, POLL_ERROR], "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(self.media_publishes(), [])
        self.assertIn("DRY RUN OK: container C1", out)
        self.assertEqual(self.inbox_files(), ["a.mp4", "a.txt", "b.mp4", "b.txt", "failed", "posted"])
        self.assertEqual(self.inbox_files("posted") + self.inbox_files("failed"), [])

    def test_credential_failure_leaves_everything_in_inbox(self):
        self.add_video("a.mp4", "A")
        code, _, err = self.run_cli(["y"], [(400, {"error": {"message": "Invalid OAuth access token"}})])
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step verify", err)
        self.assertIn("all videos stay in the inbox", err)
        self.assertIn("a.mp4", self.inbox_files())
        self.assertEqual(self.inbox_files("failed"), [])

    def test_name_clash_in_posted_gets_unique_name(self):
        self.add_video("a.mp4", "A")
        os.makedirs(os.path.join(self.inbox, "posted"))
        for ext in (".mp4", ".txt", ".json"):
            with open(os.path.join(self.inbox, "posted", "a" + ext), "w") as f:
                f.write("old")
        self.run_cli(["y"], [USER, TOKEN_OK, *resumable_ok("C1", "M1")])
        posted = self.inbox_files("posted")
        self.assertEqual(len(posted), 6)
        new_json = [n for n in posted if n.endswith(".json") and n != "a.json"][0]
        self.assertEqual(self.record("posted", new_json[:-5])["media_id"], "M1")
        with open(os.path.join(self.inbox, "posted", "a.mp4")) as f:
            self.assertEqual(f.read(), "old")

    def test_move_failure_after_publish_is_flagged_and_batch_continues(self):
        self.add_video("a.mp4", "A")
        self.add_video("b.mp4", "B")
        real = publish_inbox.file_away
        outcomes = [PermissionError("file in use"), real]

        def flaky(*args):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome(*args)

        with mock.patch.object(publish_inbox, "file_away", side_effect=flaky):
            code, _, err = self.run_cli(["y", "y"], [USER, TOKEN_OK, *resumable_ok("C1", "M1"),
                                                     *resumable_ok("C2", "M2")])
        self.assertEqual(code, 0)
        self.assertIn("It WAS published (media ID M1)", err)
        self.assertEqual(self.media_publishes(), ["C1", "C2"])
        self.assertIn("a.mp4", self.inbox_files())  # could not be moved
        self.assertEqual(self.record("posted", "b")["media_id"], "M2")

    def test_interrupt_mid_batch_files_current_video_and_leaves_rest(self):
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            self.add_video(name, name.upper())
        code, _, err = self.run_cli(["y", "y", "y"],
                                    [USER, TOKEN_OK, *resumable_ok("C1", "M1"),
                                     created("C2"), UPLOADED, FINISHED, KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertIn("Interrupted during step publish of b.mp4", err)
        self.assertIn("Publish may have succeeded", err)
        self.assertEqual(self.inbox_files("posted"), ["a.json", "a.mp4", "a.txt"])
        self.assertTrue(self.record("failed", "b")["may_have_published"])
        self.assertIn("c.mp4", self.inbox_files())


class NgrokPublishTest(InboxTestCase):
    """The file server is real; HEAD requests to the fake public URL are routed to it."""

    def setUp(self):
        super().setUp()
        self.settings.update({"PUBLISH_MODE": "ngrok", "GRAPH_HOST": "graph.instagram.com"})
        self.servers = []
        real_start, real_head = tunnel.start_file_server, tunnel.requests.head

        def start(paths, port=0):
            httpd = real_start(paths, port)
            self.servers.append((httpd, list(paths)))
            return httpd

        def head(url, **kwargs):
            port = self.ngrok.connect.call_args[0][0]
            return real_head(url.replace("https://abc.ngrok.app", f"http://127.0.0.1:{port}"), **kwargs)

        for p in [mock.patch.object(tunnel, "start_file_server", side_effect=start),
                  mock.patch.object(tunnel.requests, "head", side_effect=head)]:
            p.start()
            self.addCleanup(p.stop)

    def assert_torn_down(self):
        self.ngrok.kill.assert_called_once()
        for httpd, _ in self.servers:
            self.assertEqual(httpd.socket.fileno(), -1)

    def test_one_tunnel_for_the_whole_batch_serving_only_approved_videos(self):
        a = self.add_video("a.mp4", "A")
        self.add_video("b.mp4", "B")
        c = self.add_video("c.mp4", "C")
        seen = {}

        def probe():
            seen["b"] = tunnel.requests.head("https://abc.ngrok.app/b.mp4").status_code
            seen["a_after_move"] = tunnel.requests.head("https://abc.ngrok.app/a.mp4").status_code
            return FINISHED

        code, out, _ = self.run_cli(
            ["y", "n", "y"],
            [USER, (200, {"id": "C1"}), FINISHED, published("M1"),
             (200, {"id": "C3"}), probe, published("M3")])
        self.assertEqual(code, 0, out)
        self.ngrok.connect.assert_called_once()
        self.assertEqual(len(self.servers), 1)
        self.assertEqual(self.servers[0][1], [a, c])
        self.assertEqual(seen, {"b": 404, "a_after_move": 404})
        container_calls = [kw["data"] for (_, url), kw in zip(self.calls, self.kwargs) if url.endswith("/media")]
        self.assertEqual([d["video_url"] for d in container_calls],
                         ["https://abc.ngrok.app/a.mp4", "https://abc.ngrok.app/c.mp4"])
        self.assertEqual(out.count("-> 200 video/mp4"), 2)  # each video HEAD-checked
        self.assertEqual(self.record("posted", "c")["publish_mode"], "ngrok")
        self.assert_torn_down()

    def test_tunnel_failure_publishes_nothing_and_moves_nothing(self):
        self.add_video("a.mp4", "A")
        del self.settings["NGROK_AUTHTOKEN"]
        code, _, err = self.run_cli(["y"], [USER])
        self.assertEqual(code, 1)
        self.assertIn("FAILED at step tunnel: NGROK_AUTHTOKEN is not set", err)
        self.assertIn("a.mp4", self.inbox_files())
        self.assertEqual(self.inbox_files("failed"), [])
        self.assert_torn_down()

    def test_head_failure_on_one_video_does_not_stop_the_next(self):
        self.add_video("a.mp4", "A")
        self.add_video("b.mp4", "B")
        real_check = tunnel.check_public_url

        def check(url):
            if url.endswith("/a.mp4"):
                raise tunnel.TunnelError("Public URL check failed: HTTP 502")
            real_check(url)

        with mock.patch.object(tunnel, "check_public_url", side_effect=check):
            code, _, _ = self.run_cli(["y", "y"], [USER, (200, {"id": "C2"}), FINISHED, published("M2")])
        self.assertEqual(code, 1)
        self.assertEqual(self.record("failed", "a")["step"], "tunnel")
        self.assertEqual(self.record("posted", "b")["media_id"], "M2")
        self.assert_torn_down()

    def test_dry_run_keeps_tunnel_for_batch_and_moves_nothing(self):
        self.add_video("a.mp4", "A")
        self.add_video("b.mp4", "B")
        code, _, _ = self.run_cli(["y", "y"], [USER, (200, {"id": "C1"}), FINISHED, (200, {"id": "C2"}), FINISHED],
                                  "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.media_publishes(), [])
        self.assertEqual(self.inbox_files("posted") + self.inbox_files("failed"), [])
        self.ngrok.connect.assert_called_once()
        self.assert_torn_down()

    def test_interrupt_tears_down_tunnel(self):
        self.add_video("a.mp4", "A")
        code, _, _ = self.run_cli(["y"], [USER, (200, {"id": "C1"}), KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertEqual(self.record("failed", "a")["step"], "poll")
        self.assert_torn_down()


if __name__ == "__main__":
    unittest.main()
