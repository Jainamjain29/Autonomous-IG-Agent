"""Usage logging for LLM calls to data/core.db.

CRITICAL: Never store prompts or responses. Only store usage metadata, token
counts, cost, latency, and status flags.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def get_core_db_path(db_path: Optional[str | Path] = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    return Path("data/core.db")


def init_core_db(db_path: Optional[str | Path] = None) -> Path:
    """Initialize core database and llm_calls table."""
    path = get_core_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=10.0)
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    run_id TEXT,
                    agent TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    tokens_in INTEGER NOT NULL,
                    tokens_out INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    backup_used INTEGER NOT NULL,
                    error TEXT
                )
            """)
    finally:
        conn.close()
    return path


def log_llm_call(
    agent: str,
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    success: bool,
    backup_used: bool = False,
    run_id: Optional[str] = None,
    error: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> None:
    """Log an LLM call's usage metadata to SQLite.
    
    Prompt and response text are intentionally excluded to protect privacy
    and keep storage light.
    """
    path = init_core_db(db_path)
    now_utc = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(path, timeout=10.0)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO llm_calls (
                    ts, run_id, agent, provider, model, tokens_in, tokens_out,
                    cost_usd, latency_ms, success, backup_used, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_utc,
                    run_id,
                    agent,
                    provider,
                    model,
                    int(tokens_in),
                    int(tokens_out),
                    float(cost_usd),
                    int(latency_ms),
                    1 if success else 0,
                    1 if backup_used else 0,
                    error,
                ),
            )
    finally:
        conn.close()
