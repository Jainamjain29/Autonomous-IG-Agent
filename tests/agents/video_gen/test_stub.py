import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from core.contracts import AgentTask, RunSpec
from agents.video_gen.agent import VideoGenAgent


class TestVideoGenAgentStub(unittest.TestCase):
    """Contract tests for VideoGenAgent input handling and error reporting."""

    def test_missing_plan_json_fails(self):
        """Missing content/plan.json must fail with a clear error naming plan.json."""
        agent = VideoGenAgent()
        task = AgentTask(run_id="run_test", run_dir="/tmp_nonexistent_run_dir", spec=RunSpec())
        res = agent.run(task)
        self.assertEqual(res.status, "failed")
        self.assertIn("plan.json", str(res.error).lower())


if __name__ == "__main__":
    unittest.main()
