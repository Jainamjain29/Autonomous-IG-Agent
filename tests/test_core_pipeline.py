import sys
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[1])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from tests.core.test_pipeline import TestPipelineOrchestrator

__all__ = ["TestPipelineOrchestrator"]
