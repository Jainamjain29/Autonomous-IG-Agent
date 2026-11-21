#!/usr/bin/env python3
"""Runner script to execute QCAgent on a real or test run folder."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest import mock

import yaml

# Ensure repo root is on sys.path
repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from agents.qc import settings
from agents.qc.agent import QCAgent
from core.contracts import AgentTask, RunSpec


def main() -> int:
    parser = argparse.ArgumentParser(description="Run QCAgent on an existing run folder.")
    parser.add_argument("run_dir", help="Path to the run directory (e.g. runs/qc-real-...)")
    parser.add_argument(
        "--critic",
        choices=["on", "off"],
        default="off",
        help="Override settings.ENABLE_AI_CRITIC for this run (default: off)",
    )
    parser.add_argument("--mode", default=None, help="Optional spec mode override (e.g. video, generate)")
    parser.add_argument("--platforms", default=None, help="Optional comma-separated platforms (e.g. instagram,youtube)")
    parser.add_argument("--video-path", default=None, help="Optional video path override")

    args = parser.parse_args()

    # Print git commit and QC settings path as the first output line
    import subprocess
    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            text=True,
        ).strip()
    except Exception:
        git_commit = "unknown"
    settings_path = str(Path(settings.__file__).resolve())
    print(f"[real_run] git_commit={git_commit} settings={settings_path}")

    run_dir = Path(args.run_dir).resolve()

    if not run_dir.is_dir():
        print(f"Error: Run directory not found: {run_dir}", file=sys.stderr)
        return 1

    # Load existing spec.yaml if present, or construct RunSpec
    spec_file = run_dir / "spec.yaml"
    spec_data = {}
    if spec_file.is_file():
        try:
            with open(spec_file, "r", encoding="utf-8") as f:
                spec_data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Warning: Failed to parse spec.yaml: {e}", file=sys.stderr)

    if args.mode:
        spec_data["mode"] = args.mode
    if args.platforms:
        spec_data["platforms"] = [p.strip() for p in args.platforms.split(",")]
    if args.video_path:
        spec_data["video_path"] = args.video_path

    if "mode" not in spec_data:
        spec_data["mode"] = "video" if spec_data.get("video_path") else "generate"
    if "platforms" not in spec_data:
        spec_data["platforms"] = ["instagram"]

    spec = RunSpec.from_dict(spec_data)
    run_id = run_dir.name

    task = AgentTask(
        run_id=run_id,
        run_dir=str(run_dir),
        spec=spec,
    )

    critic_override = True if args.critic == "on" else False

    print(f"Running QCAgent on: {run_dir}")
    print(f"  Mode: {spec.mode}")
    print(f"  Platforms: {spec.platforms}")
    print(f"  Critic override: {args.critic} (settings default: {settings.ENABLE_AI_CRITIC})")

    with mock.patch.object(settings, "ENABLE_AI_CRITIC", critic_override):
        agent = QCAgent()
        result = agent.run(task)

    report_path = run_dir / "qc" / "report.json"
    print("\nQC Execution Finished:")
    print(f"  Status: {result.status}")
    print(f"  Summary: {result.summary}")
    if result.error:
        print(f"  Error: {result.error}")
    print(f"  Report Path: {report_path}")

    if report_path.is_file():
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        print(f"  Passed: {report.get('passed')}")
        failed_checks = [c for c in report.get("checks", []) if not c.get("passed")]
        if failed_checks:
            print(f"  Failed Checks ({len(failed_checks)}):")
            for c in failed_checks:
                print(f"    - [{c.get('severity', '').upper()}] {c.get('name')}: {c.get('detail')}")

    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
