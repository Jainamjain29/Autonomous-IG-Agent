"""Video Gen agent test suite."""
from pathlib import Path

# Extend package __path__ with the real implementation directory
_real_dir = str(Path(__file__).resolve().parents[3] / "agents" / "video_gen")
if _real_dir not in __path__:
    __path__.append(_real_dir)
