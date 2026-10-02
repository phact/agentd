"""omp (oh-my-pi) as a harness, running entirely inside the sandbox session.

Each turn is one ``omp -p --mode json`` run (``--resume <id>`` to continue),
prompt on stdin, inside the sandbox. Its model calls go to a provider agentd
writes into ``~/.omp/agent/models.yml`` (see :mod:`agentd.harness.routes`),
always through a host proxy that holds the real credential. Tool approval is
``yolo``: the sandbox is the boundary.

Options: ``model`` (default model; a call's ``model=`` wins), ``upstream``
(a :class:`~agentd.model_proxy.ModelUpstream`), ``config`` (omp settings,
passed as a ``--config`` overlay), ``context_window`` / ``max_tokens`` (the
model's limits, which omp can't look up for a custom provider).
"""
from __future__ import annotations

import hashlib
import json
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from agentd.harness import routes
from agentd.harness.events import HarnessEvent
from agentd.model_proxy import ModelUpstream
from agentd.sandbox.executor import SandboxExecutor
from agentd.sandbox.session import SandboxSession

_API = {"anthropic": "anthropic-messages", "openai-responses": "openai-responses", "openai-chat": "openai-completions"}
PROVIDER = "agentd"


@dataclass
class OmpHarness:
    executor: SandboxExecutor
    extra_args: list[str] = field(default_factory=list)  # extra `omp` flags
    upstream: ModelUpstream | None = None
    model: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    context_window: int = 128_000
    max_tokens: int = 16_384
    name = "omp"

    async def prepare(self, cwd) -> SandboxSession:
        return await self.executor.ensure_session(cwd)

    async def _write(self, session: SandboxSession, path: str, content: str) -> None:
        cache = session.__dict__.setdefault("_omp_files", {})
        if cache.get(path) == content:
            return
        out, code = await self.executor.run(session.sandbox.exec(
            ["/bin/sh", "-c", f'mkdir -p "$(dirname {path})" && cat > {path}'], stdin=content.encode()))
        if code != 0:
            raise RuntimeError(f"writing {path} failed: {out.decode(errors='replace')}")
        cache[path] = content

    def models_file(self, route: routes.ModelRoute) -> str:
        base = route.origin if route.api == "anthropic" else route.base_url
        return json.dumps({"providers": {PROVIDER: {  # JSON is valid YAML
            "baseUrl": base, "api": _API[route.api], "apiKey": routes.PLACEHOLDER_KEY,
            "models": [{"id": route.model, "name": route.model,
                        "contextWindow": self.context_window, "maxTokens": self.max_tokens}],
        }}}, indent=2)

    def argv(self, route: routes.ModelRoute, *, resume: str | None, append_system_prompt: str | None,
             config_path: str | None) -> list[str]:
        argv = ["omp", "-p", "--mode", "json", "--model", f"{PROVIDER}/{route.model}",
                "--approval-mode", "yolo", "--no-title", "--no-lsp"]
        if resume:
            argv += ["--resume", resume]
        if append_system_prompt:
            argv += ["--append-system-prompt", append_system_prompt]
        if config_path:
            argv += ["--config", config_path]
        return argv + list(self.extra_args)

    async def run(self, prompt: str, *, cwd, model: str | None = None, resume: str | None = None,
                  append_system_prompt: str | None = None) -> AsyncIterator[HarnessEvent]:
        session = await self.prepare(cwd)
        route = await routes.resolve(self.executor, session, model or self.model, self.upstream, "omp")
        await self._write(session, "/home/agent/.omp/agent/models.yml", self.models_file(route))
        config_path = None
        if self.config:
            text = json.dumps(self.config, indent=2)
            config_path = f"/home/agent/.omp/agent/agentd-{hashlib.sha256(text.encode()).hexdigest()[:12]}.yml"
            await self._write(session, config_path, text)
        argv = self.argv(route, resume=resume, append_system_prompt=append_system_prompt, config_path=config_path)
        lines = self.executor.stream_exec(argv, cwd=str(session.workspace), stdin=prompt.encode())
        parser = OmpEvents(resume)
        async with aclosing(lines):  # closing it kills omp in the sandbox
            async for kind, value in lines:
                if kind == "exit":
                    for event in parser.finish(value.get("exit")):
                        yield event
                    break
                for event in parser.feed(value):
                    yield event


class OmpEvents:
    """omp's ``--mode json`` lines -> harness events."""

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id
        self.last_text = ""
        self.error: str | None = None
        self.ended = False
        self.tail: list[str] = []

    def feed(self, line: bytes) -> list[HarnessEvent]:
        try:
            e = json.loads(line)
        except ValueError:
            if line.strip():
                self.tail = (self.tail + [line.decode(errors="replace")])[-20:]
            return []
        t = e.get("type")
        if t == "session":
            self.session_id = e.get("id") or self.session_id
        elif t == "tool_execution_start":
            return [HarnessEvent("tool_use", name=e.get("toolName", ""), data=e.get("args"), id=e.get("toolCallId", ""))]
        elif t == "tool_execution_end":
            result = e.get("result") or {}
            return [HarnessEvent("tool_result", name=e.get("toolName", ""), data=result.get("content", result),
                                 id=e.get("toolCallId", ""), is_error=bool(e.get("isError")))]
        elif t == "message_end":
            msg = e.get("message") or {}
            if msg.get("role") != "assistant":
                return []
            if msg.get("stopReason") == "error" or msg.get("errorMessage"):
                self.error = msg.get("errorMessage") or "the model call failed"
            text = "".join(c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text")
            if text:
                self.last_text = text
                return [HarnessEvent("text", text=text)]
        elif t == "agent_end":
            self.ended = True
        return []

    def finish(self, exit_code: Any) -> list[HarnessEvent]:
        if self.error or not self.ended or exit_code not in (0, None):
            message = self.error or f"omp exited {exit_code}: " + "\n".join(self.tail)
            return [HarnessEvent("result", text=message, session_id=self.session_id, is_error=True)]
        return [HarnessEvent("result", text=self.last_text, session_id=self.session_id)]
