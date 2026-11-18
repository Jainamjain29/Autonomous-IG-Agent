"""CLI runner for the Posting Agent.

Usage:
    python -m agents.posting --run <run_id> [--dry-run] [--platforms instagram,youtube]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agents.posting.agent import PostingAgent
from core.contracts import AgentTask, RunSpec
from core.runfolder import get_run_dir, load_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Posting Agent CLI runner")
    parser.add_argument("--run", required=True, dest="run_id", help="Run ID to publish")
    parser.add_argument("--dry-run", action="store_true", help="Run validations without publishing")
    parser.add_argument(
        "--platforms",
        type=str,
        default=None,
        help="Comma-separated target platforms (e.g. instagram,youtube)",
    )
    parser.add_argument("--base-dir", type=str, default="runs", help="Base runs directory")

    args = parser.parse_args(argv)

    run_dir = get_run_dir(args.run_id, base_dir=args.base_dir)
    if not run_dir.exists():
        print(f"Error: Run directory not found at {run_dir}", file=sys.stderr)
        return 1

    try:
        run_data = load_run(run_dir)
    except Exception as e:
        print(f"Error loading run {args.run_id}: {e}", file=sys.stderr)
        return 1

    spec = RunSpec.from_dict(run_data.get("spec", {}))
    if args.platforms:
        spec.platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]

    task = AgentTask(
        run_id=args.run_id,
        run_dir=str(run_dir),
        spec=spec,
        inputs={"dry_run": args.dry_run},
    )

    agent = PostingAgent(dry_run=args.dry_run)
    result = agent.run(task)

    # Print short per-platform summary
    print(f"Run {args.run_id} posting status: {result.status.upper()}")
    result_json_path = run_dir / "posting" / "result.json"
    if result_json_path.exists():
        try:
            res_content = json.loads(result_json_path.read_text(encoding="utf-8"))
            for post in res_content.get("posts", []):
                p_name = post.get("platform", "unknown")
                p_status = post.get("status", "unknown")
                p_id = post.get("platform_post_id") or "-"
                p_link = post.get("permalink") or "-"
                p_err = post.get("error") or ""
                err_suffix = f" [error: {p_err}]" if p_err else ""
                print(f"  * {p_name}: status={p_status}, id={p_id}, link={p_link}{err_suffix}")
        except Exception:
            pass

    if result.status != "ok":
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
