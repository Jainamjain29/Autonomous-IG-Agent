import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

repo_root = str(Path(__file__).resolve().parents[3])
while repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

from agents.qc.agent import QCAgent
from agents.qc import settings
from core.contracts import AgentTask, RunSpec
from core.pipeline import run_pipeline
from core.runfolder import create_run, load_run
from core.testing import FakeLLM
from tests.agents.qc.test_probe_real_media import generate_test_video


class TestQCAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)

        # Generate a valid 1080x1920 test video (3.0s)
        self.valid_video = generate_test_video(
            self.base_dir / "valid_reel.mp4",
            w=1080,
            h=1920,
            duration=3.0,
            with_audio=True,
            silent_audio=False,
            faststart=True,
        )

        # Generate a valid SRT covering 2.8s of the 3.0s video (>80%)
        self.valid_srt = self.base_dir / "subtitles.srt"
        self.valid_srt.write_text(
            "1\n00:00:00,000 --> 00:00:02,800\nToday we test artificial intelligence\n",
            encoding="utf-8",
        )

        # Standard plan.json data
        self.valid_plan = {
            "title": "AI Tech Overview",
            "topic": "ai_tools",
            "hook": "Today we test artificial intelligence",
            "script": "Today we test artificial intelligence",
            "scenes": [
                {
                    "id": 1,
                    "narration": "Today we test artificial intelligence",
                    "visual_prompt": "Futuristic UI",
                    "duration_hint_s": 3.0,
                }
            ],
            "caption": "Today we test artificial intelligence! #ai #tech #future",
            "hashtags": ["#ai", "#tech", "#future"],
            "platform_text": {
                "instagram": {
                    "caption": "Today we test artificial intelligence! #ai #tech #future"
                },
                "youtube": {
                    "title": "AI Tech Overview",
                    "description": "Learn about the latest artificial intelligence tools.",
                },
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_run(
        self,
        spec: RunSpec,
        plan_data: dict,
        video_path: Path,
        srt_path: Path,
        scene_timings: list = None,
        sync_duration: float = 3.0,
    ) -> tuple[str, Path]:
        run_id = create_run(spec, base_dir=self.base_dir)
        run_dir = self.base_dir / run_id

        # Write content/plan.json
        content_dir = run_dir / "content"
        content_dir.mkdir(parents=True, exist_ok=True)
        with open(content_dir / "plan.json", "w", encoding="utf-8") as f:
            json.dump(plan_data, f)

        # Write sync_merge/output.json
        if spec.mode != "video":
            sm_dir = run_dir / "sync_merge"
            sm_dir.mkdir(parents=True, exist_ok=True)
            sm_data = {
                "video_path": str(video_path),
                "srt_path": str(srt_path),
                "duration_s": sync_duration,
                "scene_timings": scene_timings or [{"scene_id": 1, "start_s": 0.0, "end_s": 3.0}],
            }
            with open(sm_dir / "output.json", "w", encoding="utf-8") as f:
                json.dump(sm_data, f)

        return run_id, run_dir

    def test_normal_mode_pass(self):
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, self.valid_video, self.valid_srt)

        agent = QCAgent()
        task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
        res = agent.run(task)

        self.assertEqual(res.status, "ok")
        self.assertIn("QC passed", res.summary)

        # Verify qc/report.json fields
        report_path = run_dir / "qc" / "report.json"
        self.assertTrue(report_path.is_file())
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)

        self.assertIn("passed", report)
        self.assertIn("checks", report)
        self.assertIn("ai_review", report)
        self.assertTrue(report["passed"])
        self.assertIsInstance(report["checks"], list)
        self.assertGreater(len(report["checks"]), 5)

        for c in report["checks"]:
            self.assertIn("name", c)
            self.assertIn("passed", c)
            self.assertIn("severity", c)
            self.assertIn("detail", c)
            self.assertIn(c["severity"], ("blocker", "warn"))

    def test_blocker_fail_stops_pipeline(self):
        # Create a landscape 16:9 video which triggers blocker failure on aspect ratio
        bad_video = generate_test_video(
            self.base_dir / "bad_ratio.mp4",
            w=1920,
            h=1080,
            duration=3.0,
            with_audio=True,
        )
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, bad_video, self.valid_srt)

        agent = QCAgent()
        task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
        res = agent.run(task)

        self.assertEqual(res.status, "failed")
        self.assertIn("video_aspect_ratio", res.error)

        # Verify report.json passed=False
        with open(run_dir / "qc" / "report.json", "r", encoding="utf-8") as f:
            report = json.load(f)
        self.assertFalse(report["passed"])

    def test_warn_only_still_passes(self):
        # 720x1280 resolution: meets minimum (720x1280) blocker, but triggers non-standard 1080x1920 warning
        warn_video = generate_test_video(
            self.base_dir / "warn_720.mp4",
            w=720,
            h=1280,
            duration=3.0,
            with_audio=True,
        )
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, warn_video, self.valid_srt)

        agent = QCAgent()
        task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
        res = agent.run(task)

        self.assertEqual(res.status, "ok")
        with open(run_dir / "qc" / "report.json", "r", encoding="utf-8") as f:
            report = json.load(f)
        self.assertTrue(report["passed"])

        checks_by_name = {c["name"]: c for c in report["checks"]}
        self.assertFalse(checks_by_name["video_resolution_standard"]["passed"])
        self.assertEqual(checks_by_name["video_resolution_standard"]["severity"], "warn")

    def test_existing_video_mode(self):
        # In existing video mode: spec.mode = "video", video_path specified in spec
        spec = RunSpec(mode="video", video_path=str(self.valid_video), platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, self.valid_video, self.valid_srt)

        agent = QCAgent()
        task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
        res = agent.run(task)

        self.assertEqual(res.status, "ok")
        with open(run_dir / "qc" / "report.json", "r", encoding="utf-8") as f:
            report = json.load(f)
        self.assertTrue(report["passed"])

        checks_by_name = {c["name"]: c for c in report["checks"]}
        # Scene timings and narration coverage should be skipped
        self.assertTrue(checks_by_name["scene_timings_validity"]["passed"])
        self.assertIn("skipped", checks_by_name["scene_timings_validity"]["detail"])
        self.assertTrue(checks_by_name["narration_word_coverage"]["passed"])
        self.assertIn("skipped", checks_by_name["narration_word_coverage"]["detail"])

    def test_multi_platform_strictest_wins(self):
        # Duration: 185s video (valid for Instagram <=900s, but fails YouTube Shorts <=180s)
        long_probe_video = generate_test_video(
            self.base_dir / "long_yt.mp4",
            w=1080,
            h=1920,
            duration=3.0,  # real video 3s, but we test multi-platform checks
        )
        spec = RunSpec(mode="generate", platforms=["instagram", "youtube"])
        # Plan missing YouTube title
        plan_no_yt_title = dict(self.valid_plan)
        plan_no_yt_title["platform_text"] = {
            "instagram": {"caption": "Valid caption #ai #tech #future"},
            "youtube": {"title": "", "description": "No title provided"},
        }
        plan_no_yt_title["title"] = ""
        run_id, run_dir = self._setup_run(spec, plan_no_yt_title, long_probe_video, self.valid_srt)

        agent = QCAgent()
        task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
        res = agent.run(task)

        self.assertEqual(res.status, "failed")
        self.assertIn("text_youtube_title_present", res.error)

    def test_ai_critic_disabled(self):
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, self.valid_video, self.valid_srt)

        with mock.patch.object(settings, "ENABLE_AI_CRITIC", False):
            agent = QCAgent()
            task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
            res = agent.run(task)

            self.assertEqual(res.status, "ok")
            with open(run_dir / "qc" / "report.json", "r", encoding="utf-8") as f:
                report = json.load(f)
            ai_rev = report["ai_review"]
            self.assertFalse(ai_rev["enabled"])
            self.assertEqual(ai_rev["model"], "")
            self.assertEqual(ai_rev["suggestions"], [])
            self.assertIsNone(ai_rev["error"])

    def test_ai_critic_enabled_with_fakellm(self):
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, self.valid_video, self.valid_srt)

        fake_llm = FakeLLM.get_instance()
        fake_llm.set_response(
            "qc",
            {
                "suggestions": [
                    {"area": "hook", "suggestion": "Add a stronger question in the first 2 seconds."},
                    {"area": "caption", "suggestion": "Include a call to action at the end."},
                ]
            },
        )

        def fake_exec(provider, model, agent, messages, **kwargs):
            return fake_llm.call(agent, model, messages, expect_json=kwargs.get("expect_json", False))

        with mock.patch("core.llm.gateway.execute_provider_call", side_effect=fake_exec):
            with mock.patch.object(settings, "ENABLE_AI_CRITIC", True):
                agent = QCAgent()
                task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
                res = agent.run(task)

                self.assertEqual(res.status, "ok")
                with open(run_dir / "qc" / "report.json", "r", encoding="utf-8") as f:
                    report = json.load(f)

                ai_rev = report["ai_review"]
                self.assertTrue(ai_rev["enabled"])
                self.assertEqual(len(ai_rev["suggestions"]), 2)
                self.assertEqual(ai_rev["suggestions"][0]["area"], "hook")
                self.assertIsNone(ai_rev["error"])
                self.assertTrue(report["passed"])

    def test_ai_critic_failure_does_not_fail_qc(self):
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, self.valid_video, self.valid_srt)

        with mock.patch("core.llm.gateway.execute_provider_call", side_effect=RuntimeError("LLM simulated crash")):
            with mock.patch.object(settings, "ENABLE_AI_CRITIC", True):
                agent = QCAgent()
                task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
                res = agent.run(task)

                # QC must STILL pass even if critic raises
                self.assertEqual(res.status, "ok")
                with open(run_dir / "qc" / "report.json", "r", encoding="utf-8") as f:
                    report = json.load(f)

                self.assertTrue(report["passed"])
                ai_rev = report["ai_review"]
                self.assertTrue(ai_rev["enabled"])
                self.assertEqual(ai_rev["model"], "")
                self.assertIsNotNone(ai_rev["error"])
                self.assertIn("LLM simulated crash", ai_rev["error"])

    def test_ai_critic_complete_called_without_model_kwarg(self):
        """Assert complete() is called without model kwarg and ai_review.model comes from response."""
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, self.valid_video, self.valid_srt)

        from core.llm.gateway import LLMResponse
        mock_response = LLMResponse(
            text=json.dumps({"suggestions": [{"area": "pacing", "suggestion": "Trim opening hook"}]}),
            data={"suggestions": [{"area": "pacing", "suggestion": "Trim opening hook"}]},
            provider="gemini",
            model="config-resolved-gemini-model",
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.0001,
            latency_ms=120,
        )

        with mock.patch("core.llm.complete", return_value=mock_response) as mock_complete:
            with mock.patch.object(settings, "ENABLE_AI_CRITIC", True):
                agent = QCAgent()
                task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
                res = agent.run(task)

                self.assertEqual(res.status, "ok")
                mock_complete.assert_called_once()
                call_kwargs = mock_complete.call_args.kwargs
                # Assert complete was called with agent="qc" and NO model kwarg
                self.assertEqual(call_kwargs.get("agent"), "qc")
                self.assertNotIn("model", call_kwargs)

                with open(run_dir / "qc" / "report.json", "r", encoding="utf-8") as f:
                    report = json.load(f)

                ai_rev = report["ai_review"]
                self.assertTrue(ai_rev["enabled"])
                self.assertEqual(ai_rev["model"], "config-resolved-gemini-model")
                self.assertEqual(len(ai_rev["suggestions"]), 1)
                self.assertEqual(ai_rev["suggestions"][0]["area"], "pacing")
                self.assertIsNone(ai_rev["error"])

    def test_report_json_exact_contract_shape(self):
        """Assert qc/report.json top-level keys, check keys and severities, and ai_review shape."""
        spec = RunSpec(mode="generate", platforms=["instagram"])
        run_id, run_dir = self._setup_run(spec, self.valid_plan, self.valid_video, self.valid_srt)

        agent = QCAgent()
        task = AgentTask(run_id=run_id, run_dir=str(run_dir), spec=spec)
        res = agent.run(task)

        report_path = run_dir / "qc" / "report.json"
        self.assertTrue(report_path.is_file())

        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)

        # Top-level keys must be exactly {passed, checks, ai_review}
        self.assertEqual(set(report.keys()), {"passed", "checks", "ai_review"})
        self.assertIsInstance(report["passed"], bool)
        self.assertIsInstance(report["checks"], list)
        self.assertGreater(len(report["checks"]), 0)

        # Every check has exactly {name, passed, severity, detail} with severity in {blocker, warn}
        for check in report["checks"]:
            self.assertEqual(
                set(check.keys()),
                {"name", "passed", "severity", "detail"},
                f"Check keys invalid: {check}",
            )
            self.assertIn(check["severity"], {"blocker", "warn"})
            self.assertIsInstance(check["name"], str)
            self.assertIsInstance(check["passed"], bool)
            self.assertIsInstance(check["detail"], str)

        # ai_review has exactly {enabled, model, suggestions, error}
        ai_review = report["ai_review"]
        self.assertEqual(
            set(ai_review.keys()),
            {"enabled", "model", "suggestions", "error"},
        )
        self.assertIsInstance(ai_review["enabled"], bool)
        self.assertIsInstance(ai_review["model"], str)
        self.assertIsInstance(ai_review["suggestions"], list)
        self.assertIn(type(ai_review["error"]), (type(None), str))

    def test_pipeline_stops_before_human_review_on_qc_failure(self):
        # Set up a run with existing-video mode but an unreadable/missing video file
        spec = RunSpec(mode="video", video_path="/nonexistent/missing_reel.mp4", platforms=["instagram"])
        run_id = create_run(spec, base_dir=self.base_dir)
        run_dir = self.base_dir / run_id

        # Write dummy plan
        content_dir = run_dir / "content"
        content_dir.mkdir(parents=True, exist_ok=True)
        with open(content_dir / "plan.json", "w", encoding="utf-8") as f:
            json.dump(self.valid_plan, f)

        # Mock content stage to succeed so pipeline reaches qc
        with mock.patch("core.pipeline.get_agent") as mock_get_agent:
            mock_content = mock.Mock()
            from core.contracts import AgentResult
            mock_content.run.return_value = AgentResult(status="ok", summary="content ok")
            mock_get_agent.side_effect = lambda name: mock_content if name == "content" else QCAgent()

            res_run_id = run_pipeline(spec, base_dir=self.base_dir, run_id=run_id)
            self.assertEqual(res_run_id, run_id)

            run_meta = load_run(run_dir)
            self.assertEqual(run_meta["status"], "FAILED")
            self.assertEqual(run_meta["stages"]["qc"]["status"], "failed")
            self.assertIn("QC", run_meta.get("error", ""))



if __name__ == "__main__":
    unittest.main()
