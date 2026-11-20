import sys
import unittest
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from core.contracts import AgentTask, RunSpec
from agents.video_gen.agent import VideoGenAgent
from tests.agents.video_gen.test_agent import TestVideoGenAgent
from tests.agents.video_gen.test_claude_code import TestClaudeCodeProvider
from tests.agents.video_gen.test_flow import TestFlowProvider
from tests.agents.video_gen.test_probe import TestVideoProbe

# When imported via tests/test_agents_stubs.py during root test discovery,
# aggregate the video_gen test suites into TestVideoGenAgentStub so all tests execute in the full suite.
_is_aggregated = any("test_agents_stubs" in m for m in sys.modules)

if _is_aggregated:
    class _BaseStubTest(
        TestVideoGenAgent,
        TestClaudeCodeProvider,
        TestFlowProvider,
        TestVideoProbe,
    ):
        def setUp(self):
            TestVideoGenAgent.setUp(self)
            TestClaudeCodeProvider.setUp(self)
            TestFlowProvider.setUp(self)

        def tearDown(self):
            TestFlowProvider.tearDown(self)
            TestClaudeCodeProvider.tearDown(self)
            TestVideoGenAgent.tearDown(self)
else:
    class _BaseStubTest(unittest.TestCase):
        pass


class TestVideoGenAgentStub(_BaseStubTest):
    """Stub contract test matching core step 0 specifications."""

    def test_stub_returns_not_implemented(self):
        agent = VideoGenAgent()
        task = AgentTask(run_id="run_test", run_dir="/tmp", spec=RunSpec())
        res = agent.run(task)
        self.assertEqual(res.status, "failed")
        self.assertIn("not implemented", res.error)


if __name__ == "__main__":
    unittest.main()
