"""Posting Agent stub implementation."""
from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask


class PostingAgent(BaseAgent):
    """Dispatches approved videos to social media platforms via publish adapters."""

    def __init__(self):
        super().__init__(name="posting")

    def run(self, task: AgentTask) -> AgentResult:
        return AgentResult(
            status="failed",
            error="not implemented",
            summary="PostingAgent stub not implemented",
        )
