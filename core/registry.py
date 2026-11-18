"""Registry for agent instances and dynamic agent discovery."""
from __future__ import annotations

import importlib
from typing import Dict, Optional, Type

from core.agent import BaseAgent

_AGENT_REGISTRY: Dict[str, BaseAgent] = {}


def register_agent(name: str, agent: BaseAgent) -> None:
    """Register an agent instance under a canonical name."""
    _AGENT_REGISTRY[name.lower()] = agent


def clear_registry() -> None:
    """Clear registered agents (used for test isolation)."""
    _AGENT_REGISTRY.clear()


def get_agent(name: str) -> BaseAgent:
    """Retrieve an agent instance by name, dynamically loading from agents.<name>.agent if needed."""
    key = name.lower()
    if key in _AGENT_REGISTRY:
        return _AGENT_REGISTRY[key]

    # Try dynamic import from agents.<name>.agent
    try:
        mod = importlib.import_module(f"agents.{key}.agent")
        # Look for agent class or instance
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseAgent)
                and attr is not BaseAgent
            ):
                agent_instance = attr()
                register_agent(key, agent_instance)
                return agent_instance
    except (ImportError, AttributeError):
        pass

    raise KeyError(f"No agent registered or found for name '{name}'")
