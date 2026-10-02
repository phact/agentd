"""Running turns: a task per turn, its Responses events buffered for (re)attaching.

A turn is either tied to its request (the default: if the caller goes away,
the turn is cancelled, which kills the harness in the sandbox) or runs in the
background (``background=true``) until it finishes or is cancelled. Either
way its events are kept, in order, so callers can attach from any sequence
number (``starting_after``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing
from typing import Any, AsyncIterator, Callable

logger = logging.getLogger(__name__)

FINAL = ("response.completed", "response.failed", "response.incomplete")


class Turn:
    def __init__(self, session_id: str, owner: str, background: bool):
        self.session_id = session_id
        self.owner = owner
        self.background = background
        self.response_id: str | None = None
        self.events: list[dict[str, Any]] = []   # each: the event as JSON, with "type" and "sequence_number"
        self.final: dict[str, Any] | None = None  # the final response
        self.started = asyncio.Event()            # first event (response.created) seen
        self.done = asyncio.Event()
        self._changed = asyncio.Condition()
        self.task: asyncio.Task | None = None
        self.finished_at: float | None = None

    @property
    def snapshot(self) -> dict[str, Any] | None:
        """The latest response object seen (final, else the in-progress one)."""
        if self.final is not None:
            return self.final
        for event in reversed(self.events):
            if "response" in event:
                return event["response"]
        return None

    async def _add(self, event: dict[str, Any]) -> None:
        async with self._changed:
            self.events.append(event)
            if "response" in event and self.response_id is None:
                self.response_id = event["response"]["id"]
            if event.get("type") in FINAL:
                self.final = event["response"]
            self._changed.notify_all()
        self.started.set()

    async def _finish(self) -> None:
        async with self._changed:
            self.finished_at = time.monotonic()
            self.done.set()
            self.started.set()
            self._changed.notify_all()

    async def follow(self, after: int = -1) -> AsyncIterator[dict[str, Any]]:
        """Events with sequence_number > ``after``, live until the turn ends."""
        i = 0
        while True:
            async with self._changed:
                while i >= len(self.events) and not self.done.is_set():
                    await self._changed.wait()
                batch = self.events[i:]
                i = len(self.events)
                ended = self.done.is_set()
            for event in batch:
                if event.get("sequence_number", 0) > after:
                    yield event
            if ended and i >= len(self.events):
                return

    def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()


def _ended_event(turn: Turn, status: str, code: str, message: str) -> dict[str, Any]:
    base = dict(turn.snapshot or {"id": turn.response_id or "resp_unknown", "object": "response", "output": []})
    response = {**base, "status": status, "error": {"code": code, "message": message}}
    seq = (turn.events[-1].get("sequence_number", -1) + 1) if turn.events else 0
    return {"type": "response.failed", "response": response, "sequence_number": seq}


class Turns:
    """Running and recently finished turns, by response id."""

    KEEP_FINISHED = 200

    def __init__(self):
        self.by_response: dict[str, Turn] = {}

    def get(self, response_id: str) -> Turn | None:
        return self.by_response.get(response_id)

    def start(self, turn: Turn, events: AsyncIterator[Any], *,
              on_event: Callable[[Turn, dict], None], on_done: Callable[[Turn], Any]) -> None:
        """Run ``events`` (agentd.harness.responses.stream_response) as ``turn``."""

        async def run() -> None:
            try:
                async with aclosing(events):  # closing it kills the harness in the sandbox
                    async for ev in events:
                        data = ev if isinstance(ev, dict) else ev.model_dump(mode="json", exclude_none=True)
                        on_event(turn, data)
                        await turn._add(data)
                        if turn.response_id and turn.response_id not in self.by_response:
                            self.by_response[turn.response_id] = turn
            except asyncio.CancelledError:
                await turn._add(_ended_event(turn, "cancelled", "cancelled", "the turn was cancelled"))
            except Exception as e:
                logger.exception("agentd serve: turn failed")
                await turn._add(_ended_event(turn, "failed", "server_error", f"{type(e).__name__}: {e}"))
            finally:
                if turn.final is None and turn.events:
                    await turn._add(_ended_event(turn, "failed", "server_error", "the turn ended without a result"))
                await turn._finish()
                try:
                    result = on_done(turn)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception("agentd serve: recording a turn failed")
                self._trim()

        turn.task = asyncio.ensure_future(run())

    def _trim(self) -> None:
        finished = sorted((t for t in self.by_response.values() if t.done.is_set()),
                          key=lambda t: t.finished_at or 0)
        for turn in finished[:-self.KEEP_FINISHED] if len(finished) > self.KEEP_FINISHED else []:
            self.by_response.pop(turn.response_id, None)
