"""Sandboxes for ``agentd serve``: one per (workspace, image), stopped when idle.

A sandbox stays up while its workspace is in use and stops ``idle_timeout``
seconds after its last turn. The next turn boots a fresh one and the harness
resumes its native session from the transcript store, so only processes and
files outside the workspace are lost (see docs/agentd-serve.md).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentd.serve.config import ServeConfig

logger = logging.getLogger(__name__)


@dataclass
class Entry:
    workspace: Path
    image: str | None
    executor: Any
    client: Any                       # per-sandbox harness state (agentd.harness.chat._state)
    active: int = 0
    last_used: float = field(default_factory=time.monotonic)

    @property
    def running(self) -> bool:
        return self.executor.session is not None


class SandboxPool:
    def __init__(self, config: ServeConfig, approvals=None):
        self.config = config
        self.approvals = approvals
        self.entries: dict[tuple[str, str | None], Entry] = {}
        self._reaper: asyncio.Task | None = None

    def _client(self) -> Any:
        options = {h: self.config.harness_kwargs(h) for h in self.config.harness_options}
        return SimpleNamespace(_harness_options=options)

    def acquire(self, workspace: Path, image: str | None) -> Entry:
        """The entry for a workspace, marked in use (call :meth:`release` after)."""
        key = (str(workspace), image)
        entry = self.entries.get(key)
        if entry is None:
            workspace.mkdir(parents=True, exist_ok=True)
            executor = self.config.make_executor(workspace, image, self.config.make_egress(self.approvals))
            entry = Entry(workspace, image, executor, self._client())
            self.entries[key] = entry
        entry.active += 1
        entry.last_used = time.monotonic()
        return entry

    def release(self, entry: Entry) -> None:
        entry.active = max(0, entry.active - 1)
        entry.last_used = time.monotonic()

    def state(self, workspace: str, image: str | None) -> str:
        entry = self.entries.get((workspace, image))
        if entry is None or not entry.running:
            return "stopped"
        return "busy" if entry.active else "idle"

    async def stop(self, entry: Entry) -> None:
        if entry.running:
            logger.info("agentd serve: stopping the sandbox for %s", entry.workspace)
            await entry.executor.stop_session()

    async def reap_once(self) -> None:
        now = time.monotonic()
        for entry in list(self.entries.values()):
            if entry.running and not entry.active and now - entry.last_used >= self.config.idle_timeout:
                try:
                    await self.stop(entry)
                except Exception:
                    logger.exception("agentd serve: stopping an idle sandbox failed")

    def start(self) -> None:
        async def loop():
            while True:
                await asyncio.sleep(min(30.0, max(1.0, self.config.idle_timeout / 4)))
                await self.reap_once()
        self._reaper = asyncio.ensure_future(loop())

    async def close(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for entry in self.entries.values():
            await asyncio.to_thread(entry.executor.close)
        self.entries.clear()
