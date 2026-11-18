"""Sync Merge Agent stub implementation."""
from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask


class SyncMergeAgent(BaseAgent):
    """Synthesizes voiceover, aligns scene timing with Whisper, and renders final video."""

    def __init__(self):
        super().__init__(name="sync_merge")

    def run(self, task: AgentTask) -> AgentResult:
        return AgentResult(
            status="failed",
            error="not implemented",
            summary="SyncMergeAgent stub not implemented",
        )
