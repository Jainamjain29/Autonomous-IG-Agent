"""Machine-wide publish lock to prevent concurrent publishes on shared accounts."""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.runfolder import append_event

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_LOCK_NAME = "ig_agent_posting.lock"


class PublishLockError(Exception):
    """Raised when publish cannot proceed due to lock contention or account concurrency."""
    pass


def get_default_lock_path() -> Path:
    """Return default lock path in system temp directory."""
    return Path(tempfile.gettempdir()) / DEFAULT_LOCK_NAME


def format_ist_time(dt: Optional[datetime] = None) -> str:
    """Format datetime into 12-hour IST string."""
    if dt is None:
        dt = datetime.now(IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        dt = dt.astimezone(IST)
    return dt.strftime("%I:%M:%S %p IST, %d %b %Y")


def is_pid_alive(pid: int) -> bool:
    """Check if process with PID is currently alive."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            err = kernel32.GetLastError()
            ERROR_ACCESS_DENIED = 5
            return err == ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        else:
            return True


def map_ngrok_error(error_msg_or_exc: Any) -> str:
    """Map ngrok session/agent limit error to a clear message."""
    msg = str(error_msg_or_exc)
    lower = msg.lower()
    if (
        "agent-limit" in lower
        or "err_ngrok_108" in lower
        or "session limit" in lower
        or "simultaneous" in lower
        or "concurrency limit" in lower
        or "endpoint limit" in lower
    ):
        return "shared ngrok account is in use — someone else is publishing"
    return msg


class PublishLock:
    """Acquires a machine-wide lock file in tempdir holding run_id, PID, host, and IST time."""

    def __init__(
        self,
        run_id: str,
        run_dir: Optional[str] = None,
        lock_path: Optional[Path] = None,
    ):
        self.run_id = run_id
        self.run_dir = run_dir
        self.lock_path = lock_path or get_default_lock_path()
        self.acquired = False

    def acquire(self) -> None:
        """Acquire machine lock, taking over stale locks or failing fast on active locks."""
        if self.lock_path.exists():
            try:
                content = self.lock_path.read_text(encoding="utf-8")
                lock_info: Dict[str, Any] = json.loads(content)
            except Exception:
                lock_info = {}

            held_pid = lock_info.get("pid")
            held_run_id = lock_info.get("run_id", "unknown")
            held_host = lock_info.get("host", "unknown")
            held_time = lock_info.get("start_time", "unknown")

            if held_pid and is_pid_alive(int(held_pid)):
                msg = f"another publish is running (run {held_run_id})"
                if self.run_dir:
                    append_event(
                        self.run_dir,
                        "posting_lock_failed",
                        {
                            "error": msg,
                            "held_run_id": held_run_id,
                            "held_pid": held_pid,
                            "held_host": held_host,
                            "held_time": held_time,
                        },
                    )
                raise PublishLockError(msg)
            else:
                # Stale lock: dead PID
                if self.run_dir:
                    append_event(
                        self.run_dir,
                        "posting_lock_stale_takeover",
                        {
                            "stale_run_id": held_run_id,
                            "stale_pid": held_pid,
                            "stale_host": held_host,
                            "stale_time": held_time,
                        },
                    )

        lock_data = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "host": platform.node(),
            "start_time": format_ist_time(),
        }

        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self.lock_path.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
            self.acquired = True
            if self.run_dir:
                append_event(
                    self.run_dir,
                    "posting_lock_acquired",
                    {
                        "lock_file": str(self.lock_path),
                        "run_id": self.run_id,
                        "pid": lock_data["pid"],
                        "start_time": lock_data["start_time"],
                    },
                )
        except Exception as e:
            raise PublishLockError(f"Failed to write lock file {self.lock_path}: {e}") from e

    def release(self) -> None:
        """Release the lock if held by this instance/process."""
        if not self.acquired:
            return
        try:
            if self.lock_path.exists():
                try:
                    data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                    if data.get("pid") == os.getpid() or data.get("run_id") == self.run_id:
                        self.lock_path.unlink(missing_ok=True)
                except Exception:
                    self.lock_path.unlink(missing_ok=True)
            if self.run_dir:
                append_event(
                    self.run_dir,
                    "posting_lock_released",
                    {"lock_file": str(self.lock_path), "run_id": self.run_id},
                )
        finally:
            self.acquired = False

    def __enter__(self) -> PublishLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
