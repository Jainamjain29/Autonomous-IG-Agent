"""Advisory AI Critic for the QC Agent.

Produces advisory improvement suggestions for the next run. Never alters passed or any check.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agents.qc import settings

logger = logging.getLogger("qc.critic")

CRITIC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["suggestions"],
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["area", "suggestion"],
                "properties": {
                    "area": {
                        "type": "string",
                        "description": "Area of critique (e.g. hook, pacing, caption, hashtags, visual)",
                    },
                    "suggestion": {
                        "type": "string",
                        "description": "Concrete recommendation for improving the next generation run",
                    },
                },
            },
        },
    },
}


def run_advisory_critic(
    plan: Dict[str, Any],
    srt_text: str,
    checks_results: List[Dict[str, Any]],
    *,
    enabled_override: Optional[bool] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute optional advisory critique via core.llm.complete.
    
    Returns ai_review structure: {enabled: bool, model: str, suggestions: list, error: str | None}.
    """
    is_enabled = settings.ENABLE_AI_CRITIC if enabled_override is None else enabled_override
    model_name = settings.CRITIC_MODEL

    if not is_enabled:
        return {
            "enabled": False,
            "model": "",
            "suggestions": [],
            "error": None,
        }

    # Prepare context for the critic
    failing_checks = [c for c in checks_results if not c.get("passed", False)]
    failing_summary = [f"[{c['severity'].upper()}] {c['name']}: {c['detail']}" for c in failing_checks]

    context = {
        "hook": plan.get("hook", ""),
        "script": plan.get("script", ""),
        "caption": plan.get("caption", ""),
        "hashtags": plan.get("hashtags", []),
        "platform_text": plan.get("platform_text", {}),
        "srt_text": srt_text,
        "failing_checks": failing_summary,
        "total_checks": len(checks_results),
    }

    user_prompt = (
        "You are an expert social video critic reviewing an automated Reel creation run. "
        "Review the planned content, transcript, and hard check results. "
        "Provide constructive, concise suggestions to improve the NEXT generation run.\n\n"
        f"Context:\n{context}\n\n"
        "Return structured JSON matching the provided schema with an array of suggestions (area, suggestion)."
    )

    messages = [
        {
            "role": "system",
            "content": "You are a professional social video critic. Provide specific advisory suggestions for video creation pipelines.",
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ]

    try:
        from core.llm import complete

        resp = complete(
            agent="qc",
            messages=messages,
            json_schema=CRITIC_SCHEMA,
            run_id=run_id,
            model=model_name,
        )

        data = resp.data if isinstance(resp.data, dict) else {}
        raw_suggestions = data.get("suggestions", [])
        clean_suggestions = []
        for s in raw_suggestions:
            if isinstance(s, dict) and "area" in s and "suggestion" in s:
                clean_suggestions.append({
                    "area": str(s["area"]),
                    "suggestion": str(s["suggestion"]),
                })

        return {
            "enabled": True,
            "model": resp.model or model_name,
            "suggestions": clean_suggestions,
            "error": None,
        }
    except Exception as e:
        logger.warning(f"AI critic call failed or timed out: {e}")
        return {
            "enabled": True,
            "model": model_name,
            "suggestions": [],
            "error": str(e),
        }
