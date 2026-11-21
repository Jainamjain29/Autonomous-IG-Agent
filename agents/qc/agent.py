"""QC (Quality Control) Agent implementation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask
from agents.qc.checks.coverage import (
    check_narration_coverage,
    check_scene_timings,
    check_subtitles,
)
from agents.qc.checks.text import check_text_compliance
from agents.qc.checks.video import (
    check_aspect_ratio,
    check_audio,
    check_codecs_and_container,
    check_duration,
    check_file_size,
    check_resolution,
    check_video_readable,
)
from agents.qc.critic import run_advisory_critic
from agents.qc.probe import ProbeData, probe_video


class QCAgent(BaseAgent):
    """Inspects video quality, subtitle coverage, scene alignment, and metadata compliance."""

    def __init__(self):
        super().__init__(name="qc")

    def run(self, task: AgentTask) -> AgentResult:
        run_dir = Path(task.run_dir)
        spec = task.spec
        platforms = spec.platforms if spec and spec.platforms else ["instagram"]
        is_existing_video = bool(spec and spec.mode and spec.mode.lower() == "video")

        all_checks: List[Dict[str, Any]] = []

        # 1. Resolve Plan Data (content/plan.json)
        plan_data: Dict[str, Any] = {}
        plan_file = run_dir / "content" / "plan.json"
        if plan_file.is_file():
            try:
                with open(plan_file, "r", encoding="utf-8") as f:
                    plan_data = json.load(f)
            except Exception as e:
                all_checks.append({
                    "name": "plan_file_readable",
                    "passed": False,
                    "severity": "blocker",
                    "detail": f"Failed to parse content/plan.json: {e}",
                })
        else:
            all_checks.append({
                "name": "plan_file_readable",
                "passed": False,
                "severity": "blocker",
                "detail": f"content/plan.json not found in {run_dir / 'content'}",
            })

        # 2. Resolve Sync Merge Data and Video Path
        sync_merge_data: Dict[str, Any] = {}
        sync_merge_file = run_dir / "sync_merge" / "output.json"
        if sync_merge_file.is_file():
            try:
                with open(sync_merge_file, "r", encoding="utf-8") as f:
                    sync_merge_data = json.load(f)
            except Exception:
                pass

        raw_video_path = None
        raw_srt_path = None
        sync_duration = sync_merge_data.get("duration_s")
        scene_timings = sync_merge_data.get("scene_timings", [])

        if is_existing_video:
            raw_video_path = spec.video_path if spec else None
            if not raw_video_path and spec and "video_path" in spec.extra:
                raw_video_path = spec.extra["video_path"]
            if not raw_video_path and "video_path" in task.inputs.get("sync_merge", {}):
                raw_video_path = task.inputs["sync_merge"]["video_path"]
            raw_srt_path = spec.extra.get("srt_path") if spec and hasattr(spec, "extra") else None
        else:
            raw_video_path = sync_merge_data.get("video_path")
            raw_srt_path = sync_merge_data.get("srt_path")

        def resolve_path(p: Optional[str]) -> Optional[Path]:
            if not p:
                return None
            candidate = Path(p)
            if candidate.is_file():
                return candidate.resolve()
            run_rel = run_dir / candidate
            if run_rel.is_file():
                return run_rel.resolve()
            return candidate

        resolved_video_path = resolve_path(raw_video_path)
        resolved_srt_path = resolve_path(raw_srt_path)

        # 3. Probe Video
        if resolved_video_path:
            probe = probe_video(resolved_video_path)
        else:
            probe = ProbeData(file_exists=False, readable=False, error="Video path not specified in inputs")

        # 4. Execute Video Checks
        all_checks.append(check_video_readable(probe))
        if probe.readable:
            all_checks.append(check_aspect_ratio(probe))
            all_checks.extend(check_resolution(probe, platforms))
            all_checks.extend(check_codecs_and_container(probe, platforms, is_existing_video=is_existing_video))
            all_checks.extend(check_duration(probe, platforms, sync_duration, is_existing_video))
            all_checks.extend(check_audio(probe, platforms))
            all_checks.extend(check_file_size(probe, platforms))
        else:
            # Add failing blocker checks if video could not be read
            all_checks.append({
                "name": "video_aspect_ratio",
                "passed": False,
                "severity": "blocker",
                "detail": "Skipped due to unreadable video file",
            })
            all_checks.append({
                "name": "audio_stream_present",
                "passed": False,
                "severity": "blocker",
                "detail": "Skipped due to unreadable video file",
            })

        # 5. Execute Subtitle Checks
        subtitle_checks, srt_text = check_subtitles(
            resolved_srt_path,
            probe.duration_s,
            is_existing_video=is_existing_video,
        )
        all_checks.extend(subtitle_checks)

        # 6. Execute Scene & Narration Coverage Checks
        plan_scenes = plan_data.get("scenes", [])
        all_checks.extend(check_scene_timings(
            plan_scenes,
            scene_timings,
            probe.duration_s,
            is_existing_video=is_existing_video,
        ))

        all_checks.append(check_narration_coverage(
            plan_scenes or plan_data.get("script", ""),
            srt_text,
            is_existing_video=is_existing_video,
        ))

        # 7. Execute Text Compliance Checks
        all_checks.extend(check_text_compliance(plan_data, platforms))

        # 8. Evaluate Pass/Fail Condition
        # passed = true only if NO blocker check failed. Warnings never fail the run.
        blockers = [c for c in all_checks if c.get("severity") == "blocker"]
        failing_blockers = [c for c in blockers if not c.get("passed", False)]
        passed = len(failing_blockers) == 0

        # 9. Execute Advisory AI Critic
        ai_review = run_advisory_critic(plan_data, srt_text, all_checks, run_id=task.run_id)

        # 10. Write Primary Output: qc/report.json
        report_data = {
            "passed": passed,
            "checks": all_checks,
            "ai_review": ai_review,
        }
        report_path = self.write_output_json(task, "report.json", report_data)

        # 11. Return AgentResult
        if passed:
            warning_count = sum(1 for c in all_checks if c.get("severity") == "warn" and not c.get("passed", False))
            summary_msg = f"QC passed: all {len(blockers)} blockers passed"
            if warning_count > 0:
                summary_msg += f" ({warning_count} warnings)"
            return AgentResult(
                status="ok",
                outputs={"report": str(report_path)},
                summary=summary_msg,
            )
        else:
            failed_names = [c["name"] for c in failing_blockers]
            err_msg = f"QC failed on {len(failing_blockers)} blocker(s): {', '.join(failed_names)}"
            return AgentResult(
                status="failed",
                outputs={"report": str(report_path)},
                summary=err_msg,
                error=err_msg,
            )
