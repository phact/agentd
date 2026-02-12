"""JSONL conversation logging for agentd.

Logs all conversation events (messages, tool calls, tool results) to JSONL files.
One file per handler invocation (each .create() call = one file).

Env vars:
    AGENTD_LOG_DISABLE: Set to "1" or "true" to disable logging (default: enabled)
    AGENTD_LOG_DIR: Directory for log files (default: ./logs)
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _is_disabled() -> bool:
    val = os.environ.get("AGENTD_LOG_DISABLE", "").lower()
    return val in ("1", "true")


def _get_log_dir() -> Path:
    return Path(os.environ.get("AGENTD_LOG_DIR", "./logs"))


class ConversationLog:
    """One instance per handler invocation. Writes JSONL events."""

    def __init__(self, patch_type: str, model: str, log_dir: Path):
        self._id = uuid4().hex[:12]
        log_dir.mkdir(parents=True, exist_ok=True)
        self._file = open(log_dir / f"{self._id}.jsonl", "a")
        self._lock = threading.Lock()
        self._write({
            "event": "session_start",
            "patch_type": patch_type,
            "model": model,
        })

    def _write(self, event: dict):
        """Thread-safe JSONL write + flush."""
        event["ts"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                self._file.write(json.dumps(event, default=str) + "\n")
                self._file.flush()
            except Exception:
                pass

    def message(self, role: str, content):
        """Log a system/user/assistant message."""
        self._write({"event": "message", "role": role, "content": content})

    def tool_call(self, tool_type: str, name: str, input_data):
        """Log a tool invocation."""
        self._write({
            "event": "tool_call",
            "tool_type": tool_type,
            "name": name,
            "input": input_data,
        })

    def tool_result(self, tool_type: str, name: str, output, duration_ms: int | None = None):
        """Log a tool result."""
        ev = {
            "event": "tool_result",
            "tool_type": tool_type,
            "name": name,
            "output": output,
        }
        if duration_ms is not None:
            ev["duration_ms"] = duration_ms
        self._write(ev)

    def end(self, turns: int):
        """Write session_end event and close file."""
        self._write({"event": "session_end", "turns": turns})
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass


def create_log(patch_type: str, model: str) -> ConversationLog | None:
    """Create a new conversation log. Returns None if logging is disabled."""
    if _is_disabled():
        return None
    try:
        return ConversationLog(patch_type, model, _get_log_dir())
    except Exception:
        return None
