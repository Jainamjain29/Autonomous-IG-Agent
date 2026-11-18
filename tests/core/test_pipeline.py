"""End-to-end tests for the multi-agent pipeline orchestrator."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[2])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask, RunSpec
from core.pipeline import approve, publish, run_pipeline, get_status
from core.registry import register_agent, clear_registry


class FakeContentAgent(BaseAgent):
    def __init__(self):
        super().__init__(name="content")

    def run(self, task: AgentTask) -> AgentResult:
        plan_path = self.write_output_json(
            task,
            "plan.json",
            {
                "title": "Test Title",
                "topic": task.spec.topic or "Test Topic",
                "script": "Narration text",
                "scenes": [{"id": 1, "narration": "Narration", "visual_prompt": "Prompt"}],
            },
        )
        return AgentResult(status="ok", outputs={"plan": str(plan_path)}, summary="Plan generated")


class FakeVideoGenAgent(BaseAgent):
    def __init__(self):
        super().__init__(name="video_gen")

    def run(self, task: AgentTask) -> AgentResult:
        clips_path = self.write_output_json(
            task,
            "clips.json",
            {"clips": [{"scene_id": 1, "path": "scene_1.mp4", "duration_s": 5.0}]},
        )
        return AgentResult(status="ok", outputs={"clips": str(clips_path)}, summary="Clips generated")


class FakeSyncMergeAgent(BaseAgent):
    def __init__(self):
        super().__init__(name="sync_merge")

    def run(self, task: AgentTask) -> AgentResult:
        out_path = self.write_output_json(
            task,
            "output.json",
            {
                "video_path": "final_video.mp4",
                "audio_path": "audio.mp3",
                "duration_s": 5.0,
            },
        )
        return AgentResult(status="ok", outputs={"video": str(out_path)}, summary="Video synced and merged")


class FakeQCAgent(BaseAgent):
    def __init__(self, should_pass: bool = True):
        super().__init__(name="qc")
        self.should_pass = should_pass

    def run(self, task: AgentTask) -> AgentResult:
        report_path = self.write_output_json(
            task,
            "report.json",
            {
                "passed": self.should_pass,
                "checks": [{"name": "aspect_ratio", "passed": self.should_pass, "severity": "blocker"}],
                "ai_review": "Looking good" if self.should_pass else "Quality poor",
            },
        )
        status = "ok" if self.should_pass else "failed"
        return AgentResult(
            status=status,
            outputs={"report": str(report_path)},
            summary="QC passed" if self.should_pass else "QC failed",
        )


class FakePostingAgent(BaseAgent):
    def __init__(self):
        super().__init__(name="posting")

    def run(self, task: AgentTask) -> AgentResult:
        res_path = self.write_output_json(
            task,
            "result.json",
            {
                "posts": [
                    {
                        "platform": "instagram",
                        "status": "ok",
                        "platform_post_id": "17841440000",
                        "permalink": "https://instagram.com/reel/xyz",
                    }
                ]
            },
        )
        return AgentResult(status="ok", outputs={"result": str(res_path)}, summary="Published to Instagram")


class TestPipelineOrchestrator(unittest.TestCase):
    def setUp(self):
        clear_registry()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

        # Register fake pipeline agents
        register_agent("content", FakeContentAgent())
        register_agent("video_gen", FakeVideoGenAgent())
        register_agent("sync_merge", FakeSyncMergeAgent())
        register_agent("qc", FakeQCAgent(should_pass=True))
        register_agent("posting", FakePostingAgent())

    def tearDown(self):
        self.tmp_dir.cleanup()
        clear_registry()

    def test_pipeline_normal_run_halts_at_pending_approval(self):
        """Pipeline runs content -> video_gen -> sync_merge -> qc and STOPS at PENDING_APPROVAL."""
        spec = RunSpec(mode="generate", topic="Autonomous AI")
        run_id = run_pipeline(spec, base_dir=self.base_dir)

        status_data = get_status(run_id, base_dir=self.base_dir)
        self.assertEqual(status_data["status"], "PENDING_APPROVAL")
        self.assertEqual(status_data["stages"]["content"]["status"], "ok")
        self.assertEqual(status_data["stages"]["video_gen"]["status"], "ok")
        self.assertEqual(status_data["stages"]["sync_merge"]["status"], "ok")
        self.assertEqual(status_data["stages"]["qc"]["status"], "ok")
        self.assertEqual(status_data["stages"]["posting"]["status"], "pending")

    def test_publish_refused_without_approval(self):
        """Publishing must fail if approve() was not called."""
        spec = RunSpec(mode="generate", topic="Test Refusal")
        run_id = run_pipeline(spec, base_dir=self.base_dir)

        with self.assertRaises(RuntimeError) as ctx:
            publish(run_id, base_dir=self.base_dir)
        self.assertIn("approval.json not found", str(ctx.exception))

    def test_approve_and_publish_success(self):
        """Pipeline can be approved and then published successfully."""
        spec = RunSpec(mode="generate", topic="Approval Flow")
        run_id = run_pipeline(spec, base_dir=self.base_dir)

        # Approve
        approval = approve(run_id, reviewer="alice", notes="Looks great", base_dir=self.base_dir)
        self.assertTrue(approval["approved"])

        status_after_approval = get_status(run_id, base_dir=self.base_dir)
        self.assertEqual(status_after_approval["status"], "APPROVED")

        # Publish
        res = publish(run_id, base_dir=self.base_dir)
        self.assertEqual(res.status, "ok")

        final_status = get_status(run_id, base_dir=self.base_dir)
        self.assertEqual(final_status["status"], "COMPLETED")
        self.assertEqual(final_status["stages"]["posting"]["status"], "ok")

    def test_video_mode_skips_video_gen_and_sync_merge(self):
        """Mode 'video' skips video_gen and sync_merge stages directly to qc."""
        spec = RunSpec(mode="video", video_path="workspace/input_video.mp4")
        run_id = run_pipeline(spec, base_dir=self.base_dir)

        status_data = get_status(run_id, base_dir=self.base_dir)
        self.assertEqual(status_data["status"], "PENDING_APPROVAL")
        self.assertEqual(status_data["stages"]["content"]["status"], "ok")
        self.assertEqual(status_data["stages"]["video_gen"]["status"], "skipped")
        self.assertEqual(status_data["stages"]["sync_merge"]["status"], "skipped")
        self.assertEqual(status_data["stages"]["qc"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
