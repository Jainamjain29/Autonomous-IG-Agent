"""Comprehensive unit tests for the Posting Agent, adapters, and lock."""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import ig_service
import tunnel
from agents.posting.agent import PostingAgent
from agents.posting.lock import PublishLock, PublishLockError, map_ngrok_error
from agents.posting.platforms.instagram import InstagramAdapter
from agents.posting.platforms.youtube import YouTubeAdapter
from core.contracts import AgentResult, AgentTask, RunSpec
from core.platforms.base import PublishAdapter, PublishRecord
from core.runfolder import create_run
from core.testing import make_tiny_mp4


class FakeAdapter(PublishAdapter):
    """Configurable fake adapter for testing PostingAgent."""

    def __init__(self, platform: str, status: str = "published", error: str | None = None):
        super().__init__(platform=platform)
        self.target_status = status
        self.target_error = error
        self.call_count = 0
        self.last_text = None
        self.last_dry_run = None

    def publish(self, video_path: str, text: str, dry_run: bool = False, **kwargs) -> PublishRecord:
        self.call_count += 1
        self.last_text = text
        self.last_dry_run = dry_run
        return PublishRecord(
            platform=self.platform,
            status=self.target_status,
            platform_post_id=f"id_{self.platform}_123" if self.target_status == "published" else None,
            permalink=f"https://{self.platform}.com/post/123" if self.target_status == "published" else None,
            error=self.target_error,
        )


