"""Run folder creation, status tracking, and event journaling."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml

from core.contracts import RunSpec

DEFAULT_STAGES = ["content", "video_gen", "sync_merge", "qc", "posting"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_run_dir(run_id: str, base_dir: Union[str, Path] = "runs") -> Path:
    """Return the Path to a run directory given its ID."""
    return Path(base_dir) / run_id


def create_run(spec: RunSpec, base_dir: Union[str, Path] = "runs") -> str:
    """Create a new run directory with spec.yaml, run.json, and events.jsonl.
    
    Returns the created run_id.
    """
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_uid = uuid.uuid4().hex[:6]
    run_id = f"run_{ts_str}_{short_uid}"

    run_dir = get_run_dir(run_id, base_dir=base_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. spec.yaml
    spec_path = run_dir / "spec.yaml"
    with open(spec_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec.to_dict(), f, sort_keys=False)

    # 2. run.json
    now = _now_iso()
    run_meta = {
        "run_id": run_id,
        "status": "RUNNING",
        "created_at": now,
        "updated_at": now,
        "spec": spec.to_dict(),
        "stages": {stage: {"status": "pending"} for stage in DEFAULT_STAGES},
    }
    run_json_path = run_dir / "run.json"
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(run_meta, f, indent=2)

    # 3. events.jsonl
    events_path = run_dir / "events.jsonl"
    events_path.touch()

    # Log run_created event
    append_event(run_dir, "run_created", {"spec": spec.to_dict()})

    return run_id


def append_event(
    run_dir: Union[str, Path], event_type: str, data: Optional[Dict[str, Any]] = None
) -> None:
    """Append a structured event line to events.jsonl."""
    events_path = Path(run_dir) / "events.jsonl"
    record = {
        "ts": _now_iso(),
        "event": event_type,
        "data": data or {},
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_run(run_id_or_dir: Union[str, Path], base_dir: Union[str, Path] = "runs") -> Dict[str, Any]:
    """Load and return run.json."""
    path = Path(run_id_or_dir)
    if not path.is_dir():
        path = get_run_dir(str(run_id_or_dir), base_dir=base_dir)
    run_json_path = path / "run.json"
    if not run_json_path.exists():
        raise FileNotFoundError(f"Run metadata not found at {run_json_path}")
    with open(run_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_run(run_dir: Union[str, Path], run_data: Dict[str, Any]) -> None:
    """Persist run_data to run.json with an updated timestamp."""
    run_data["updated_at"] = _now_iso()
    run_json_path = Path(run_dir) / "run.json"
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(run_data, f, indent=2)


def update_stage_status(
    run_dir: Union[str, Path],
    stage: str,
    status: str,
    summary: str = "",
    error: Optional[str] = None,
    outputs: Optional[Dict[str, str]] = None,
) -> None:
    """Update status of a specific stage inside run.json and record an event."""
    data = load_run(run_dir)
    stages = data.setdefault("stages", {})
    stage_entry = stages.setdefault(stage, {})
    stage_entry["status"] = status
    if summary:
        stage_entry["summary"] = summary
    if error:
        stage_entry["error"] = error
    if outputs:
        stage_entry["outputs"] = outputs
    stage_entry["updated_at"] = _now_iso()
    save_run(run_dir, data)

    append_event(
        run_dir,
        f"stage_{stage}_{status}",
        {"stage": stage, "status": status, "summary": summary, "error": error},
    )
