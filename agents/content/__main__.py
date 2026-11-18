"""CLI entrypoint for running the Content Agent directly against a run folder."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from agents.content.agent import ContentAgent
from core.contracts import AgentTask, RunSpec


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute Content Agent on a run directory.")
    parser.add_argument("run_dir", type=str, help="Path to the target run directory")
    args = parser.parse_args()

    run_path = Path(args.run_dir).resolve()
    if not run_path.exists():
        print(f"Error: Run directory does not exist: {run_path}", file=sys.stderr)
        return 1

    spec_path = run_path / "spec.yaml"
    if not spec_path.exists():
        print(f"Error: spec.yaml not found in {run_path}", file=sys.stderr)
        return 1

    with open(spec_path, "r", encoding="utf-8") as f:
        spec_data = yaml.safe_load(f) or {}

    spec = RunSpec.from_dict(spec_data)
    run_id = run_path.name

    task = AgentTask(
        run_id=run_id,
        run_dir=str(run_path),
        spec=spec,
    )

    agent = ContentAgent()
    result = agent.run(task)

    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
