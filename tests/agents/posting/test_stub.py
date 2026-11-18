import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from agents.posting.agent import PostingAgent
from tests.agents.posting.test_posting import TestPostingAgentSuite


class TestPostingAgentStub(TestPostingAgentSuite):
    def test_agent_initialization(self):
        agent = PostingAgent()
        self.assertEqual(agent.name, "posting")




if __name__ == "__main__":
    unittest.main()
