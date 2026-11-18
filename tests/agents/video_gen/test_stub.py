"""Contract test for VideoGenAgent interface stub."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from core.contracts import AgentTask, RunSpec
from agents.video_gen.agent import VideoGenAgent


class TestVideoGenAgentStub(unittest.TestCase):
    """Stub test matching other agents' stub contract tests."""

    def test_stub_returns_not_implemented(self):
        """Verify handling of nonexistent input directory / missing plan.json."""
        agent = VideoGenAgent()
        task = AgentTask(run_id="run_test", run_dir="/tmp_nonexistent", spec=RunSpec())
        res = agent.run(task)
        self.assertEqual(res.status, "failed")
        self.assertTrue(
            "plan.json" in str(res.error)
            or "not implemented" in str(res.error)
            or "Failed to initialize" in str(res.error)
        )


if __name__ == "__main__":
    unittest.main()
