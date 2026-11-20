"""Tests for pipeline agents."""
from pathlib import Path

# Extend __path__ to include repo_root/agents so that both tests/agents and agents/
# resolve cleanly regardless of whether unittest was invoked with or without `-t .`.
_repo_agents = str(Path(__file__).resolve().parents[2] / "agents")
if _repo_agents not in __path__:
    __path__.append(_repo_agents)
