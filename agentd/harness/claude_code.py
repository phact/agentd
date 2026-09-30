"""Claude Code as a harness, running entirely inside the sandbox session.

Each turn runs ``claude -p --output-format stream-json`` (``--resume`` to
continue a session) inside the microVM, so the CLI and every tool it runs
live there. Its model calls go to the sandbox-side ``anthropic`` endpoint,
tunneled to the host proxy that adds the real credential; the sandbox holds a
placeholder. Permissions are ``bypassPermissions``: the VM is the boundary.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import AsyncIterator

from agentd.harness import claude_cli
from agentd.harness.events import HarnessEvent
from agentd.sandbox.executor import SandboxExecutor
from agentd.sandbox.session import SandboxSession


@dataclass
class ClaudeCodeHarness:
    executor: SandboxExecutor
    extra_args: list[str] = field(default_factory=list)  # extra `claude` flags
    name = "claude-code"

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
        argv = claude_cli.claude_argv(
            model=model, resume=resume, append_system_prompt=append_system_prompt,
            setting_sources=["user", "project"], permission_mode="bypassPermissions",
            extra_args=self.extra_args,
        )
        async for event in claude_cli.run_in_sandbox(self.executor, argv, prompt, cwd=str(session.workspace)):
            yield event
