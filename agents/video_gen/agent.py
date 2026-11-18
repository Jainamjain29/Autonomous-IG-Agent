"""Video Gen Agent: generates video clips for each planned scene using swappable engines."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask
from core.runfolder import append_event
from agents.video_gen.defaults import DEFAULT_PROVIDER, MAX_SCENE_RETRIES
from agents.video_gen.probe import probe_video
from agents.video_gen.providers import CharacterModel, get_provider


def _safe_append_event(run_dir: Union[str, Path], event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
    try:
        append_event(run_dir, event_type, data)
    except Exception:
        pass


class VideoGenAgent(BaseAgent):
    """Generates visual clips for each planned scene through a pluggable engine layer."""

    def __init__(self):
        super().__init__(name="video_gen")

    def run(self, task: AgentTask) -> AgentResult:
        """Execute video generation workflow for all planned scenes."""
        try:
            agent_dir = self.get_agent_dir(task)
            clips_dir = agent_dir / "clips"
            clips_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            err_msg = f"Failed to initialize video_gen agent directory: {e}"
            return AgentResult(status="failed", error=err_msg, summary=err_msg)

        # 1. Load scenes from content/plan.json
        try:
            plan = self.read_input_json(task, "content", "plan.json")
        except Exception as e:
            err_msg = f"Failed to load content/plan.json: {e}"
            _safe_append_event(task.run_dir, "video_gen_error", {"error": err_msg})
            return AgentResult(status="failed", error=err_msg, summary=err_msg)

        scenes: List[Dict[str, Any]] = plan.get("scenes", [])
        if not scenes:
            err_msg = "No scenes found in content/plan.json"
            _safe_append_event(task.run_dir, "video_gen_error", {"error": err_msg})
            return AgentResult(status="failed", error=err_msg, summary=err_msg)

        # 2. Read spec.yaml for provider and character configuration
        spec_yaml: Dict[str, Any] = {}
        spec_path = Path(task.run_dir) / "spec.yaml"
        if spec_path.is_file():
            try:
                with open(spec_path, "r", encoding="utf-8") as f:
                    spec_yaml = yaml.safe_load(f) or {}
            except Exception:
                pass

        # Determine engine name
        video_cfg = task.spec.extra.get("video") if isinstance(task.spec.extra, dict) else None
        if not video_cfg and isinstance(spec_yaml.get("video"), dict):
            video_cfg = spec_yaml["video"]

        provider_name = DEFAULT_PROVIDER
        if isinstance(video_cfg, dict) and video_cfg.get("provider"):
            provider_name = str(video_cfg["provider"])

        # Resolve engine
        try:
            engine = get_provider(provider_name)
        except Exception as e:
            err_msg = f"Failed to load video engine: {e}"
            append_event(task.run_dir, "video_gen_error", {"error": err_msg})
            return AgentResult(status="failed", error=err_msg, summary=err_msg)

        # Build CharacterModel
        char_desc = (
            task.spec.character_text
            or spec_yaml.get("character_text")
            or (spec_yaml.get("character", {}).get("description") if isinstance(spec_yaml.get("character"), dict) else "")
            or ""
        )
        char_img = (
            task.spec.character_image
            or spec_yaml.get("character_image")
            or (spec_yaml.get("character", {}).get("image") if isinstance(spec_yaml.get("character"), dict) else None)
        )
        character = CharacterModel.build(description=char_desc, reference_image=char_img)

        # 3. Prepare engine for run
        run_ctx = {
            "run_id": task.run_id,
            "run_dir": task.run_dir,
            "agent_dir": agent_dir,
        }
        try:
            engine.prepare(run_ctx=run_ctx, character=character)
            append_event(task.run_dir, "video_gen_prepared", {"provider": engine.name})
        except Exception as e:
            err_msg = f"Engine prepare failed ({engine.name}): {e}"
            append_event(task.run_dir, "video_gen_error", {"error": err_msg})
            return AgentResult(status="failed", error=err_msg, summary=err_msg)

        # 4. Generate clips for each scene (resumable)
        clips_records: List[Dict[str, Any]] = []

        for idx, scene in enumerate(scenes):
            scene_id = int(scene.get("id", idx + 1))
            out_clip_path = clips_dir / f"scene_{scene_id}.mp4"
            legacy_clip_path = agent_dir / f"scene_{scene_id}.mp4"

            # Check if valid clip already exists (resumable check)
            existing_valid_path: Optional[Path] = None
            for candidate in [out_clip_path, legacy_clip_path]:
                if candidate.is_file():
                    try:
                        dur, w, h = probe_video(candidate)
                        if dur > 0 and w > 0 and h > 0:
                            existing_valid_path = candidate
                            break
                    except Exception:
                        pass

            if existing_valid_path:
                rel_path = str(existing_valid_path.relative_to(task.run_dir)).replace("\\", "/")
                dur, w, h = probe_video(existing_valid_path)
                clips_records.append({
                    "scene_id": scene_id,
                    "path": rel_path,
                    "duration_s": dur,
                    "width": w,
                    "height": h,
                    "provider": engine.name,
                })
                append_event(task.run_dir, "video_gen_clip_skipped", {
                    "scene_id": scene_id,
                    "reason": "existing_valid_clip",
                    "path": rel_path,
                })
                continue

            # Generation loop with up to MAX_SCENE_RETRIES retries
            success = False
            last_err: Optional[Exception] = None

            for attempt in range(1 + MAX_SCENE_RETRIES):
                try:
                    engine.generate(
                        scene=scene,
                        character=character,
                        out_path=out_clip_path,
                        target={},
                    )

                    # Strictly probe with ffprobe/ffmpeg; never trust engine metadata
                    real_dur, real_w, real_h = probe_video(out_clip_path)

                    rel_path = str(out_clip_path.relative_to(task.run_dir)).replace("\\", "/")
                    clips_records.append({
                        "scene_id": scene_id,
                        "path": rel_path,
                        "duration_s": real_dur,
                        "width": real_w,
                        "height": real_h,
                        "provider": engine.name,
                    })

                    _safe_append_event(task.run_dir, "video_gen_clip_created", {
                        "scene_id": scene_id,
                        "path": rel_path,
                        "duration_s": real_dur,
                        "width": real_w,
                        "height": real_h,
                        "attempt": attempt + 1,
                    })
                    success = True
                    break
                except Exception as ex:
                    last_err = ex
                    _safe_append_event(task.run_dir, "video_gen_clip_attempt_failed", {
                        "scene_id": scene_id,
                        "attempt": attempt + 1,
                        "error": str(ex),
                    })

            if not success:
                err_msg = (
                    f"Failed to generate clip for scene {scene_id} after "
                    f"{1 + MAX_SCENE_RETRIES} attempts: {last_err}"
                )
                _safe_append_event(task.run_dir, "video_gen_failed", {
                    "scene_id": scene_id,
                    "error": err_msg,
                })
                return AgentResult(
                    status="failed",
                    error=err_msg,
                    summary=f"VideoGenAgent stopped at scene {scene_id}: {last_err}",
                )

        # 5. Write output clips.json
        clips_data = {"clips": clips_records}
        output_file = self.write_output_json(task, "clips.json", clips_data)

        _safe_append_event(task.run_dir, "video_gen_completed", {
            "clips_count": len(clips_records),
            "output_path": str(output_file),
        })

        return AgentResult(
            status="ok",
            outputs={"clips": str(output_file)},
            summary=f"Successfully generated {len(clips_records)} clips using engine '{engine.name}'",
        )
