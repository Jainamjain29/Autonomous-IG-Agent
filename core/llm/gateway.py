"""Unified AI model gateway for multi-agent execution."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from core.llm.db import log_llm_call
from core.llm.pricing import calculate_cost_usd
from core.llm.providers import (
    LLMError,
    RetryableLLMError,
    execute_provider_call,
)


@dataclass
class LLMResponse:
    """Standardized response from the LLM model gateway."""
    text: str
    data: Any
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    backup_used: bool = False


_AGENTS_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def load_agent_config(config_path: Union[str, Path] = "config/agents.yaml") -> Dict[str, Any]:
    """Load agents.yaml configuration."""
    global _AGENTS_CONFIG_CACHE
    if _AGENTS_CONFIG_CACHE is not None:
        return _AGENTS_CONFIG_CACHE

    path = Path(config_path)
    if not path.exists():
        _AGENTS_CONFIG_CACHE = {
            "defaults": {
                "provider": "gemini",
                "model": "gemini-2.5-flash",
                "temperature": 0.7,
                "timeout_s": 30,
                "backup": {
                    "provider": "gemini",
                    "model": "gemini-2.5-flash-lite",
                    "temperature": 0.7,
                    "timeout_s": 30,
                },
            },
            "agents": {},
        }
        return _AGENTS_CONFIG_CACHE

    with open(path, "r", encoding="utf-8") as f:
        _AGENTS_CONFIG_CACHE = yaml.safe_load(f) or {}
    return _AGENTS_CONFIG_CACHE


def _extract_and_parse_json(raw_text: str) -> Any:
    """Parse JSON string, stripping markdown code fences or conversational text if present."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Search for curly braces or brackets
        match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise


def _validate_json_schema(parsed_data: Any, schema: Dict[str, Any]) -> None:
    """Validate parsed JSON against expected required schema fields."""
    if not isinstance(parsed_data, dict):
        if schema.get("type") == "object" or "required" in schema:
            raise ValueError(f"Expected JSON object, got {type(parsed_data).__name__}")
        return

    required = schema.get("required", [])
    missing = [req for req in required if req not in parsed_data]
    if missing:
        raise ValueError(f"Missing required schema fields: {missing}")


