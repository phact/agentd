"""Harness tool calls as PTC-style execution events in streams.

PTC streams each executed code block as a completed ``code_interpreter_call``
output item, which ``display_events`` turns into ``CodeExecution``. Harness
streams do the same for every tool the harness runs in the sandbox, so
consumers see tool activity identically whichever harness is running:

  * shell tools (Claude Code ``Bash``, Codex ``shell``): code = the command
  * anything else (``Read``, ``Edit``, MCP/skills, ``web_search``, ...):
    code = the tool's arguments as JSON

The tool name stands where PTC puts the fence type, and the result is the
output. These are already executed: clients never run them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agentd.harness.events import HarnessEvent

_SHELL_TOOLS = ("Bash", "shell", "bash")  # Claude Code, Codex, OpenCode / omp


@dataclass
class ToolCall:
    name: str
    code: str
    output: str
    status: str  # "completed" | "failed" | "incomplete"


def _as_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, list) and all(isinstance(b, dict) and "text" in b for b in data):
        return "\n".join(b["text"] for b in data)
    return json.dumps(data, ensure_ascii=False, default=str)


def _code(use: HarnessEvent) -> str:
    data = use.data
    if use.name in _SHELL_TOOLS and isinstance(data, dict) and isinstance(data.get("command"), str):
        return data["command"]
    return _as_text(data)


@dataclass
class ToolCalls:
    """Pairs ``tool_use`` events with their ``tool_result``."""

    pending: dict[str, HarnessEvent] = field(default_factory=dict)

    def started(self, use: HarnessEvent) -> None:
        self.pending[use.id or f"_anon{len(self.pending)}"] = use

    def finished(self, result: HarnessEvent) -> ToolCall | None:
        """The completed call, or None for results with no known use (e.g. Codex retry notices)."""
        use = self.pending.pop(result.id, None) if result.id else None
        if use is None:
            return None
        return ToolCall(use.name or "tool", _code(use), _as_text(result.data),
                        "failed" if result.is_error else "completed")

    def unfinished(self) -> list[ToolCall]:
        """Calls that never got a result (the turn ended or was cut off)."""
        calls = [ToolCall(u.name or "tool", _code(u), "", "incomplete") for u in self.pending.values()]
        self.pending.clear()
        return calls
