"""Posting Agent: publishes approved videos across social platforms."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.posting.lock import PublishLock, PublishLockError
from agents.posting.platforms.instagram import InstagramAdapter
from agents.posting.platforms.youtube import YouTubeAdapter
from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask
from core.platforms.base import PublishAdapter, PublishRecord
from core.runfolder import append_event


class PostingAgent(BaseAgent):
    """Dispatches approved videos to social media platforms via publish adapters."""

    def __init__(
        self,
        dry_run: bool = False,
        adapters: Optional[Dict[str, PublishAdapter]] = None,
        lock_path: Optional[Path] = None,
    ):
        super().__init__(name="posting")
        self.dry_run = dry_run
        self.adapters = adapters or {}
        self.lock_path = lock_path

    def _get_adapter(self, platform: str) -> Optional[PublishAdapter]:
        if platform in self.adapters:
            return self.adapters[platform]
        if platform == "instagram":
            return InstagramAdapter()
        elif platform == "youtube":
            return YouTubeAdapter()
        return None

    def run(self, task: AgentTask) -> AgentResult:
        run_dir = Path(task.run_dir)
        effective_dry_run = (
            self.dry_run
            or getattr(task, "dry_run", False)
            or bool(task.spec.extra.get("dry_run", False))
        )

        # ---------------------------------------------------------
        # 1. Gates verification
        # ---------------------------------------------------------
        # 1a. Human approval gate
        approval_file = run_dir / "approval.json"
        if not approval_file.exists():
            msg = f"Publish refused for run '{task.run_id}': approval.json not found."
            append_event(run_dir, "posting_gate_failed", {"gate": "approval", "reason": "missing"})
            return AgentResult(status="failed", error=msg, summary="Publish refused: missing approval")

        try:
            approval_data = json.loads(approval_file.read_text(encoding="utf-8"))
        except Exception as e:
            msg = f"Publish refused for run '{task.run_id}': cannot parse approval.json: {e}"
            append_event(run_dir, "posting_gate_failed", {"gate": "approval", "reason": "parse_error"})
            return AgentResult(status="failed", error=msg, summary="Publish refused: corrupted approval.json")

        if not approval_data.get("approved"):
            msg = f"Publish refused for run '{task.run_id}': approval.json indicates run is not approved."
            append_event(run_dir, "posting_gate_failed", {"gate": "approval", "reason": "not_approved"})
            return AgentResult(status="failed", error=msg, summary="Publish refused: unapproved run")

        append_event(run_dir, "posting_gate_passed", {"gate": "approval"})

        # 1b. QC passed gate
        qc_file = run_dir / "qc" / "report.json"
        if not qc_file.exists():
            msg = f"Publish refused for run '{task.run_id}': qc/report.json not found."
            append_event(run_dir, "posting_gate_failed", {"gate": "qc", "reason": "missing"})
            return AgentResult(status="failed", error=msg, summary="Publish refused: missing QC report")

        try:
            qc_data = json.loads(qc_file.read_text(encoding="utf-8"))
        except Exception as e:
            msg = f"Publish refused for run '{task.run_id}': cannot parse qc/report.json: {e}"
            append_event(run_dir, "posting_gate_failed", {"gate": "qc", "reason": "parse_error"})
            return AgentResult(status="failed", error=msg, summary="Publish refused: corrupted QC report")

        if not qc_data.get("passed"):
            msg = f"Publish refused for run '{task.run_id}': qc/report.json passed is false."
            append_event(run_dir, "posting_gate_failed", {"gate": "qc", "reason": "failed_qc"})
            return AgentResult(status="failed", error=msg, summary="Publish refused: QC did not pass")

        append_event(run_dir, "posting_gate_passed", {"gate": "qc"})

        # ---------------------------------------------------------
        # 2. Video path resolution & validation
        # ---------------------------------------------------------
        is_video_mode = task.spec.mode.lower() == "video"
        if is_video_mode:
            raw_video_path = task.spec.video_path
            if not raw_video_path:
                msg = f"Publish failed for run '{task.run_id}': video mode specified without video_path."
                return AgentResult(status="failed", error=msg, summary="Missing video_path in spec")
            video_file = Path(raw_video_path)
            if not video_file.is_absolute():
                candidate = run_dir / video_file
                if candidate.exists():
                    video_file = candidate
        else:
            sync_file = run_dir / "sync_merge" / "output.json"
            if not sync_file.exists():
                msg = f"Publish failed for run '{task.run_id}': sync_merge/output.json not found."
                return AgentResult(status="failed", error=msg, summary="Missing sync_merge output")
            try:
                sync_data = json.loads(sync_file.read_text(encoding="utf-8"))
                raw_video_path = sync_data.get("video_path")
            except Exception as e:
                msg = f"Publish failed for run '{task.run_id}': failed to parse sync_merge/output.json: {e}"
                return AgentResult(status="failed", error=msg, summary="Corrupted sync_merge output")

            if not raw_video_path:
                msg = f"Publish failed for run '{task.run_id}': sync_merge/output.json missing video_path."
                return AgentResult(status="failed", error=msg, summary="Missing video_path in sync_merge")

            video_file = Path(raw_video_path)
            if not video_file.is_absolute():
                video_file = run_dir / video_file

        if not video_file.is_file() or video_file.stat().st_size == 0:
            msg = f"Publish failed for run '{task.run_id}': video file does not exist or is empty ({video_file})."
            return AgentResult(status="failed", error=msg, summary="Video file missing or empty")

        # ---------------------------------------------------------
        # 3. Content plan & captions preparation
        # ---------------------------------------------------------
        plan_file = run_dir / "content" / "plan.json"
        plan_data: Dict[str, Any] = {}
        if plan_file.exists():
            try:
                plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception:
                plan_data = {}

        # Instagram caption & hashtag formatting
        ig_text = plan_data.get("platform_text", {}).get("instagram", {}).get("caption")
        if not ig_text:
            ig_text = plan_data.get("caption", "")

        hashtags_list = plan_data.get("hashtags", [])
        final_ig_caption = self._format_instagram_caption(ig_text, hashtags_list)

        # YouTube metadata
        yt_meta = plan_data.get("platform_text", {}).get("youtube", {})
        yt_title = yt_meta.get("title") or plan_data.get("title") or task.spec.topic or "Shorts Video"
        yt_desc = yt_meta.get("description") or final_ig_caption
        yt_privacy = (
            task.spec.extra.get("privacy")
            or task.spec.extra.get("youtube", {}).get("privacy")
            or "private"
        )

        # ---------------------------------------------------------
        # 4. Target platforms & Idempotency check
        # ---------------------------------------------------------
        selected_platforms = list(task.spec.platforms) if task.spec.platforms else ["instagram"]

        result_path = run_dir / "posting" / "result.json"
        existing_posts_by_platform: Dict[str, Dict[str, Any]] = {}
        if result_path.exists():
            try:
                prev_res = json.loads(result_path.read_text(encoding="utf-8"))
                for p in prev_res.get("posts", []):
                    existing_posts_by_platform[p.get("platform")] = p
            except Exception:
                existing_posts_by_platform = {}

        # Idempotency rule: platforms already marked "published" (or legacy "ok") are skipped.
        # dry_run is NEVER treated as published.
        platforms_to_run: List[str] = []
        posts_out: List[Dict[str, Any]] = []

        for p_name in selected_platforms:
            prev_entry = existing_posts_by_platform.get(p_name)
            if prev_entry and prev_entry.get("status") in ("published", "ok"):
                # Retain published post exactly as is
                posts_out.append(prev_entry)
                append_event(
                    run_dir,
                    "posting_platform_skipped_idempotent",
                    {"platform": p_name, "post_id": prev_entry.get("platform_post_id")},
                )
            else:
                platforms_to_run.append(p_name)

        # If all platforms already published, write result and return ok
        if not platforms_to_run:
            self._write_result(task, posts_out)
            return AgentResult(
                status="ok",
                outputs={"result": str(result_path)},
                summary="All target platforms already published (idempotent skip)",
            )

        # ---------------------------------------------------------
        # 5. Lock acquisition (if not dry run)
        # ---------------------------------------------------------
        lock: Optional[PublishLock] = None
        if not effective_dry_run:
            lock = PublishLock(run_id=task.run_id, run_dir=str(run_dir), lock_path=self.lock_path)
            try:
                lock.acquire()
            except PublishLockError as e:
                return AgentResult(
                    status="failed",
                    error=str(e),
                    summary=f"Lock conflict: {e}",
                )

        try:
            # -----------------------------------------------------
            # 6. Execute per-platform publishing
            # -----------------------------------------------------
            for platform_name in platforms_to_run:
                append_event(
                    run_dir,
                    "posting_platform_start",
                    {"platform": platform_name, "dry_run": effective_dry_run},
                )
                adapter = self._get_adapter(platform_name)
                if not adapter:
                    record = PublishRecord(
                        platform=platform_name,
                        status="failed",
                        error=f"Unsupported platform: {platform_name}",
                    )
                else:
                    if platform_name == "instagram":
                        record = adapter.publish(
                            video_path=str(video_file),
                            text=final_ig_caption,
                            dry_run=effective_dry_run,
                        )
                    elif platform_name == "youtube":
                        record = adapter.publish(
                            video_path=str(video_file),
                            text=final_ig_caption,
                            dry_run=effective_dry_run,
                            title=yt_title,
                            description=yt_desc,
                            privacy=yt_privacy,
                        )
                    else:
                        record = adapter.publish(
                            video_path=str(video_file),
                            text=final_ig_caption,
                            dry_run=effective_dry_run,
                        )

                post_entry = {
                    "platform": record.platform,
                    "status": record.status,
                    "platform_post_id": record.platform_post_id,
                    "permalink": record.permalink,
                    "error": record.error,
                }
                posts_out.append(post_entry)

                append_event(
                    run_dir,
                    "posting_platform_result",
                    {
                        "platform": record.platform,
                        "status": record.status,
                        "platform_post_id": record.platform_post_id,
                        "permalink": record.permalink,
                        "error": record.error,
                    },
                )

        finally:
            if lock is not None:
                lock.release()

        # ---------------------------------------------------------
        # 7. Write result.json
        # ---------------------------------------------------------
        self._write_result(task, posts_out)

        # ---------------------------------------------------------
        # 8. Evaluate overall stage status
        # ---------------------------------------------------------
        # Stage fails if any platform failed.
        # "skipped" (e.g. not configured) does NOT fail the stage.
        failed_platforms = [p for p in posts_out if p["status"] == "failed"]
        if failed_platforms:
            err_details = "; ".join(f"{p['platform']}: {p['error']}" for p in failed_platforms)
            return AgentResult(
                status="failed",
                outputs={"result": str(result_path)},
                summary=f"Posting failed on: {', '.join(p['platform'] for p in failed_platforms)}",
                error=err_details,
            )

        summary_tokens = [f"{p['platform']} ({p['status']})" for p in posts_out]
        return AgentResult(
            status="ok",
            outputs={"result": str(result_path)},
            summary=f"Posting completed: {', '.join(summary_tokens)}",
        )

    def _write_result(self, task: AgentTask, posts: List[Dict[str, Any]]) -> Path:
        agent_dir = self.get_agent_dir(task)
        result_path = agent_dir / "result.json"
        data = {"posts": posts}
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return result_path

    @staticmethod
    def _format_instagram_caption(caption_text: str, hashtags: List[Any]) -> str:
        """Format Instagram caption, appending non-duplicate hashtags (case-insensitive)."""
        text = (caption_text or "").strip()
        existing_tags = set(m.lower() for m in re.findall(r"#\w+", text))

        to_append: List[str] = []
        for tag in hashtags:
            raw = str(tag).strip()
            if not raw:
                continue
            norm = "#" + raw.lstrip("#")
            if norm.lower() not in existing_tags:
                pattern = re.compile(rf"(?i){re.escape(norm)}(?![a-zA-Z0-9_])")
                if not pattern.search(text):
                    to_append.append(norm)
                    existing_tags.add(norm.lower())

        if to_append:
            if text:
                text = f"{text} {' '.join(to_append)}"
            else:
                text = " ".join(to_append)

        return text