def complete(
    agent: str,
    messages: List[Dict[str, str]],
    *,
    json_schema: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
    **overrides: Any,
) -> LLMResponse:
    """Execute LLM call for a specific agent with retries, backup switching, and usage logging.
    
    Args:
        agent: Name of the agent making the call (e.g. 'content', 'qc', 'video_gen')
        messages: List of chat messages [{'role': 'user', 'content': '...'}]
        json_schema: Optional JSON schema specifying structure and required keys
        run_id: Optional execution run identifier for usage tracking
        db_path: Optional override for core database path
        **overrides: Override configuration (provider, model, temperature, timeout_s, backup)
    """
    config = load_agent_config()
    defaults = config.get("defaults", {})
    agent_cfg = config.get("agents", {}).get(agent, {})

    # Resolve primary configuration
    provider = overrides.get("provider", agent_cfg.get("provider", defaults.get("provider", "gemini")))
    model = overrides.get("model", agent_cfg.get("model", defaults.get("model", "gemini-2.5-flash")))
    temperature = overrides.get("temperature", agent_cfg.get("temperature", defaults.get("temperature", 0.7)))
    timeout_s = overrides.get("timeout_s", agent_cfg.get("timeout_s", defaults.get("timeout_s", 30)))

    # Resolve backup configuration
    backup_cfg = overrides.get("backup", agent_cfg.get("backup", defaults.get("backup", {})))
    backup_provider = backup_cfg.get("provider", provider)
    backup_model = backup_cfg.get("model", "gemini-2.5-flash-lite")
    backup_temperature = backup_cfg.get("temperature", temperature)
    backup_timeout_s = backup_cfg.get("timeout_s", timeout_s)

    expect_json = json_schema is not None
    extra_kwargs = {k: v for k, v in overrides.items() if k not in (
        "provider", "model", "temperature", "timeout_s", "backup"
    )}

    start_time = time.perf_counter()
    backup_used = False
    current_provider = provider
    current_model = model
    current_temp = temperature
    current_timeout = timeout_s

    raw_text = ""
    tokens_in = 0
    tokens_out = 0
    parsed_data = None
    last_error: Optional[Exception] = None

    # Step 1: Attempt execution with primary, then fallback to backup on failure
    max_retries_per_tier = 2
    executed = False

    for tier in ("primary", "backup"):
        if tier == "backup":
            if not backup_model or (backup_model == model and backup_provider == provider):
                break
            backup_used = True
            current_provider = backup_provider
            current_model = backup_model
            current_temp = backup_temperature
            current_timeout = backup_timeout_s

        for attempt in range(max_retries_per_tier + 1):
            try:
                raw_text, tokens_in, tokens_out = execute_provider_call(
                    provider=current_provider,
                    model=current_model,
                    agent=agent,
                    messages=messages,
                    temperature=current_temp,
                    timeout_s=current_timeout,
                    expect_json=expect_json,
                    **extra_kwargs,
                )
                executed = True
                break
            except RetryableLLMError as e:
                last_error = e
                if attempt < max_retries_per_tier:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                break
            except Exception as e:
                last_error = e
                break

        if executed:
            break

    if not executed:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        err_msg = str(last_error) if last_error else "LLM execution failed"
        log_llm_call(
            agent=agent,
            provider=current_provider,
            model=current_model,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=latency_ms,
            success=False,
            backup_used=backup_used,
            run_id=run_id,
            error=err_msg,
            db_path=db_path,
        )
        raise LLMError(f"LLM call failed for agent '{agent}' on both primary and backup: {err_msg}") from last_error

    # Step 2: Validate JSON schema and handle one repair retry if needed
    if expect_json and json_schema is not None:
        try:
            parsed_data = _extract_and_parse_json(raw_text)
            _validate_json_schema(parsed_data, json_schema)
        except Exception as parse_err:
            # One repair retry
            repair_prompt = (
                f"The previous output was invalid JSON or missing required fields: {parse_err}\n"
                f"Required schema: {json.dumps(json_schema)}\n"
                f"Please fix and output ONLY valid JSON without explanation:\n{raw_text}"
            )
            repair_messages = list(messages) + [
                {"role": "assistant", "content": raw_text},
                {"role": "user", "content": repair_prompt},
            ]
            try:
                repaired_text, rep_in, rep_out = execute_provider_call(
                    provider=current_provider,
                    model=current_model,
                    agent=agent,
                    messages=repair_messages,
                    temperature=0.1,
                    timeout_s=current_timeout,
                    expect_json=True,
                    **extra_kwargs,
                )
                tokens_in += rep_in
                tokens_out += rep_out
                parsed_data = _extract_and_parse_json(repaired_text)
                _validate_json_schema(parsed_data, json_schema)
                raw_text = repaired_text
            except Exception as final_err:
                latency_ms = int((time.perf_counter() - start_time) * 1000)
                cost_usd = calculate_cost_usd(current_model, tokens_in, tokens_out)
                log_llm_call(
                    agent=agent,
                    provider=current_provider,
                    model=current_model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    success=False,
                    backup_used=backup_used,
                    run_id=run_id,
                    error=f"JSON validation failed after repair: {final_err}",
                    db_path=db_path,
                )
                raise LLMError(f"JSON validation failed after repair retry: {final_err}") from final_err

    # Step 3: Record successful usage log
    latency_ms = int((time.perf_counter() - start_time) * 1000)
    cost_usd = calculate_cost_usd(current_model, tokens_in, tokens_out)

    log_llm_call(
        agent=agent,
        provider=current_provider,
        model=current_model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        success=True,
        backup_used=backup_used,
        run_id=run_id,
        error=None,
        db_path=db_path,
    )

    return LLMResponse(
        text=raw_text,
        data=parsed_data,
        provider=current_provider,
        model=current_model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        backup_used=backup_used,
    )