class TestPostingAgentSuite(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.lock_file = self.base_dir / "test_posting.lock"

        # Create valid run directory
        self.spec = RunSpec(mode="generate", topic="AI Testing", platforms=["instagram"])
        self.run_id = create_run(self.spec, base_dir=self.base_dir)
        self.run_dir = self.base_dir / self.run_id

        # Setup approval.json
        approval_file = self.run_dir / "approval.json"
        approval_file.write_text(
            json.dumps({"approved": True, "reviewer": "tester", "timestamp": "2026-11-18T00:00:00Z"}),
            encoding="utf-8",
        )

        # Setup qc/report.json
        qc_dir = self.run_dir / "qc"
        qc_dir.mkdir(parents=True, exist_ok=True)
        (qc_dir / "report.json").write_text(
            json.dumps({"passed": True, "checks": [{"name": "aspect_ratio", "passed": True}]}),
            encoding="utf-8",
        )

        # Setup sync_merge/output.json & tiny video
        sync_dir = self.run_dir / "sync_merge"
        sync_dir.mkdir(parents=True, exist_ok=True)
        self.video_path = make_tiny_mp4(sync_dir / "final_video.mp4", seconds=0.5)
        (sync_dir / "output.json").write_text(
            json.dumps({"video_path": "sync_merge/final_video.mp4", "duration_s": 0.5}),
            encoding="utf-8",
        )

        # Setup content/plan.json
        content_dir = self.run_dir / "content"
        content_dir.mkdir(parents=True, exist_ok=True)
        (content_dir / "plan.json").write_text(
            json.dumps({
                "caption": "Base caption for testing",
                "hashtags": ["#ai", "#test"],
                "platform_text": {
                    "instagram": {"caption": "Custom IG caption #ai"},
                    "youtube": {"title": "YT Custom Title", "description": "YT Custom Description"},
                },
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _make_task(self, spec: RunSpec | None = None) -> AgentTask:
        return AgentTask(
            run_id=self.run_id,
            run_dir=str(self.run_dir),
            spec=spec or self.spec,
        )

    # -------------------------------------------------------------
    # 1. Approval missing or not approved -> refused
    # -------------------------------------------------------------
    def test_approval_missing_refused(self):
        (self.run_dir / "approval.json").unlink()
        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "failed")
        self.assertIn("approval.json not found", res.error)
        self.assertEqual(fake_ig.call_count, 0)

    def test_approval_false_refused(self):
        (self.run_dir / "approval.json").write_text(
            json.dumps({"approved": False}), encoding="utf-8"
        )
        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "failed")
        self.assertIn("not approved", res.error)
        self.assertEqual(fake_ig.call_count, 0)

    # -------------------------------------------------------------
    # 2. QC missing or failed -> refused
    # -------------------------------------------------------------
    def test_qc_missing_refused(self):
        (self.run_dir / "qc" / "report.json").unlink()
        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "failed")
        self.assertIn("qc/report.json not found", res.error)
        self.assertEqual(fake_ig.call_count, 0)

    def test_qc_failed_refused(self):
        (self.run_dir / "qc" / "report.json").write_text(
            json.dumps({"passed": False}), encoding="utf-8"
        )
        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "failed")
        self.assertIn("passed is false", res.error)
        self.assertEqual(fake_ig.call_count, 0)

    # -------------------------------------------------------------
    # 3. Caption fallback (platform_text.instagram.caption -> caption)
    # -------------------------------------------------------------
    def test_caption_fallback(self):
        (self.run_dir / "content" / "plan.json").write_text(
            json.dumps({
                "caption": "Fallback Caption Only",
                "hashtags": ["#tag1"],
            }),
            encoding="utf-8",
        )
        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "ok")
        self.assertIn("Fallback Caption Only #tag1", fake_ig.last_text)

    # -------------------------------------------------------------
    # 4. Hashtag append + no duplicates (case-insensitive, normalise #)
    # -------------------------------------------------------------
    def test_hashtag_append_and_no_duplicates(self):
        (self.run_dir / "content" / "plan.json").write_text(
            json.dumps({
                "caption": "Exploring AI technology #AI today",
                "hashtags": ["#ai", "AI", "python", "#MachineLearning"],
            }),
            encoding="utf-8",
        )
        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "ok")
        text = fake_ig.last_text
        self.assertEqual(text.lower().count("#ai"), 1)
        self.assertIn("#python", text)
        self.assertIn("#MachineLearning", text)

    # -------------------------------------------------------------
    # 5. 2,200-char and 30-hashtag limits fail before network
    # -------------------------------------------------------------
    def test_caption_char_limit_fails_before_network(self):
        long_caption = "x" * 2201
        (self.run_dir / "content" / "plan.json").write_text(
            json.dumps({"caption": long_caption, "hashtags": []}), encoding="utf-8"
        )
        adapter = InstagramAdapter()
        with mock.patch("agents.posting.platforms.instagram.tunnel.start_file_server") as mock_server:
            res = adapter.publish(str(self.video_path), long_caption)
            self.assertEqual(res.status, "failed")
            self.assertIn("character limit", res.error)
            mock_server.assert_not_called()

    def test_caption_hashtag_limit_fails_before_network(self):
        many_tags = " ".join([f"#tag{i}" for i in range(31)])
        adapter = InstagramAdapter()
        with mock.patch("agents.posting.platforms.instagram.tunnel.start_file_server") as mock_server:
            res = adapter.publish(str(self.video_path), many_tags)
            self.assertEqual(res.status, "failed")
            self.assertIn("hashtag limit", res.error)
            mock_server.assert_not_called()

    # -------------------------------------------------------------
    # 6. Existing-video mode video path
    # -------------------------------------------------------------
    def test_existing_video_mode(self):
        custom_video = make_tiny_mp4(self.run_dir / "custom_input.mp4", seconds=0.5)
        spec = RunSpec(mode="video", video_path="custom_input.mp4", platforms=["instagram"])
        (self.run_dir / "sync_merge" / "output.json").unlink()

        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task(spec))

        self.assertEqual(res.status, "ok")
        self.assertEqual(fake_ig.call_count, 1)

    # -------------------------------------------------------------
    # 7. IG success writes post id + permalink
    # -------------------------------------------------------------
    @mock.patch("agents.posting.platforms.instagram.db.get_setting")
    @mock.patch("agents.posting.platforms.instagram.tunnel.stop")
    @mock.patch("agents.posting.platforms.instagram.tunnel.check_public_url")
    @mock.patch("agents.posting.platforms.instagram.tunnel.url_name", return_value="final_video.mp4")
    @mock.patch("agents.posting.platforms.instagram.tunnel.open_tunnel", return_value="https://tunnel.ngrok-free.app")
    @mock.patch("agents.posting.platforms.instagram.tunnel.start_file_server")
    @mock.patch("agents.posting.platforms.instagram.ig_service.get_config", return_value=("graph.instagram.com", "v26.0", "https://graph.instagram.com/v26.0"))
    @mock.patch("agents.posting.platforms.instagram.ig_service._request", return_value={"permalink": "https://www.instagram.com/reel/Cxxxxxx/"})
    @mock.patch("agents.posting.platforms.instagram.ig_service.publish_reel", return_value="17999888777")
    def test_ig_success_writes_post_id_and_permalink(
        self, mock_pub, mock_req, mock_cfg, mock_srv, mock_open, mock_url, mock_check, mock_stop, mock_db
    ):
        mock_db.side_effect = lambda k: {
            "META_ACCESS_TOKEN": "IGAA_test_token_123",
            "IG_USER_ID": "17841440000",
            "NGROK_AUTHTOKEN": "test_ngrok",
        }.get(k)

        fake_httpd = mock.Mock()
        fake_httpd.server_address = ("127.0.0.1", 8080)
        mock_srv.return_value = fake_httpd

        adapter = InstagramAdapter()
        rec = adapter.publish(str(self.video_path), "Test caption #ai")

        self.assertEqual(rec.status, "published")
        self.assertEqual(rec.platform_post_id, "17999888777")
        self.assertEqual(rec.permalink, "https://www.instagram.com/reel/Cxxxxxx/")
        self.assertIsNone(rec.error)
        mock_stop.assert_called_once_with(fake_httpd)

    # -------------------------------------------------------------
    # 8. Permalink failure: status stays "published" with permalink null and note in error
    # -------------------------------------------------------------
    @mock.patch("agents.posting.platforms.instagram.db.get_setting")
    @mock.patch("agents.posting.platforms.instagram.tunnel.stop")
    @mock.patch("agents.posting.platforms.instagram.tunnel.check_public_url")
    @mock.patch("agents.posting.platforms.instagram.tunnel.url_name", return_value="final_video.mp4")
    @mock.patch("agents.posting.platforms.instagram.tunnel.open_tunnel", return_value="https://tunnel.ngrok-free.app")
    @mock.patch("agents.posting.platforms.instagram.tunnel.start_file_server")
    @mock.patch("agents.posting.platforms.instagram.ig_service.get_config", return_value=("graph.instagram.com", "v26.0", "https://graph.instagram.com/v26.0"))
    @mock.patch("agents.posting.platforms.instagram.ig_service._request", side_effect=Exception("HTTP 500 permalink endpoint down"))
    @mock.patch("agents.posting.platforms.instagram.ig_service.publish_reel", return_value="17999888777")
    def test_permalink_failure_keeps_published(
        self, mock_pub, mock_req, mock_cfg, mock_srv, mock_open, mock_url, mock_check, mock_stop, mock_db
    ):
        mock_db.side_effect = lambda k: {
            "META_ACCESS_TOKEN": "IGAA_test_token_123",
            "IG_USER_ID": "17841440000",
        }.get(k)

        fake_httpd = mock.Mock()
        fake_httpd.server_address = ("127.0.0.1", 8080)
        mock_srv.return_value = fake_httpd

        adapter = InstagramAdapter()
        rec = adapter.publish(str(self.video_path), "Test caption #ai")

        self.assertEqual(rec.status, "published")
        self.assertEqual(rec.platform_post_id, "17999888777")
        self.assertIsNone(rec.permalink)
        self.assertIsNotNone(rec.error)
        self.assertIn("failed to fetch permalink", rec.error)
        mock_stop.assert_called_once_with(fake_httpd)

    # -------------------------------------------------------------
    # 9. IG error mapped to readable string
    # -------------------------------------------------------------
    @mock.patch("agents.posting.platforms.instagram.db.get_setting")
    @mock.patch("agents.posting.platforms.instagram.tunnel.stop")
    @mock.patch("agents.posting.platforms.instagram.tunnel.check_public_url")
    @mock.patch("agents.posting.platforms.instagram.tunnel.url_name", return_value="final_video.mp4")
    @mock.patch("agents.posting.platforms.instagram.tunnel.open_tunnel", return_value="https://tunnel.ngrok-free.app")
    @mock.patch("agents.posting.platforms.instagram.tunnel.start_file_server")
    @mock.patch(
        "agents.posting.platforms.instagram.ig_service.publish_reel",
        side_effect=ig_service.IGPublishError("Media container creation error", step="container"),
    )
    def test_ig_error_mapped(self, mock_pub, mock_srv, mock_open, mock_url, mock_check, mock_stop, mock_db):
        mock_db.side_effect = lambda k: {
            "META_ACCESS_TOKEN": "IGAA_test_token_123",
            "IG_USER_ID": "17841440000",
        }.get(k)
        fake_httpd = mock.Mock()
        fake_httpd.server_address = ("127.0.0.1", 8080)
        mock_srv.return_value = fake_httpd

        adapter = InstagramAdapter()
        rec = adapter.publish(str(self.video_path), "Test caption")

        self.assertEqual(rec.status, "failed")
        self.assertIn("Instagram publish failed [container]: Media container creation error", rec.error)
        mock_stop.assert_called_once_with(fake_httpd)

    # -------------------------------------------------------------
    # 10. Tunnel closed on error
    # -------------------------------------------------------------
    @mock.patch("agents.posting.platforms.instagram.db.get_setting")
    @mock.patch("agents.posting.platforms.instagram.tunnel.stop")
    @mock.patch("agents.posting.platforms.instagram.tunnel.check_public_url")
    @mock.patch("agents.posting.platforms.instagram.tunnel.url_name", return_value="final_video.mp4")
    @mock.patch("agents.posting.platforms.instagram.tunnel.open_tunnel", return_value="https://tunnel.ngrok-free.app")
    @mock.patch("agents.posting.platforms.instagram.tunnel.start_file_server")
    @mock.patch("agents.posting.platforms.instagram.ig_service.publish_reel", side_effect=RuntimeError("Fatal crash inside ig_service"))
    def test_tunnel_closed_on_error(self, mock_pub, mock_srv, mock_open, mock_url, mock_check, mock_stop, mock_db):
        mock_db.side_effect = lambda k: {
            "META_ACCESS_TOKEN": "IGAA_test_token_123",
            "IG_USER_ID": "17841440000",
        }.get(k)
        fake_httpd = mock.Mock()
        fake_httpd.server_address = ("127.0.0.1", 8080)
        mock_srv.return_value = fake_httpd

        adapter = InstagramAdapter()
        rec = adapter.publish(str(self.video_path), "Test caption")

        self.assertEqual(rec.status, "failed")
        mock_stop.assert_called_once_with(fake_httpd)

    # -------------------------------------------------------------
    # 11. YouTube not configured -> skipped and stage not failed
    # -------------------------------------------------------------
    @mock.patch("agents.posting.platforms.youtube.db.get_setting", return_value=None)
    def test_youtube_not_configured_skipped_and_stage_not_failed(self, mock_setting):
        spec = RunSpec(platforms=["youtube"])
        agent = PostingAgent(lock_path=self.lock_file)
        res = agent.run(self._make_task(spec))

        self.assertEqual(res.status, "ok")
        result_file = self.run_dir / "posting" / "result.json"
        data = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["posts"]), 1)
        post = data["posts"][0]
        self.assertEqual(post["platform"], "youtube")
        self.assertEqual(post["status"], "skipped")
        self.assertIn("not configured: Google OAuth not set up", post["error"])

    # -------------------------------------------------------------
    # 12. Partial (IG ok, other fails) -> stage failed
    # -------------------------------------------------------------
    def test_partial_stage_failed(self):
        fake_ig = FakeAdapter("instagram", status="published")
        fake_yt = FakeAdapter("youtube", status="failed", error="Upload quota exceeded")
        spec = RunSpec(platforms=["instagram", "youtube"])
        agent = PostingAgent(adapters={"instagram": fake_ig, "youtube": fake_yt}, lock_path=self.lock_file)
        res = agent.run(self._make_task(spec))

        self.assertEqual(res.status, "failed")
        self.assertIn("youtube: Upload quota exceeded", res.error)

        result_file = self.run_dir / "posting" / "result.json"
        data = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["posts"]), 2)
        by_plat = {p["platform"]: p for p in data["posts"]}
        self.assertEqual(by_plat["instagram"]["status"], "published")
        self.assertEqual(by_plat["youtube"]["status"], "failed")

    # -------------------------------------------------------------
    # 13. Re-run skips published, retries failed
    # -------------------------------------------------------------
    def test_rerun_skips_published_retries_failed(self):
        posting_dir = self.run_dir / "posting"
        posting_dir.mkdir(parents=True, exist_ok=True)
        (posting_dir / "result.json").write_text(
            json.dumps({
                "posts": [
                    {
                        "platform": "instagram",
                        "status": "published",
                        "platform_post_id": "orig_ig_post_1",
                        "permalink": "https://instagram.com/reel/1",
                        "error": None,
                    },
                    {
                        "platform": "youtube",
                        "status": "failed",
                        "platform_post_id": None,
                        "permalink": None,
                        "error": "Temporary upload error",
                    },
                ]
            }),
            encoding="utf-8",
        )

        fake_ig = FakeAdapter("instagram", status="published")
        fake_yt = FakeAdapter("youtube", status="published")
        spec = RunSpec(platforms=["instagram", "youtube"])
        agent = PostingAgent(adapters={"instagram": fake_ig, "youtube": fake_yt}, lock_path=self.lock_file)
        res = agent.run(self._make_task(spec))

        self.assertEqual(res.status, "ok")
        self.assertEqual(fake_ig.call_count, 0)
        self.assertEqual(fake_yt.call_count, 1)

        result_data = json.loads((posting_dir / "result.json").read_text(encoding="utf-8"))
        by_plat = {p["platform"]: p for p in result_data["posts"]}
        self.assertEqual(by_plat["instagram"]["platform_post_id"], "orig_ig_post_1")
        self.assertEqual(by_plat["youtube"]["status"], "published")

    # -------------------------------------------------------------
    # 14. Dry run never counted as published
    # -------------------------------------------------------------
    def test_dry_run_never_counted_as_published(self):
        posting_dir = self.run_dir / "posting"
        posting_dir.mkdir(parents=True, exist_ok=True)
        (posting_dir / "result.json").write_text(
            json.dumps({
                "posts": [
                    {
                        "platform": "instagram",
                        "status": "dry_run",
                        "platform_post_id": None,
                        "permalink": "dry_run: caption",
                        "error": None,
                    }
                ]
            }),
            encoding="utf-8",
        )

        fake_ig = FakeAdapter("instagram", status="published")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "ok")
        self.assertEqual(fake_ig.call_count, 1)

    # -------------------------------------------------------------
    # 15. Lock held -> fail fast ("another publish is running")
    # -------------------------------------------------------------
    def test_lock_held_fails_fast(self):
        lock_data = {
            "run_id": "active_run_999",
            "pid": os.getpid(),
            "host": platform.node(),
            "start_time": "10:00:00 AM IST, 18 Nov 2026",
        }
        self.lock_file.write_text(json.dumps(lock_data), encoding="utf-8")

        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "failed")
        self.assertIn("another publish is running (run active_run_999)", res.error)
        self.assertEqual(fake_ig.call_count, 0)

    # -------------------------------------------------------------
    # 16. Stale lock taken over
    # -------------------------------------------------------------
    def test_stale_lock_taken_over(self):
        lock_data = {
            "run_id": "dead_run_111",
            "pid": 99999999,
            "host": platform.node(),
            "start_time": "08:00:00 AM IST, 18 Nov 2026",
        }
        self.lock_file.write_text(json.dumps(lock_data), encoding="utf-8")

        fake_ig = FakeAdapter("instagram")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        res = agent.run(self._make_task())

        self.assertEqual(res.status, "ok")
        self.assertEqual(fake_ig.call_count, 1)
        self.assertFalse(self.lock_file.exists())

    # -------------------------------------------------------------
    # 17. Lock released on exception
    # -------------------------------------------------------------
    def test_lock_released_on_exception(self):
        class ExplodingAdapter(PublishAdapter):
            def __init__(self):
                super().__init__("instagram")

            def publish(self, *args, **kwargs):
                raise RuntimeError("Catastrophic unexpected failure")

        agent = PostingAgent(adapters={"instagram": ExplodingAdapter()}, lock_path=self.lock_file)
        with self.assertRaises(RuntimeError):
            agent.run(self._make_task())

        self.assertFalse(self.lock_file.exists())

    # -------------------------------------------------------------
    # 18. ngrok session-limit error mapped
    # -------------------------------------------------------------
    def test_ngrok_session_limit_error_mapped(self):
        raw_err = (
            "TunnelError: Could not open ngrok tunnel: PyngrokNgrokHTTPError: "
            "ERR_NGROK_108 Your account is limited to 1 simultaneous ngrok agent sessions."
        )
        mapped = map_ngrok_error(raw_err)
        self.assertEqual(mapped, "shared ngrok account is in use — someone else is publishing")

    # -------------------------------------------------------------
    # 19. Dry run makes zero network/tunnel calls
    # -------------------------------------------------------------
    @mock.patch("agents.posting.platforms.instagram.db.get_setting")
    def test_dry_run_makes_zero_network_or_tunnel_calls(self, mock_db):
        mock_db.side_effect = lambda k: {
            "META_ACCESS_TOKEN": "IGAA_dry_run_token",
            "IG_USER_ID": "17841440000",
            "NGROK_AUTHTOKEN": "dry_ngrok_token",
        }.get(k)

        with (
            mock.patch("agents.posting.platforms.instagram.tunnel.start_file_server") as mock_srv,
            mock.patch("agents.posting.platforms.instagram.tunnel.open_tunnel") as mock_tun,
            mock.patch("agents.posting.platforms.instagram.ig_service.publish_reel") as mock_pub,
        ):
            agent = PostingAgent(dry_run=True, lock_path=self.lock_file)
            res = agent.run(self._make_task())

            self.assertEqual(res.status, "ok")
            mock_srv.assert_not_called()
            mock_tun.assert_not_called()
            mock_pub.assert_not_called()
            self.assertFalse(self.lock_file.exists())

            result_file = self.run_dir / "posting" / "result.json"
            data = json.loads(result_file.read_text(encoding="utf-8"))
            post = data["posts"][0]
            self.assertEqual(post["status"], "dry_run")
            self.assertIsNone(post["error"])
            self.assertIn("dry_run: Custom IG caption #ai", post["permalink"])

    # -------------------------------------------------------------
    # 20. result.json matches README fields exactly
    # -------------------------------------------------------------
    def test_result_json_matches_readme_fields_exactly(self):
        fake_ig = FakeAdapter("instagram", status="published")
        agent = PostingAgent(adapters={"instagram": fake_ig}, lock_path=self.lock_file)
        agent.run(self._make_task())

        result_path = self.run_dir / "posting" / "result.json"
        self.assertTrue(result_path.exists())
        data = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertIn("posts", data)
        self.assertIsInstance(data["posts"], list)
        expected_keys = {"platform", "status", "platform_post_id", "permalink", "error"}

        for p in data["posts"]:
            self.assertEqual(set(p.keys()), expected_keys)
            self.assertEqual(p["platform"], "instagram")
            self.assertEqual(p["status"], "published")

    # -------------------------------------------------------------
    # 21. CLI entrypoint test
    # -------------------------------------------------------------
    @mock.patch("agents.posting.platforms.instagram.db.get_setting")
    def test_cli_runner(self, mock_db):
        from agents.posting.__main__ import main as cli_main

        mock_db.side_effect = lambda k: {
            "META_ACCESS_TOKEN": "IGAA_dry_run_token",
            "IG_USER_ID": "17841440000",
            "NGROK_AUTHTOKEN": "dry_ngrok_token",
        }.get(k)

        with mock.patch("sys.stdout") as mock_out:
            exit_code = cli_main([
                "--run", self.run_id,
                "--base-dir", str(self.base_dir),
                "--dry-run",
                "--platforms", "instagram",
            ])
            self.assertEqual(exit_code, 0)



if __name__ == "__main__":
    unittest.main()
