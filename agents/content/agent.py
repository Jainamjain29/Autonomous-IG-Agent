"""Content Agent implementation for multi-agent video creation."""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.agent import BaseAgent
from core.contracts import AgentResult, AgentTask, RunSpec
from core import llm
from agents.content.prompts import (
    build_character_prompt,
    build_generate_prompt,
    build_repair_prompt,
    build_script_prompt,
    build_script_retry_prompt,
    build_system_prompt,
    build_video_mode_prompt,
)
from agents.content.schemas import PLAN_JSON_SCHEMA
from agents.content.settings import ALLOWED_MODES, DEFAULT_BRAND_VOICE, DEFAULT_LANGUAGE
from agents.content.validator import (
    compute_scene_duration,
    ensure_character_in_visual_prompts,
    sanitize_and_finalize_plan,
    split_long_script_scenes,
    validate_plan,
)

logger = logging.getLogger(__name__)


class ContentAgent(BaseAgent):
    """Generates video concepts, scripts, scene prompts, captions, and platform metadata."""

    def __init__(
        self,
        brand_voice: str = DEFAULT_BRAND_VOICE,
        language: str = DEFAULT_LANGUAGE,
        provider: Optional[str] = None,
        **extra_kwargs: Any,
    ):
        super().__init__(name="content")
        self.brand_voice = brand_voice
        self.language = language
        self.provider = provider or extra_kwargs.get("provider")

    def _read_spec(self, task: AgentTask) -> RunSpec:
        """Read RunSpec strictly from the run's spec.yaml file if available."""
        spec_path = Path(task.run_dir) / "spec.yaml"
        if spec_path.exists():
            with open(spec_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            # Preserve extra keys like character if specified directly
            spec = RunSpec.from_dict(data)
            if "character" in data and not spec.character_text:
                spec.character_text = str(data["character"])
            if "description" in data:
                spec.extra["description"] = data["description"]
            return spec
        return task.spec

    def _resolve_character_desc(self, spec: RunSpec, raw_dict: Dict[str, Any]) -> Optional[str]:
        """Extract character description from standard and alternate spec fields."""
        return (
            spec.character_text
            or raw_dict.get("character")
            or raw_dict.get("character_text")
            or spec.extra.get("character")
            or spec.extra.get("character_text")
        )

    def run(self, task: AgentTask) -> AgentResult:
        """Execute the content agent task and generate plan.json atomically."""
        agent_dir = self.get_agent_dir(task)
        plan_output_path = agent_dir / "plan.json"

        # 1. Read spec strictly from run folder
        spec = self._read_spec(task)

        # Check raw yaml for any unmapped fields
        spec_path = Path(task.run_dir) / "spec.yaml"
        raw_yaml_dict: Dict[str, Any] = {}
        if spec_path.exists():
            try:
                with open(spec_path, "r", encoding="utf-8") as f:
                    raw_yaml_dict = yaml.safe_load(f) or {}
            except Exception:
                pass

        raw_mode = (spec.mode or "").strip().lower()
        if raw_mode not in ALLOWED_MODES:
            return AgentResult(
                status="failed",
                error=f"Unknown or unsupported mode '{spec.mode}'. Allowed modes are: {sorted(ALLOWED_MODES)}",
            )
        mode = raw_mode

        character_desc = self._resolve_character_desc(spec, raw_yaml_dict)

        # 2. Build system and user prompt for the given mode
        system_prompt = build_system_prompt(brand_voice=self.brand_voice, language=self.language)

        if mode == "video":
            # Existing video mode: caption only. NEVER open or analyze the video file.
            topic_or_desc = (
                spec.topic
                or raw_yaml_dict.get("description")
                or spec.extra.get("description")
                or "Existing Video"
            )
            user_prompt = build_video_mode_prompt(topic=topic_or_desc)
            expected_script = None

        elif mode == "script":
            user_script = spec.script or raw_yaml_dict.get("script") or ""
            if not user_script.strip():
                return AgentResult(
                    status="failed",
                    error="Mode 'script' requires a non-empty script in spec.yaml",
                )
            user_prompt = build_script_prompt(script=user_script, topic=spec.topic)
            expected_script = user_script

        elif mode == "character":
            if not character_desc:
                return AgentResult(
                    status="failed",
                    error="Mode 'character' requires 'character' or 'character_text' in spec.yaml",
                )
            topic = spec.topic or "Tech Innovation"
            user_prompt = build_character_prompt(topic=topic, character_desc=character_desc)
            expected_script = None

        else:
            # Default: generate mode
            topic = spec.topic or "Latest Tech Trends"
            user_prompt = build_generate_prompt(topic=topic)
            expected_script = None

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # 3. Call core LLM
        # Model choice comes ONLY from config/agents.yaml via core.llm
        llm_kwargs: Dict[str, Any] = {}
        provider = self.provider or os.getenv("LLM_PROVIDER")
        if provider:
            llm_kwargs["provider"] = provider

        try:
            response = llm.complete(
                agent="content",
                messages=messages,
                json_schema=PLAN_JSON_SCHEMA,
                run_id=task.run_id,
                **llm_kwargs,
            )
        except Exception as e:
            return AgentResult(
                status="failed",
                error=f"Initial LLM call failed: {e}",
            )

        plan_data = response.data
        if not isinstance(plan_data, dict):
            try:
                plan_data = json.loads(response.text)
            except Exception:
                plan_data = None

        # In script mode: code splits any scene whose narration needs > 8s at sentence/clause boundary
        if mode == "script" and isinstance(plan_data, dict) and isinstance(plan_data.get("scenes"), list):
            plan_data["scenes"] = split_long_script_scenes(plan_data["scenes"])
            if plan_data["scenes"]:
                plan_data["hook"] = plan_data["scenes"][0].get("narration", plan_data.get("hook", ""))

        # Code computes duration_hint_s for all scenes from word count (ignoring model's numbers)
        if isinstance(plan_data, dict) and isinstance(plan_data.get("scenes"), list):
            for s in plan_data["scenes"]:
                if isinstance(s, dict):
                    s["duration_hint_s"] = compute_scene_duration(s.get("narration", ""))

        # Guarantee character description in every visual_prompt before validation
        if mode == "character" and character_desc and isinstance(plan_data, dict):
            ensure_character_in_visual_prompts(plan_data.get("scenes", []), character_desc)

        # 4. Validate output
        errors = validate_plan(
            plan=plan_data,
            mode=mode,
            expected_script=expected_script,
            character_desc=character_desc,
        )

        # 5. Handle repair / retry if validation failed
        if errors:
            logger.warning(f"Initial ContentAgent output invalid: {errors}. Initiating 1 repair attempt.")
            is_script_mismatch = any("Script mismatch" in err for err in errors)

            if is_script_mismatch and expected_script:
                mismatch_err = next(err for err in errors if "Script mismatch" in err)
                repair_prompt = build_script_retry_prompt(expected_script, mismatch_err)
            else:
                repair_prompt = build_repair_prompt(errors, response.text)

            repair_messages: List[Dict[str, str]] = list(messages)
            repair_messages.append({"role": "assistant", "content": response.text})
            repair_messages.append({"role": "user", "content": repair_prompt})

            try:
                repair_resp = llm.complete(
                    agent="content",
                    messages=repair_messages,
                    json_schema=PLAN_JSON_SCHEMA,
                    run_id=task.run_id,
                    **llm_kwargs,
                )
                repaired_data = repair_resp.data
                if not isinstance(repaired_data, dict):
                    try:
                        repaired_data = json.loads(repair_resp.text)
                    except Exception:
                        repaired_data = None

                # In script mode, code splits long scenes on repaired data as well
                if mode == "script" and isinstance(repaired_data, dict) and isinstance(repaired_data.get("scenes"), list):
                    repaired_data["scenes"] = split_long_script_scenes(repaired_data["scenes"])
                    if repaired_data["scenes"]:
                        repaired_data["hook"] = repaired_data["scenes"][0].get("narration", repaired_data.get("hook", ""))

                # Code computes duration_hint_s on repaired data as well
                if isinstance(repaired_data, dict) and isinstance(repaired_data.get("scenes"), list):
                    for s in repaired_data["scenes"]:
                        if isinstance(s, dict):
                            s["duration_hint_s"] = compute_scene_duration(s.get("narration", ""))

                # Guarantee character description on repaired data as well
                if mode == "character" and character_desc and isinstance(repaired_data, dict):
                    ensure_character_in_visual_prompts(repaired_data.get("scenes", []), character_desc)

                repair_errors = validate_plan(
                    plan=repaired_data,
                    mode=mode,
                    expected_script=expected_script,
                    character_desc=character_desc,
                )
                if repair_errors:
                    return AgentResult(
                        status="failed",
                        error=f"Validation failed after repair attempt: {'; '.join(repair_errors)}",
                    )
                plan_data = repaired_data
            except Exception as e:
                return AgentResult(
                    status="failed",
                    error=f"Repair LLM call failed: {e}",
                )

        # 6. Sanitize, cap, and assemble final platform_text
        final_plan = sanitize_and_finalize_plan(
            plan=plan_data,
            mode=mode,
            character_desc=character_desc,
        )

        # 7. Atomically write plan.json (temp file + replace)
        tmp_filename = f"plan.json.tmp_{uuid.uuid4().hex[:8]}"
        tmp_path = agent_dir / tmp_filename
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(final_plan, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(plan_output_path)
        except Exception as e:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return AgentResult(
                status="failed",
                error=f"Failed to atomically write plan.json: {e}",
            )

        summary_msg = f"Generated {len(final_plan.get('scenes', []))} scenes in mode '{mode}'"
        return AgentResult(
            status="ok",
            outputs={"plan": str(plan_output_path)},
            summary=summary_msg,
        )
