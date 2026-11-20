"""Video Gen agent test suite."""
from pathlib import Path

_repo_subagent = str(Path(__file__).resolve().parents[3] / "agents" / Path(__file__).parent.name)
if _repo_subagent not in __path__:
    __path__.append(_repo_subagent)
