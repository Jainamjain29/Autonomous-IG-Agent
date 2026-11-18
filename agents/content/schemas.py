"""JSON schemas for Content Agent LLM structured outputs."""
from __future__ import annotations
from typing import Any, Dict

PLAN_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": [
        "title",
        "topic",
        "hook",
        "script",
        "scenes",
        "caption",
        "hashtags",
        "platform_text",
    ],
    "properties": {
        "title": {"type": "string"},
        "topic": {"type": "string"},
        "hook": {"type": "string"},
        "script": {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "narration", "visual_prompt", "duration_hint_s"],
                "properties": {
                    "id": {"type": "integer"},
                    "narration": {"type": "string"},
                    "visual_prompt": {"type": "string"},
                    "duration_hint_s": {"type": "number"},
                },
            },
        },
        "caption": {"type": "string"},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "platform_text": {
            "type": "object",
            "required": ["instagram", "youtube"],
            "properties": {
                "instagram": {
                    "type": "object",
                    "required": ["caption"],
                    "properties": {
                        "caption": {"type": "string"},
                    },
                },
                "youtube": {
                    "type": "object",
                    "required": ["title", "description"],
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
        },
    },
}
