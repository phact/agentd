"""Claude Code as a harness, running entirely inside the sandbox session.

The CLI and every tool it runs live in the microVM. Its model calls go to
the sandbox-side ``anthropic`` endpoint, tunneled to the host proxy that adds
the real credential; the sandbox holds a placeholder. Permissions are
``bypassPermissions``: the VM is the boundary.

By default a conversation keeps one ``claude`` process
(:mod:`agentd.harness.claude_live`): a turn is a message on its stdin, so
background tasks survive between turns, and the turns the CLI starts on its
own when one finishes go to ``on_unprompted``. Cancelling a turn interrupts
it; the process stays. A process idle for ``idle_minutes`` with no
background tasks is closed and the next turn resumes it with ``--resume``.
A model change is applied with ``set_model``. With ``persistent=False``
each turn is its own ``claude -p`` run, ended when the turn is.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from agentd.harness import claude_cli
from agentd.harness.claude_live import ClaudeProcess, LiveTurn
from agentd.harness.events import HarnessEvent
from agentd.sandbox.executor import SandboxExecutor
from agentd.sandbox.session import SandboxSession

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeHarness:
    executor: SandboxExecutor
    extra_args: list[str] = field(default_factory=list)  # extra `claude` flags
    persistent: bool = True
    idle_minutes: float = 10.0
    # Called with each unprompted turn (a LiveTurn: .session_id, .events()) on the loop
    # of the latest turn (agentd's executor loop if that one is gone); it may return a
    # coroutine. Without one they go to .unprompted (an asyncio.Queue on that loop).
    on_unprompted: Callable[[LiveTurn], Any] | None = None
    name = "claude-code"
    _live: dict[str, ClaudeProcess] = field(default_factory=dict, init=False, repr=False)
    _unprompted: asyncio.Queue | None = field(default=None, init=False, repr=False)
    _caller_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)

    @property
    def unprompted(self) -> asyncio.Queue:
        if self._unprompted is None:
            self._unprompted = asyncio.Queue()
        return self._unprompted

    async def prepare(self, cwd) -> SandboxSession:
        session = await self.executor.ensure_session(cwd)
        if not getattr(session, "_claude_ready", False):
            # Let Claude Code discover agentd's generated skills natively.
            skills = shlex.quote(str(session.base_env()["PTC_SKILLS_DIR"]))
            await self.executor.run(session.sandbox.exec(
                ["/bin/sh", "-c", f"mkdir -p ~/.claude && ln -sfn {skills} ~/.claude/skills"]
            ))
            session._claude_ready = True
        return session

    def _argv(self, *, model, resume, append_system_prompt, live: bool) -> list[str]:
        extra = ["--settings", json.dumps(claude_cli.SANDBOX_SETTINGS)]
        if live:
            extra += ["--input-format", "stream-json", "--replay-user-messages"]
        return claude_cli.claude_argv(
            model=model, resume=resume, append_system_prompt=append_system_prompt,
            setting_sources=["user", "project"], permission_mode="bypassPermissions",
            extra_args=[*extra, *self.extra_args],
        )

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
        if not self.persistent:
            argv = self._argv(model=model, resume=resume, append_system_prompt=append_system_prompt, live=False)
            events = claude_cli.run_in_sandbox(self.executor, argv, prompt, cwd=str(session.workspace))
            async with aclosing(events):
                async for event in events:
                    yield event
            return

        self._caller_loop = asyncio.get_running_loop()
        proc = await self._process(session, model=model, resume=resume, append_system_prompt=append_system_prompt)
        await proc.settle()
        turn = await proc.send(prompt)
        try:
            async for event in turn.events():
                yield event
        finally:
            if not turn.finished and proc.alive:
                # Abandoned (cancelled): interrupt the turn, keep the process and its background tasks.
                proc.soon(proc._interrupt(turn))

    async def _process(self, session, *, model, resume, append_system_prompt) -> ClaudeProcess:
        """The conversation's live process (``resume``'s), else a new one."""
        proc = self._live.get(resume) if resume else None
        if proc is not None and proc.alive:
            if model and model != proc.model and not await proc.set_model(model):
                await proc.close()
            else:
                return proc
        argv = self._argv(model=model, resume=resume, append_system_prompt=append_system_prompt, live=True)
        proc = ClaudeProcess(
            self.executor, argv, cwd=str(session.workspace), env=claude_cli.sandbox_env(), model=model,
            idle=self.idle_minutes * 60, on_unprompted=self._deliver, on_session=self._register,
            on_exit=self._forget)
        if resume:
            proc.session_id = resume
            self._live[resume] = proc
        await proc.start()
        return proc

    def _register(self, proc: ClaudeProcess) -> None:
        self._live[proc.session_id] = proc

    def _forget(self, proc: ClaudeProcess) -> None:
        for sid in [sid for sid, p in self._live.items() if p is proc]:
            del self._live[sid]

    def _deliver(self, turn: LiveTurn) -> None:
        """(On the executor's loop.) Hand an unprompted turn to the caller's loop."""
        def deliver():
            if self.on_unprompted is not None:
                result = self.on_unprompted(turn)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            else:
                self.unprompted.put_nowait(turn)

        loop = self._caller_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(deliver)
        else:
            deliver()
        asyncio.ensure_future(self._sync_after(turn))

    async def _sync_after(self, turn: LiveTurn) -> None:
        """Keep the host's transcript copy current after an unprompted turn."""
        await turn._done.wait()
        session = self.executor.session
        if session is not None:
            await asyncio.to_thread(session.sync_transcripts_out, self.name)

    async def close(self) -> None:
        """End every live process (their background tasks too)."""
        await asyncio.gather(*(p.close() for p in set(self._live.values())), return_exceptions=True)
