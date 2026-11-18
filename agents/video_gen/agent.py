"""Video Gen Agent stub implementation."""
from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask


class VideoGenAgent(BaseAgent):
    """Generates raw visual clips corresponding to each planned scene."""

    def __init__(self):
        super().__init__(name="video_gen")

    def run(self, task: AgentTask) -> AgentResult:
        return AgentResult(
            status="failed",
            error="not implemented",
            summary="VideoGenAgent stub not implemented",
        )
