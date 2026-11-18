import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from agents.content.agent import ContentAgent
from core.contracts import AgentTask, RunSpec


class TestContentAgentStub(unittest.TestCase):
    def test_content_agent_instantiation(self):
        agent = ContentAgent()
        self.assertEqual(agent.name, "content")


if __name__ == "__main__":
    unittest.main()
