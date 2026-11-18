"""BaseAgent definition with shared utilities for input/output handling."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional, Union

from core.contracts import AgentResult, AgentTask


class BaseAgent(ABC):
    """Abstract base class for all pipeline agents."""

    def __init__(self, name: Optional[str] = None):
        self.name = name or self.__class__.__name__.lower()

    @abstractmethod
    def run(self, task: AgentTask) -> AgentResult:
        """Execute the agent's work and return an AgentResult."""
        raise NotImplementedError

    def get_agent_dir(self, task: AgentTask) -> Path:
        """Return and ensure existence of this agent's subdirectory in the run folder."""
        agent_dir = Path(task.run_dir) / self.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return agent_dir

    def read_input_json(self, task: AgentTask, agent_name: str, filename: str) -> Dict[str, Any]:
        """Read and parse a JSON output file produced by an earlier agent."""
        file_path = Path(task.run_dir) / agent_name / filename
        if not file_path.exists():
            raise FileNotFoundError(f"Input file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_output_json(self, task: AgentTask, filename: str, data: Any) -> Path:
        """Write structured data to a JSON file in this agent's subdirectory."""
        agent_dir = self.get_agent_dir(task)
        output_path = agent_dir / filename
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return output_path

    def write_output_file(
        self, task: AgentTask, filename: str, content: Union[str, bytes]
    ) -> Path:
        """Write raw text or binary data to a file in this agent's subdirectory."""
        agent_dir = self.get_agent_dir(task)
        output_path = agent_dir / filename
        if isinstance(content, str):
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            with open(output_path, "wb") as f:
                f.write(content)
        return output_path
