"""Base VideoProvider engine interface, data structures, and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type


@dataclass
class Capabilities:
    """Capabilities describing an engine's constraints and features."""
    supports_9x16: bool = True
    supports_reference_image: bool = True
    native_clip_s: Optional[float] = None  # None = variable / arbitrary duration
    max_clip_s: Optional[float] = 15.0


@dataclass
class CharacterModel:
    """Character model passed to every engine."""
    description: str = ""
    reference_image: Optional[str] = None
    locked_block: str = ""

    @classmethod
    def build(cls, description: Optional[str] = None, reference_image: Optional[str] = None) -> CharacterModel:
        """Construct character model and lock the character block text for the run."""
        desc = (description or "").strip()
        ref = (reference_image or "").strip() or None

        lines = ["[CHARACTER SPECIFICATION]"]
        if desc:
            lines.append(f"Visual Description: {desc}")
        else:
            lines.append("Visual Description: Modern stylized character consistency block")
        if ref:
            lines.append(f"Reference Image File: {ref}")
        lines.append("[END CHARACTER SPECIFICATION]")

        locked_block = "\n".join(lines)
        return cls(
            description=desc,
            reference_image=ref,
            locked_block=locked_block,
        )


@dataclass
class ClipInfo:
    """Metadata representing a generated and probed video clip."""
    scene_id: int
    path: str
    duration_s: float
    width: int
    height: int
    provider: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "path": self.path,
            "duration_s": self.duration_s,
            "width": self.width,
            "height": self.height,
            "provider": self.provider,
        }


class VideoProvider(ABC):
    """Abstract base class for all pluggable video generation engines."""

    def __init__(self, name: str, capabilities: Optional[Capabilities] = None):
        self.name = name
        self.capabilities = capabilities or Capabilities()

    @abstractmethod
    def prepare(self, run_ctx: Dict[str, Any], character: CharacterModel) -> None:
        """Initialize engine resources for a specific run. Called once per run."""
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        scene: Dict[str, Any],
        character: CharacterModel,
        out_path: Path,
        target: Optional[Dict[str, Any]] = None,
    ) -> ClipInfo:
        """Generate a video clip for a single scene and write it to out_path.
        
        Args:
            scene: Scene dictionary from plan.json (id, narration, visual_prompt, duration_hint_s).
            character: Locked character model.
            out_path: Target filesystem path where MP4 must be written.
            target: Optional engine parameters (e.g. width, height, fps, duration).
            
        Returns:
            ClipInfo with real probed dimensions and duration.
        """
        raise NotImplementedError


# Registry mapping engine names to engine classes
_PROVIDER_REGISTRY: Dict[str, Type[VideoProvider]] = {}


def register_provider(name: str, provider_cls: Type[VideoProvider]) -> None:
    """Register a VideoProvider class under a given name."""
    _PROVIDER_REGISTRY[name.lower()] = provider_cls


def get_provider(name: str) -> VideoProvider:
    """Retrieve an instantiated VideoProvider by engine name."""
    clean_name = name.lower().strip()
    if clean_name not in _PROVIDER_REGISTRY:
        avail = ", ".join(sorted(_PROVIDER_REGISTRY.keys()))
        raise ValueError(
            f"Unknown video provider '{name}'. Available engines: [{avail}]"
        )
    return _PROVIDER_REGISTRY[clean_name]()


def list_providers() -> List[str]:
    """Return list of registered provider names."""
    return sorted(list(_PROVIDER_REGISTRY.keys()))
