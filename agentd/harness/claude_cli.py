"""Drive the ``claude`` CLI directly: ``claude -p --output-format stream-json``.

Shared by the Claude Code harness and the subscription model transport for
PTC. The prompt goes in on stdin; the CLI prints one JSON object per line:

    {"type": "system", "subtype": "init", "session_id": ...}
    {"type": "assistant", "message": {"content": [{"type": "text"|"tool_use", ...}]}}
    {"type": "user", "message": {"content": [{"type": "tool_result", ...}]}}
    {"type": "result", "result": "...", "session_id": ..., "is_error": ...}

which :func:`parse_line` maps to neutral :class:`HarnessEvent` objects.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncIterator

from agentd.harness.events import HarnessEvent
from agentd.sandbox.session import ANTHROPIC_ENDPOINT


def sandbox_env() -> dict[str, str]:
    """Environment for ``claude`` inside the sandbox (no real credentials)."""
    return {
        "ANTHROPIC_BASE_URL": f"http://{ANTHROPIC_ENDPOINT[1]}:{ANTHROPIC_ENDPOINT[2]}",
        # Placeholder: the host proxy strips it and adds the real credential.
        "ANTHROPIC_AUTH_TOKEN": "agentd-sandbox",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        # Nothing from the sandbox goes to claude.ai: no artifacts.
        "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
    }


# Flag settings outrank the workspace's own .claude/settings.json.
SANDBOX_SETTINGS = {"enableArtifact": False, "autoUploadSessions": False, "disableRemoteControl": True}


def claude_argv(
    *,
    model: str | None = None,
    resume: str | None = None,
    system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    tools: list[str] | None = None,
    setting_sources: list[str] | None = None,
    permission_mode: str | None = None,
    strict_mcp_config: bool = False,
    extra_args: list[str] | None = None,
) -> list[str]:
    """``claude -p`` arguments; ``tools=[]`` disables every built-in tool."""
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if model:
        argv += ["--model", model]
    if resume:
        argv += ["--resume", resume]
    if system_prompt:
        argv += ["--system-prompt", system_prompt]
    if append_system_prompt:
        argv += ["--append-system-prompt", append_system_prompt]
    if tools is not None:
        argv += ["--tools", ",".join(tools)]
    if setting_sources is not None:
        argv += ["--setting-sources", ",".join(setting_sources)]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    if strict_mcp_config:
        argv.append("--strict-mcp-config")
    return argv + list(extra_args or [])


def parse_line(line: bytes | str) -> list[HarnessEvent]:
    """Neutral events for one stream-json line (non-JSON lines yield none)."""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    kind = obj.get("type")
    blocks = (obj.get("message") or {}).get("content") or []
    if kind == "assistant":
        events = []
        for block in blocks:
            if block.get("type") == "text":
                events.append(HarnessEvent("text", text=block.get("text", "")))
            elif block.get("type") == "tool_use":
                events.append(HarnessEvent("tool_use", name=block.get("name", ""), data=block.get("input"),
                                           id=block.get("id", "")))
        return events
    if kind == "user" and isinstance(blocks, list):
        return [
            HarnessEvent("tool_result", data=block.get("content"), is_error=bool(block.get("is_error")),
                         id=block.get("tool_use_id", ""))
            for block in blocks if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
    if kind == "result":
        return [HarnessEvent(
            "result", text=obj.get("result") or "", session_id=obj.get("session_id"),
            is_error=bool(obj.get("is_error")), data=obj.get("usage"),
        )]
    return []


async def run_in_sandbox(executor, argv: list[str], prompt: str, *, cwd: str) -> AsyncIterator[HarnessEvent]:
    """Run ``argv`` in the executor's (started) sandbox session."""
    tail: list[str] = []
    done = False
    lines = executor.stream_exec(argv, cwd=cwd, env=sandbox_env(), stdin=prompt.encode())
    async with aclosing(lines):  # closing it kills the CLI in the sandbox
        async for kind, value in lines:
            if kind == "line":
                events = parse_line(value)
                if not events and value.strip() and not value.lstrip().startswith(b"{"):
                    tail = (tail + [value.decode(errors="replace")])[-20:]
                for event in events:
                    done = done or event.kind == "result"
                    yield event
            elif not done:
                yield HarnessEvent("result", text=f"claude exited {value.get('exit')}: " + "\n".join(tail),
                                   is_error=True)


async def run_on_host(argv: list[str], prompt: str, *, cwd: str | Path | None) -> AsyncIterator[HarnessEvent]:
    """Run ``argv`` with the host's ``claude`` (used only with every tool disabled)."""
    # Drop CLAUDECODE so a nested run does not think it is inside a parent session.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd) if cwd else None, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )
    proc.stdin.write(prompt.encode())
    proc.stdin.close()
    stderr_task = asyncio.ensure_future(proc.stderr.read())
    done = False
    try:
        async for line in proc.stdout:
            for event in parse_line(line):
                done = done or event.kind == "result"
                yield event
        await proc.wait()
        if not done:
            stderr = (await stderr_task).decode(errors="replace")
            yield HarnessEvent("result", text=f"claude exited {proc.returncode}: {stderr[-2000:]}", is_error=True)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def assistant_text(events: list[HarnessEvent]) -> str:
    return "\n".join(e.text for e in events if e.kind == "text").strip()

