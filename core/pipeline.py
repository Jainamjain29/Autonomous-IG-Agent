"""Pipeline orchestrator for multi-agent execution."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from core.contracts import AgentError, AgentResult, AgentTask, RunSpec
from core.registry import get_agent
from core.runfolder import (
    append_event,
    create_run,
    get_run_dir,
    load_run,
    save_run,
    update_stage_status,
)


def run_pipeline(
    spec: RunSpec,
    base_dir: Union[str, Path] = "runs",
    run_id: Optional[str] = None,
) -> str:
    """Execute stages up to human approval (STOP at PENDING_APPROVAL).
    
    Stages:
    1. content
    2. video_gen (skipped if mode == 'video')
    3. sync_merge (skipped if mode == 'video')
    4. qc
    -> STOP at PENDING_APPROVAL
    """
    if run_id is None:
        run_id = create_run(spec, base_dir=base_dir)

    run_dir = get_run_dir(run_id, base_dir=base_dir)
    inputs: Dict[str, Any] = {}

    # Define stage sequence based on mode
    is_video_mode = spec.mode.lower() == "video"
    if is_video_mode:
        stages = ["content", "qc"]
    else:
        stages = ["content", "video_gen", "sync_merge", "qc"]

    # Mark skipped stages in run.json if in video mode
    if is_video_mode:
        for skipped_stage in ["video_gen", "sync_merge"]:
            update_stage_status(
                run_dir,
                skipped_stage,
                "skipped",
                summary="Skipped due to video mode",
            )

    # Execute active pipeline stages
    for stage_name in stages:
        update_stage_status(run_dir, stage_name, "running")
        try:
            agent = get_agent(stage_name)
        except Exception as e:
            err_msg = f"Failed to acquire agent '{stage_name}': {e}"
            update_stage_status(run_dir, stage_name, "failed", error=err_msg)
            _mark_run_failed(run_dir, err_msg)
            return run_id

        task = AgentTask(
            run_id=run_id,
            run_dir=str(run_dir),
            spec=spec,
            inputs=inputs,
        )

        try:
            result = agent.run(task)
        except Exception as e:
            err_msg = f"Agent '{stage_name}' crashed: {e}"
            update_stage_status(run_dir, stage_name, "failed", error=err_msg)
            _mark_run_failed(run_dir, err_msg)
            return run_id

        if result.status == "failed":
            err_msg = result.error or f"Agent '{stage_name}' reported failure"
            update_stage_status(
                run_dir,
                stage_name,
                "failed",
                summary=result.summary,
                error=err_msg,
                outputs=result.outputs,
            )
            _mark_run_failed(run_dir, err_msg)
            return run_id
        elif result.status == "skipped":
            update_stage_status(
                run_dir,
                stage_name,
                "skipped",
                summary=result.summary,
                outputs=result.outputs,
            )
        else:
            update_stage_status(
                run_dir,
                stage_name,
                "ok",
                summary=result.summary,
                outputs=result.outputs,
            )
            inputs[stage_name] = result.outputs

    # Evaluate QC outcome before marking PENDING_APPROVAL
    run_meta = load_run(run_dir)
    qc_entry = run_meta.get("stages", {}).get("qc", {})
    if qc_entry.get("status") != "ok":
        _mark_run_failed(run_dir, "QC stage did not pass successfully")
        return run_id

    # Check qc/report.json if available
    qc_report_path = run_dir / "qc" / "report.json"
    if qc_report_path.exists():
        try:
            with open(qc_report_path, "r", encoding="utf-8") as f:
                report_data = json.load(f)
                if not report_data.get("passed", False):
                    _mark_run_failed(run_dir, "QC report indicated failure")
                    return run_id
        except Exception as e:
            _mark_run_failed(run_dir, f"Failed to parse QC report: {e}")
            return run_id

    # Pipeline successfully completed generation -> halt for human review
    run_meta["status"] = "PENDING_APPROVAL"
    save_run(run_dir, run_meta)
    append_event(run_dir, "pending_approval", {"message": "Pipeline halted for human review."})
    return run_id


def _mark_run_failed(run_dir: Path, error_message: str) -> None:
    data = load_run(run_dir)
    data["status"] = "FAILED"
    data["error"] = error_message
    save_run(run_dir, data)
    append_event(run_dir, "run_failed", {"error": error_message})


def approve(
    run_id: str,
    reviewer: str = "human",
    notes: str = "",
    base_dir: Union[str, Path] = "runs",
) -> Dict[str, Any]:
    """Approve a run awaiting review and write approval.json."""
    run_dir = get_run_dir(run_id, base_dir=base_dir)
    run_meta = load_run(run_dir)

    current_status = run_meta.get("status")
    if current_status != "PENDING_APPROVAL":
        raise RuntimeError(
            f"Cannot approve run '{run_id}': status is '{current_status}', expected 'PENDING_APPROVAL'."
        )

    now_utc = datetime.now(timezone.utc).isoformat()
    approval_data = {
        "approved": True,
        "reviewer": reviewer,
        "notes": notes,
        "timestamp": now_utc,
    }

    approval_path = run_dir / "approval.json"
    with open(approval_path, "w", encoding="utf-8") as f:
        json.dump(approval_data, f, indent=2)

    run_meta["status"] = "APPROVED"
    save_run(run_dir, run_meta)
    append_event(run_dir, "approved", approval_data)

    return approval_data


def publish(
    run_id: str,
    base_dir: Union[str, Path] = "runs",
) -> AgentResult:
    """Execute the posting agent for an approved run.
    
    Refuses execution if approval.json is missing or if QC did not pass.
    """
    run_dir = get_run_dir(run_id, base_dir=base_dir)
    run_meta = load_run(run_dir)

    # 1. Enforce approval verification
    approval_path = run_dir / "approval.json"
    if not approval_path.exists():
        raise RuntimeError(
            f"Publish refused for run '{run_id}': approval.json not found."
        )

    with open(approval_path, "r", encoding="utf-8") as f:
        approval_data = json.load(f)
    if not approval_data.get("approved"):
        raise RuntimeError(
            f"Publish refused for run '{run_id}': approval.json does not indicate approval."
        )

    # 2. Enforce QC passed verification
    qc_stage = run_meta.get("stages", {}).get("qc", {})
    if qc_stage.get("status") != "ok":
        raise RuntimeError(
            f"Publish refused for run '{run_id}': QC stage status is '{qc_stage.get('status')}', expected 'ok'."
        )

    qc_report = run_dir / "qc" / "report.json"
    if qc_report.exists():
        with open(qc_report, "r", encoding="utf-8") as f:
            qc_data = json.load(f)
            if not qc_data.get("passed", False):
                raise RuntimeError(
                    f"Publish refused for run '{run_id}': QC report failed."
                )

    # 3. Transition to PUBLISHING and execute posting agent
    run_meta["status"] = "PUBLISHING"
    save_run(run_dir, run_meta)
    update_stage_status(run_dir, "posting", "running")

    posting_agent = get_agent("posting")
    spec = RunSpec.from_dict(run_meta.get("spec", {}))
    task = AgentTask(
        run_id=run_id,
        run_dir=str(run_dir),
        spec=spec,
        inputs={"qc": run_meta.get("stages", {}).get("qc", {}).get("outputs", {})},
    )

    try:
        result = posting_agent.run(task)
    except Exception as e:
        err_msg = f"Posting agent crashed: {e}"
        update_stage_status(run_dir, "posting", "failed", error=err_msg)
        _mark_run_failed(run_dir, err_msg)
        return AgentResult(status="failed", error=err_msg)

    if result.status == "ok":
        update_stage_status(
            run_dir,
            "posting",
            "ok",
            summary=result.summary,
            outputs=result.outputs,
        )
        run_meta = load_run(run_dir)
        run_meta["status"] = "COMPLETED"
        save_run(run_dir, run_meta)
        append_event(run_dir, "published", {"summary": result.summary})
    else:
        err_msg = result.error or "Posting agent failed"
        update_stage_status(
            run_dir,
            "posting",
            "failed",
            summary=result.summary,
            error=err_msg,
            outputs=result.outputs,
        )
        _mark_run_failed(run_dir, err_msg)

    return result


def get_status(run_id: str, base_dir: Union[str, Path] = "runs") -> Dict[str, Any]:
    """Retrieve run status summary."""
    run_dir = get_run_dir(run_id, base_dir=base_dir)
    return load_run(run_dir)


def main() -> int:
    """CLI dispatcher: python -m core.pipeline new|approve|publish|status."""
    parser = argparse.ArgumentParser(description="Multi-Agent Pipeline CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # new
    new_parser = subparsers.add_parser("new", help="Create and start a new run")
    new_parser.add_argument("--mode", default="generate", choices=["generate", "script", "character", "video"])
    new_parser.add_argument("--topic", default=None)
    new_parser.add_argument("--script", default=None)
    new_parser.add_argument("--character-text", default=None)
    new_parser.add_argument("--character-image", default=None)
    new_parser.add_argument("--video", default=None, dest="video_path")
    new_parser.add_argument("--platforms", nargs="+", default=["instagram"])

    # approve
    approve_parser = subparsers.add_parser("approve", help="Approve a run in PENDING_APPROVAL")
    approve_parser.add_argument("run_id")
    approve_parser.add_argument("--reviewer", default="human")
    approve_parser.add_argument("--notes", default="")

    # publish
    publish_parser = subparsers.add_parser("publish", help="Publish an approved run")
    publish_parser.add_argument("run_id")

    # status
    status_parser = subparsers.add_parser("status", help="Get status of a run")
    status_parser.add_argument("run_id")

    args = parser.parse_args()

    if args.subcommand == "new":
        spec = RunSpec(
            mode=args.mode,
            topic=args.topic,
            script=args.script,
            character_text=args.character_text,
            character_image=args.character_image,
            video_path=args.video_path,
            platforms=args.platforms,
        )
        run_id = run_pipeline(spec)
        status_info = get_status(run_id)
        print(f"Run created: {run_id}")
        print(f"Current Status: {status_info.get('status')}")
        return 0

    elif args.subcommand == "approve":
        app_data = approve(args.run_id, reviewer=args.reviewer, notes=args.notes)
        print(f"Run {args.run_id} approved: {app_data}")
        return 0

    elif args.subcommand == "publish":
        res = publish(args.run_id)
        print(f"Publish result for {args.run_id}: status={res.status}")
        return 0 if res.status == "ok" else 1

    elif args.subcommand == "status":
        info = get_status(args.run_id)
        print(json.dumps(info, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
