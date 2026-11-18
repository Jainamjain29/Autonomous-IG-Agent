"""Testing utilities, FakeLLM provider, and fixture generators."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import imageio_ffmpeg

from core.contracts import RunSpec
from core.runfolder import create_run, update_stage_status


class FakeLLM:
    """In-memory fake LLM registry and recorder for tests."""
    _instance: Optional[FakeLLM] = None

    def __init__(self):
        self.scripted_responses: Dict[str, Any] = {}
        self.recorded_calls: List[Dict[str, Any]] = []
        self.fail_count: Dict[str, int] = {}
        self.fail_type: Dict[str, str] = {}

    @classmethod
    def get_instance(cls) -> FakeLLM:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = cls()

    def set_response(self, agent: str, response: Union[str, Dict[str, Any]], model: Optional[str] = None) -> None:
        key = f"{agent}:{model}" if model else agent
        self.scripted_responses[key] = response

    def set_transient_failure(self, agent: str, count: int = 1, error_type: str = "timeout") -> None:
        """Configure fake provider to fail `count` times before succeeding."""
        self.fail_count[agent] = count
        self.fail_type[agent] = error_type

    def call(
        self,
        agent: str,
        model: str,
        messages: List[Dict[str, str]],
        expect_json: bool = False,
    ) -> Tuple[str, int, int]:
        from core.llm.providers import RetryableLLMError, LLMError

        self.recorded_calls.append({
            "agent": agent,
            "model": model,
            "messages": messages,
            "expect_json": expect_json,
        })

        # Check configured failures
        if self.fail_count.get(agent, 0) > 0:
            self.fail_count[agent] -= 1
            etype = self.fail_type.get(agent, "timeout")
            if etype == "timeout":
                raise RetryableLLMError("Simulated fake timeout error")
            elif etype == "rate_limit":
                raise RetryableLLMError("Simulated fake rate limit 429 error")
            else:
                raise LLMError("Simulated non-retryable error")

        # Check scripted responses
        key_with_model = f"{agent}:{model}"
        raw = self.scripted_responses.get(key_with_model, self.scripted_responses.get(agent))

        if raw is not None:
            if isinstance(raw, (dict, list)):
                text = json.dumps(raw)
            else:
                text = str(raw)
            return text, 10, len(text.split())

        # Default fallback response
        if expect_json:
            text = json.dumps({"status": "ok", "message": f"fake response for {agent}"})
        else:
            text = f"Fake response from {model} for agent {agent}."

        return text, 15, 20


def make_sample_run(tmp_path: Union[str, Path], spec: Optional[RunSpec] = None) -> Path:
    """Create a fully structured sample run directory for testing."""
    spec = spec or RunSpec(mode="generate", topic="AI testing sample")
    base_dir = Path(tmp_path)
    run_id = create_run(spec, base_dir=base_dir)
    run_dir = base_dir / run_id

    # Create dummy outputs for each stage
    for stage in ["content", "video_gen", "sync_merge", "qc", "posting"]:
        stage_dir = run_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        sample_file = stage_dir / f"{stage}_output.json"
        with open(sample_file, "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "status": "mocked"}, f)
        update_stage_status(run_dir, stage, "ok", summary=f"Sample {stage} completed")

    return run_dir


def make_tiny_mp4(path: Union[str, Path], w: int = 64, h: int = 64, seconds: float = 1.0) -> Path:
    """Generate a tiny valid MP4 video using imageio-ffmpeg for tests."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-y",
        "-f", "lavfi",
        "-i", f"color=c=blue:s={w}x{h}:d={seconds}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-r", "24",
        str(out_path),
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg failed to create tiny MP4: {res.stderr}")

    return out_path
