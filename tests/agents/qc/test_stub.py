import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
while repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

from agents.qc.agent import QCAgent


class TestQCAgentStub(unittest.TestCase):
    def test_qc_agent_instantiation(self):
        agent = QCAgent()
        self.assertEqual(agent.name, "qc")


if __name__ == "__main__":
    unittest.main()
