"""OpenCode as a harness, running entirely inside the sandbox session.

Each turn is one ``opencode run --format json`` (``--session <id>`` to
continue), prompt on stdin, inside the sandbox. agentd passes OpenCode's whole
configuration in ``OPENCODE_CONFIG_CONTENT``: one provider pointing at a
sandbox-side endpoint (see :mod:`agentd.harness.routes`), permissions allowed
(the sandbox is the boundary), and auto-update, model catalog fetches, LSP
downloads and sharing off (there's no network).

OpenCode keeps sessions in SQLite inside the sandbox. After each turn agentd
exports the session (``opencode export``) to the transcript store, and before
resuming one in a fresh sandbox it imports it back.

Options: ``model``, ``upstream``, ``config`` (merged into OpenCode's config),
``context_window`` / ``max_tokens``.
"""
from __future__ import annotations

import hashlib
import json
import shlex
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from agentd.harness import routes, transcripts
from agentd.harness.events import HarnessEvent
from agentd.model_proxy import ModelUpstream
from agentd.sandbox.executor import SandboxExecutor
from agentd.sandbox.session import SandboxSession

_NPM = {"anthropic": "@ai-sdk/anthropic", "openai-responses": "@ai-sdk/openai",
        "openai-chat": "@ai-sdk/openai-compatible"}
PROVIDER = "agentd"
OFFLINE_ENV = {
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_SHARE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",  # don't pull in ~/.claude prompts and skills
}


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@dataclass
class OpenCodeHarness:
    executor: SandboxExecutor
    extra_args: list[str] = field(default_factory=list)  # extra `opencode run` flags
    upstream: ModelUpstream | None = None
    model: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    context_window: int = 128_000
    max_tokens: int = 16_384
    name = "opencode"

    async def prepare(self, cwd) -> SandboxSession:
        return await self.executor.ensure_session(cwd)

    def opencode_config(self, route: routes.ModelRoute, instructions_path: str | None) -> dict[str, Any]:
        base_url = route.origin + "/v1" if route.api == "anthropic" else route.base_url
        config = {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "provider": {PROVIDER: {
                "npm": _NPM[route.api], "name": "agentd",
                "options": {"baseURL": base_url, "apiKey": routes.PLACEHOLDER_KEY},
                "models": {route.model: {"name": route.model,
                                         "limit": {"context": self.context_window, "output": self.max_tokens}}},
            }},
            "model": f"{PROVIDER}/{route.model}",
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow", "external_directory": "allow"},
        }
        if instructions_path:
            config["instructions"] = [instructions_path]
        return _merge(config, self.config)

    async def _exec(self, session: SandboxSession, script: str, stdin: bytes = b"") -> tuple[bytes, int]:
        return await self.executor.run(session.sandbox.exec(
            ["/bin/sh", "-c", script], cwd=str(session.workspace),
            env={**session.base_env(), **OFFLINE_ENV}, stdin=stdin))

    def _store(self, session: SandboxSession) -> str:
        return transcripts.sandbox_dir("opencode", session.workspace)

    async def _import(self, session: SandboxSession, session_id: str) -> None:
        """Bring a session exported by an earlier sandbox into this one's database."""
        known = session.__dict__.setdefault("_opencode_sessions", set())
        if session_id in known:
            return
        path = shlex.quote(f"{self._store(session)}/{session_id}.json")
        await self._exec(session, f"test -f {path} && opencode import {path} >/dev/null 2>&1; true")
        known.add(session_id)

    async def _export(self, session: SandboxSession, session_id: str) -> None:
        path = f"{self._store(session)}/{session_id}.json"
        tmp = f"{self._store(session)}/.{session_id}.json.tmp"
        out, code = await self._exec(session, f"mkdir -p {shlex.quote(self._store(session))} && "
                                              f"opencode export {shlex.quote(session_id)} > {shlex.quote(tmp)} && "
                                              f"mv {shlex.quote(tmp)} {shlex.quote(path)}")
        if code == 0:
            session.__dict__.setdefault("_opencode_sessions", set()).add(session_id)

    def argv(self, resume: str | None) -> list[str]:
        argv = ["opencode", "run", "--format", "json", "--auto"]
        if resume:
            argv += ["--session", resume]
        return argv + list(self.extra_args)

    async def run(self, prompt: str, *, cwd, model: str | None = None, resume: str | None = None,
                  append_system_prompt: str | None = None) -> AsyncIterator[HarnessEvent]:
        session = await self.prepare(cwd)
        route = await routes.resolve(self.executor, session, model or self.model, self.upstream, "opencode")
        instructions = None
        if append_system_prompt:
            digest = hashlib.sha256(append_system_prompt.encode()).hexdigest()[:12]
            instructions = f"/tmp/agentd-opencode-instructions-{digest}.md"
            await self._exec(session, f"cat > {instructions}", stdin=append_system_prompt.encode())
        if resume:
            await self._import(session, resume)
        env = {**OFFLINE_ENV, "OPENCODE_CONFIG_CONTENT": json.dumps(self.opencode_config(route, instructions))}
        lines = self.executor.stream_exec(self.argv(resume), cwd=str(session.workspace), env=env,
                                          stdin=prompt.encode())
        parser = OpenCodeEvents(resume)
        finished = False
        async with aclosing(lines):  # closing it kills opencode in the sandbox
            async for kind, value in lines:
                if kind == "exit":
                    finished = True
                    if parser.session_id and value.get("exit") == 0:
                        await self._export(session, parser.session_id)
                    for event in parser.finish(value.get("exit")):
                        yield event
                    break
                for event in parser.feed(value):
                    yield event
        if not finished and parser.session_id:
            pass  # abandoned: the process was killed; the session isn't exported


class OpenCodeEvents:
    """``opencode run --format json`` lines -> harness events."""

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id
        self.last_text = ""
        self.error: str | None = None
        self.tail: list[str] = []

    def feed(self, line: bytes) -> list[HarnessEvent]:
        try:
            e = json.loads(line)
        except ValueError:
            if line.strip():
                self.tail = (self.tail + [line.decode(errors="replace")])[-20:]
            return []
        self.session_id = e.get("sessionID") or self.session_id
        t = e.get("type")
        part = e.get("part") or {}
        if t == "text" and part.get("text"):
            self.last_text = part["text"]
            return [HarnessEvent("text", text=part["text"])]
        if t == "tool_use":
            state = part.get("state") or {}
            cid, name = part.get("callID", ""), part.get("tool", "")
            failed = state.get("status") == "error"
            return [HarnessEvent("tool_use", name=name, data=state.get("input"), id=cid),
                    HarnessEvent("tool_result", name=name, data=state.get("error") if failed else state.get("output"),
                                 id=cid, is_error=failed)]
        if t == "error":
            err = e.get("error") or {}
            data = err.get("data") if isinstance(err, dict) and isinstance(err.get("data"), dict) else {}
            message = data.get("message") or (err.get("name") if isinstance(err, dict) else None) or json.dumps(err)
            status = data.get("statusCode")
            name = err.get("name") if isinstance(err, dict) else None
            self.error = f"{name} {status}: {message}" if status else message
        return []

    def finish(self, exit_code: Any) -> list[HarnessEvent]:
        if self.error or exit_code not in (0, None):
            message = self.error or f"opencode exited {exit_code}: " + "\n".join(self.tail)
            return [HarnessEvent("result", text=message, session_id=self.session_id, is_error=True)]
        return [HarnessEvent("result", text=self.last_text, session_id=self.session_id)]
