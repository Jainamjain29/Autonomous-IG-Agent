"""View state manager for dashboard dismissals and last-seen timestamps.

Strictly separated from the main SQLite DB to guarantee the view remains read-only
with respect to data/insight.db. Uses atomic file replacement (os.replace).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from .timeutil import UTC

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VIEW_STATE_PATH = os.path.join(REPO_ROOT, "data", "view_state.json")


def load_view_state(path: str = DEFAULT_VIEW_STATE_PATH) -> dict[str, Any]:
    """Load view state from JSON file or return defaults."""
    if not os.path.exists(path):
        return {
            "dismissed_dedupe_keys": [],
            "last_seen_at": None,
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"dismissed_dedupe_keys": [], "last_seen_at": None}
            return {
                "dismissed_dedupe_keys": list(data.get("dismissed_dedupe_keys", [])),
                "last_seen_at": data.get("last_seen_at"),
            }
    except Exception:
        return {"dismissed_dedupe_keys": [], "last_seen_at": None}


def save_view_state(state: dict[str, Any], path: str = DEFAULT_VIEW_STATE_PATH) -> None:
    """Save view state atomically using a temp file and os.replace."""
    dir_name = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_name, exist_ok=True)

    # Atomic write via temp file in same directory
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
        json.dump(state, tf, indent=2, sort_keys=True)
        temp_name = tf.name

    os.replace(temp_name, path)


def dismiss_alert(dedupe_key: str, path: str = DEFAULT_VIEW_STATE_PATH) -> None:
    """Record an alert dedupe_key as dismissed."""
    state = load_view_state(path)
    dismissed = set(state.get("dismissed_dedupe_keys", []))
    dismissed.add(dedupe_key)
    state["dismissed_dedupe_keys"] = sorted(dismissed)
    save_view_state(state, path)


def mark_alerts_seen(now: datetime | None = None, path: str = DEFAULT_VIEW_STATE_PATH) -> None:
    """Update last_seen_at timestamp to now."""
    state = load_view_state(path)
    cur = now or datetime.now(timezone.utc)
    state["last_seen_at"] = cur.isoformat()
    save_view_state(state, path)


def is_alert_dismissed(dedupe_key: str, state: dict[str, Any] | None = None, path: str = DEFAULT_VIEW_STATE_PATH) -> bool:
    """Check if an alert dedupe_key has been dismissed."""
    if state is None:
        state = load_view_state(path)
    return dedupe_key in state.get("dismissed_dedupe_keys", [])
