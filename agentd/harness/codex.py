"""OpenAI Codex CLI as a harness, running entirely inside the sandbox session.

Each turn is one ``codex exec --json`` run (``codex exec resume <id>`` to
continue a thread) inside the microVM, so Codex and every command it runs
live there. Codex's own sandboxing is turned
off (``--dangerously-bypass-approvals-and-sandbox``): the VM is the boundary.

Credentials never enter the sandbox. Codex's model calls go to the
sandbox-side ``openai`` endpoint (``openai_base_url``), tunneled to a host
proxy that adds the real credential:

  * ChatGPT login (host ``~/.codex/auth.json``): the sandbox gets a
    placeholder ChatGPT ``auth.json`` — dummy tokens with a far-future expiry
    so Codex never tries to refresh, carrying only the non-secret plan type
    and account id Codex uses to pick models — and the proxy swaps in the
    real ``Authorization`` / ``ChatGPT-Account-ID``.
  * ``OPENAI_API_KEY`` on the host: the sandbox gets a placeholder API key.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from agentd.harness.events import HarnessEvent
from agentd.sandbox.executor import SandboxExecutor
from agentd.sandbox.session import OPENAI_ENDPOINT, SandboxSession
from agentd.model_proxy import CodexChatGPTCredentials

PLACEHOLDER = "agentd-sandbox-placeholder"


def _fake_jwt(claims: dict) -> str:
    """An unsigned JWT-shaped string; the proxy replaces it before anything leaves the host."""
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.{PLACEHOLDER}"


@dataclass
class CodexHarness:
    executor: SandboxExecutor
    extra_args: list[str] = field(default_factory=list)  # extra `codex exec` flags
    name = "codex"

    def codex_home_files(self, session: SandboxSession) -> dict[str, str]:
        """``~/.codex`` contents for the sandbox user (no real credentials)."""
        base = f"http://{OPENAI_ENDPOINT[1]}:{OPENAI_ENDPOINT[2]}"
        creds = session.openai_credentials
        if isinstance(creds, CodexChatGPTCredentials):
            claims = creds.claims()
            far = int(time.time()) + 365 * 86400
            auth = {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _fake_jwt({"https://api.openai.com/auth": claims, "exp": far}),
                    "access_token": _fake_jwt({"exp": far}),
                    "refresh_token": PLACEHOLDER,
                    "account_id": claims.get("chatgpt_account_id"),
                },
                "last_refresh": datetime.now(timezone.utc).isoformat(),
            }
            # No URL overrides: ChatGPT mode requires https origins, so the
            # sandbox serves chatgpt.com itself (agentd.sandbox.tls) and Codex
            # talks to its usual https://chatgpt.com/backend-api/codex.
            base_url = None
        else:
            auth = {"auth_mode": "apikey", "OPENAI_API_KEY": PLACEHOLDER}
            base_url = f"{base}/v1"
        config = f"openai_base_url = {json.dumps(base_url)}\n" if base_url else ""
        config += f"\n[projects.{json.dumps(str(session.workspace))}]\ntrust_level = \"trusted\"\n"
        return {"config.toml": config, "auth.json": json.dumps(auth)}

    async def prepare(self, cwd) -> SandboxSession:
        session = await self.executor.ensure_session(cwd)
        if not getattr(session, "_codex_ready", False):
            for name, content in self.codex_home_files(session).items():
                out, code = await self.executor.run(session.sandbox.exec(
                    ["/bin/sh", "-c", f"mkdir -p ~/.codex && umask 077 && cat > ~/.codex/{name}"],
                    stdin=content.encode(),
                ))
                if code != 0:
                    raise RuntimeError(f"writing ~/.codex/{name} failed: {out.decode(errors='replace')}")
            session._codex_ready = True
        return session

    async def run(
        self,
        prompt: str,
        *,
        cwd,
        model: str | None = None,
        resume: str | None = None,
        append_system_prompt: str | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        session = await self.prepare(cwd)
        args = ["exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                "-C", str(session.workspace), *self.extra_args]
        if model:
            args += ["-m", model]
        if append_system_prompt:
            args += ["-c", f"developer_instructions={json.dumps(append_system_prompt)}"]
        if resume:
            args += ["resume", resume]
        args.append("-")  # prompt on stdin

        thread_id, last_message, done, tail = resume, "", False, []
        async for kind, value in self.executor.stream_exec(
            ["codex", *args], cwd=str(session.workspace), stdin=prompt.encode()
        ):
            if kind == "exit":
                if not done:
                    yield HarnessEvent("result", text=f"codex exited {value.get('exit')}: " + "\n".join(tail),
                                       session_id=thread_id, is_error=True)
                break
            try:
                event = json.loads(value)
            except ValueError:
                if value.strip():  # stderr is merged into the stream
                    tail = (tail + [value.decode(errors="replace")])[-20:]
                continue
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = event.get("thread_id") or thread_id
            elif etype == "item.completed":
                item = event.get("item") or {}
                for ev in _item_events(item):
                    if ev.kind == "text":
                        last_message = ev.text
                    yield ev
            elif etype == "turn.completed":
                done = True
                yield HarnessEvent("result", text=last_message, session_id=thread_id, data=event.get("usage"))
            elif etype == "error":
                # Non-fatal (e.g. "Reconnecting... 2/5"); turn.failed ends the turn.
                yield HarnessEvent("tool_result", name="codex.error", data=event.get("message"), is_error=True)
            elif etype == "turn.failed":
                err = event.get("error") or event
                done = True
                yield HarnessEvent("result", text=err.get("message", json.dumps(err)), session_id=thread_id,
                                   is_error=True)


def _item_events(item: dict[str, Any]) -> list[HarnessEvent]:
    kind = item.get("type")
    if kind == "agent_message":
        return [HarnessEvent("text", text=item.get("text", ""))]
    if kind == "command_execution":
        return [
            HarnessEvent("tool_use", name="shell", data={"command": item.get("command")}),
            HarnessEvent("tool_result", name="shell", data=item.get("aggregated_output", ""),
                         is_error=item.get("exit_code") not in (0, None)),
        ]
    if kind == "mcp_tool_call":
        name = f"{item.get('server')}.{item.get('tool')}"
        return [
            HarnessEvent("tool_use", name=name, data=item.get("arguments")),
            HarnessEvent("tool_result", name=name, data=item.get("result") or item.get("error"),
                         is_error=item.get("error") is not None),
        ]
    if kind == "web_search":
        return [HarnessEvent("tool_use", name="web_search", data={"query": item.get("query")})]
    if kind == "file_change":
        return [HarnessEvent("tool_use", name="apply_patch", data=item.get("changes"))]
    return []
