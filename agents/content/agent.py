"""Content Agent stub implementation."""
from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask


class ContentAgent(BaseAgent):
    """Generates video concepts, scripts, scene prompts, and metadata."""

    def __init__(self):
        super().__init__(name="content")

    def run(self, task: AgentTask) -> AgentResult:
        return AgentResult(
            status="failed",
            error="not implemented",
            summary="ContentAgent stub not implemented",
        )
