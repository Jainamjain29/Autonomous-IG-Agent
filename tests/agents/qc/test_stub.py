import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from agents.qc.agent import QCAgent
from core.contracts import AgentTask, RunSpec


class TestQCAgentStub(unittest.TestCase):
    def test_stub_returns_not_implemented(self):
        agent = QCAgent()
        task = AgentTask(run_id="run_test", run_dir="/tmp", spec=RunSpec())
        res = agent.run(task)
        self.assertEqual(res.status, "failed")
        self.assertIn("not implemented", res.error)


if __name__ == "__main__":
    unittest.main()
