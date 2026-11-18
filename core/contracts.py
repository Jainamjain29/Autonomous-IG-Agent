"""Core contracts and data structures for agents and workflow runs."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


class AgentError(Exception):
    """Base exception for agent errors."""
    pass


@dataclass
class RunSpec:
    """Specification defining the parameters of a video creation/publishing run."""
    mode: str = "generate"  # generate | script | character | video
    topic: Optional[str] = None
    script: Optional[str] = None
    character_text: Optional[str] = None
    character_image: Optional[str] = None
    video_path: Optional[str] = None
    platforms: List[str] = field(default_factory=lambda: ["instagram"])
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RunSpec:
        valid_fields = {
            "mode", "topic", "script", "character_text",
            "character_image", "video_path", "platforms", "extra"
        }
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class AgentTask:
    """Task assigned to an agent during a pipeline run."""
    run_id: str
    run_dir: str
    spec: RunSpec
    inputs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "spec": self.spec.to_dict(),
            "inputs": self.inputs,
        }


@dataclass
class AgentResult:
    """Result returned by an agent after execution."""
    status: str  # ok | failed | skipped
    outputs: Dict[str, str] = field(default_factory=dict)  # map of key to output file path
    summary: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AgentResult:
        return cls(
            status=data.get("status", "failed"),
            outputs=data.get("outputs", {}),
            summary=data.get("summary", ""),
            error=data.get("error"),
        )
