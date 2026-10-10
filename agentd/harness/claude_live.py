"""One long-lived ``claude`` process per conversation, in the sandbox.

``claude -p --input-format stream-json`` keeps reading user messages from
stdin, so a conversation can stay in one process: its background tasks
(``Bash`` with ``run_in_background``, background agents) outlive the turn
that started them, and when one finishes the CLI starts a turn on its own.

    {"type": "user", "uuid": ..., "message": {"role": "user", "content": ...}}   # a turn, on stdin
    {"type": "control_request", "request_id": ..., "request": {"subtype": "interrupt"}}

Each message carries a uuid; the CLI reports ``command_lifecycle`` events for
it (queued, started, completed), which is how output is attributed:

  * events between a message's ``started`` and the next ``result`` answer it.
    A message written while a turn is running may be folded into that turn
    (at its next tool result): it then takes over the rest of the turn, and
    the earlier turn's stream ends.
  * events that start while no turn is running are an **unprompted** turn
    (after a background task's ``task_notification``); they go to
    ``on_unprompted`` as a stream of their own.
  * ``interrupt`` ends the running turn (its ``result`` has
    ``subtype: error_during_execution``) without ending the process or its
    background tasks.

``background_tasks_changed`` events say which background tasks are running;
the process is closed (stdin closed, so it exits) once it has been idle for
``idle`` seconds with none, and the next turn starts a new one with
``--resume``.

The process's I/O and bookkeeping run on the executor's loop (which outlives
any one caller's: a sync client runs each call on a loop of its own); the
public methods and :meth:`LiveTurn.events` work from any loop. The
``on_*`` callbacks are called on the executor's loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid as _uuid
from typing import Any, AsyncIterator, Callable

from agentd.harness.claude_cli import parse_line
from agentd.harness.events import HarnessEvent

logger = logging.getLogger(__name__)

READ_SIZE = 64 * 1024
CONTROL_TIMEOUT = 15.0   # seconds to wait for a control_response
SETTLE_TIMEOUT = 20.0    # seconds an interrupted turn has to end before a new one goes in


async def _direct(coro):
    return await coro


class LiveTurn:
    """One turn's events, ending with a ``result``."""

    def __init__(self, uuid: str | None = None, *, prompted: bool = True, run: Callable = _direct):
        self.uuid = uuid
        self._run = run  # runs a coroutine on the loop that owns this turn
        self.prompted = prompted
        self.session_id: str | None = None
        self.started = False
        self.abandoned = False      # its reader went away: interrupt it when it runs
        self.interrupted = False
        self._done = asyncio.Event()
        self._queue: asyncio.Queue[HarnessEvent] = asyncio.Queue()

    def put(self, event: HarnessEvent) -> None:
        if self._done.is_set():
            return
        self._queue.put_nowait(event)
        if event.kind == "result":
            self._done.set()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    async def wait(self) -> None:
        """Until the turn's result is in (from any loop)."""
        await self._run(self._done.wait())

    async def events(self) -> AsyncIterator[HarnessEvent]:
        """The turn's events, up to and including its result (from any loop)."""
        while True:
            event = await self._run(self._queue.get())
            yield event
            if event.kind == "result":
                return


class ClaudeProcess:
    """A ``claude`` process in the sandbox that takes one message per turn on stdin."""

    def __init__(self, executor, argv: list[str], *, cwd: str, env: dict[str, str], model: str | None = None,
                 idle: float = 600.0, on_unprompted: Callable[[LiveTurn], Any] | None = None,
                 on_session: Callable[["ClaudeProcess"], Any] | None = None,
                 on_exit: Callable[["ClaudeProcess"], Any] | None = None):
        self.executor = executor
        self.argv, self.cwd, self.env = argv, cwd, env
        self.model = model
        self.idle = idle
        self.on_unprompted, self.on_session, self.on_exit = on_unprompted, on_session, on_exit
        self.session_id: str | None = None
        self.background: list[dict] = []     # running background tasks (background_tasks_changed)
        self.alive = False
        self._stream = None
        self._turns: dict[str, LiveTurn] = {}    # written messages not finished yet, by uuid
        self._current: LiveTurn | None = None    # the turn the CLI is running
        self._notes: list[dict] = []             # task notifications since the last turn
        self._lifecycle_seen = False
        self._controls: dict[str, asyncio.Future] = {}
        self._tail: list[str] = []
        self._last = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        await self.executor.run(self._start())

    async def _start(self) -> None:
        session = self.executor.session
        self._loop = asyncio.get_running_loop()
        self._stream = await session.sandbox.open_exec(
            self.argv, cwd=self.cwd, env={**session.base_env(), **self.executor.extra_env, **self.env})
        self.alive = True
        self._tasks = [asyncio.ensure_future(self._read()), asyncio.ensure_future(self._idle_watch())]

    def soon(self, coro) -> None:
        """Run ``coro`` on the process's loop, from any thread, without waiting."""
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    @property
    def busy(self) -> bool:
        """A turn is running or waiting, or a background task is."""
        return self._current is not None or bool(self._turns) or bool(self.background)

    async def send(self, prompt: str | list) -> LiveTurn:
        """Write a user message; its turn's events follow."""
        return await self.executor.run(self._send(prompt))

    async def _send(self, prompt: str | list) -> LiveTurn:
        turn = LiveTurn(str(_uuid.uuid4()), run=self.executor.run)
        self._turns[turn.uuid] = turn
        await self._write({"type": "user", "uuid": turn.uuid, "session_id": self.session_id or "",
                           "parent_tool_use_id": None, "message": {"role": "user", "content": prompt}})
        return turn

    async def settle(self) -> None:
        """Before a new question: interrupt a running prompted turn and let it end.
        A running unprompted turn is left alone (the new message queues behind it)."""
        await self.executor.run(self._settle())

    async def _settle(self) -> None:
        turn = self._current
        if turn is None or not turn.prompted or turn._done.is_set():
            return
        await self._interrupt(turn)
        try:
            await asyncio.wait_for(turn._done.wait(), SETTLE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("claude: an interrupted turn didn't end in %ss", SETTLE_TIMEOUT)

    async def interrupt(self, turn: LiveTurn | None = None) -> bool:
        """Interrupt ``turn`` if it's running (any running turn if None); one not
        started yet is interrupted as soon as it starts."""
        return await self.executor.run(self._interrupt(turn))

    async def _interrupt(self, turn: LiveTurn | None = None) -> bool:
        if turn is not None and not turn.started:
            turn.abandoned = True
            return True
        if turn is not None and (turn is not self._current or turn._done.is_set()):
            return True
        if turn is not None:
            if turn.interrupted:
                return True
            turn.interrupted = True
        response = await self._control({"subtype": "interrupt"})
        return response is not None and response.get("subtype") == "success"

    async def set_model(self, model: str) -> bool:
        response = await self.executor.run(self._control({"subtype": "set_model", "model": model}))
        if response is not None and response.get("subtype") == "success":
            self.model = model
            return True
        logger.warning("claude: set_model %s failed: %s", model, response)
        return False

    async def _control(self, request: dict) -> dict | None:
        """Send a control request; its response (None on timeout or if the process ended)."""
        if not self.alive:
            return None
        request_id = f"req_{os.urandom(6).hex()}"
        fut = asyncio.get_running_loop().create_future()
        self._controls[request_id] = fut
        try:
            await self._write({"type": "control_request", "request_id": request_id, "request": request})
            return await asyncio.wait_for(fut, CONTROL_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return None
        finally:
            self._controls.pop(request_id, None)

    async def close(self, timeout: float = 10.0) -> None:
        """Close stdin (the CLI exits); kill it if it doesn't within ``timeout``."""
        if self._stream is not None:
            await self.executor.run(self._close(timeout))

    async def _close(self, timeout: float) -> None:
        stream, alive = self._stream, self.alive
        if alive:
            try:
                await stream.write_eof()
            except Exception:  # noqa: BLE001
                pass
            reader = self._tasks[0] if self._tasks else None
            if reader is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(reader), timeout)
                except asyncio.TimeoutError:
                    pass
        if self.alive:
            try:
                await stream.close({"signal": "cancel"})
            except Exception:  # noqa: BLE001
                pass
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()

    # ------------------------------------------------------------------ #

    async def _write(self, obj: dict) -> None:
        async with self._write_lock:
            await self._stream.write((json.dumps(obj) + "\n").encode())
        self._last = time.monotonic()

    async def _read(self) -> None:
        buf = b""
        meta: dict = {}
        try:
            while chunk := await self._stream.read(READ_SIZE):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    self._on_line(line)
            if buf:
                self._on_line(buf)
            meta = await self._stream.wait_closed() or {}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            meta = {"error": str(e)}
        finally:
            self._ended(meta)

    def _ended(self, meta: dict) -> None:
        if not self.alive:
            return
        self.alive = False
        why = f"claude exited {meta.get('exit', meta.get('error', ''))}: " + "\n".join(self._tail)
        for turn in [*self._turns.values(), *([self._current] if self._current else [])]:
            turn.put(HarnessEvent("result", text=why, session_id=self.session_id, is_error=True))
        self._turns.clear()
        self._current = None
        for fut in self._controls.values():
            if not fut.done():
                fut.set_result(None)
        if self.on_exit is not None:
            self.on_exit(self)

    def _on_line(self, line: bytes) -> None:
        self._last = time.monotonic()
        try:
            obj = json.loads(line)
        except ValueError:
            if line.strip():
                self._tail = (self._tail + [line.decode(errors="replace")])[-20:]
            return
        if not isinstance(obj, dict):
            return
        kind, sub = obj.get("type"), obj.get("subtype")
        if obj.get("session_id") and obj["session_id"] != self.session_id:
            self.session_id = obj["session_id"]
            if self.on_session is not None:
                self.on_session(self)
        if kind == "control_response":
            response = obj.get("response") or {}
            fut = self._controls.get(response.get("request_id"))
            if fut is not None and not fut.done():
                fut.set_result(response)
        elif kind == "command_lifecycle":
            self._lifecycle_seen = True
            self._lifecycle(obj.get("command_uuid"), obj.get("state"))
        elif kind == "system":
            if sub == "background_tasks_changed":
                self.background = list(obj.get("tasks") or [])
            elif sub == "task_notification" and self._current is None:
                self._notes.append(obj)
        elif kind == "user" and obj.get("isReplay"):
            pass
        elif kind in ("assistant", "user", "result"):
            if self._current is None:
                waiting = [t for t in self._turns.values() if not t.started]
                if waiting and not self._lifecycle_seen:  # a CLI without command_lifecycle: oldest first
                    self._lifecycle(waiting[0].uuid, "started")
                else:
                    self._unprompted()
            turn = self._current
            for event in parse_line(line):
                turn.put(event)
            if kind == "result":
                turn.session_id = obj.get("session_id")
                self._current = None
                if not self._lifecycle_seen:
                    self._turns.pop(turn.uuid or "", None)

    def _lifecycle(self, command: str | None, state: str | None) -> None:
        turn = self._turns.get(command or "")
        if turn is None:
            return
        if state == "started":
            current = self._current
            if current is not None and current is not turn and not current._done.is_set():
                # Folded into the running turn: the rest of it answers this message.
                current.put(HarnessEvent("result", session_id=self.session_id, data={"continued_by": turn.uuid}))
            turn.started = True
            self._current = turn
            if turn.abandoned:
                asyncio.ensure_future(self._interrupt(turn))
        elif state != "queued":
            self._turns.pop(turn.uuid, None)
            if turn is not self._current and not turn._done.is_set():
                turn.put(HarnessEvent("result", text=f"the message was {state} before it ran",
                                      session_id=self.session_id, is_error=True))

    def _unprompted(self) -> None:
        turn = LiveTurn(prompted=False, run=self.executor.run)
        turn.session_id = self.session_id
        turn.started = True
        for note in self._notes:
            turn.put(HarnessEvent("task", text=note.get("summary") or "", data=note, session_id=self.session_id))
        self._notes = []
        self._current = turn
        if self.on_unprompted is not None:
            try:
                self.on_unprompted(turn)
            except Exception:  # noqa: BLE001
                logger.exception("on_unprompted failed")

    async def _idle_watch(self) -> None:
        """Close the process once it's been idle ``idle`` seconds with no background tasks."""
        while self.alive:
            await asyncio.sleep(min(15.0, max(self.idle / 4, 0.05)))
            if self.alive and not self.busy and time.monotonic() - self._last >= self.idle:
                logger.info("claude: closing idle session %s", self.session_id)
                await self._close(10.0)
                return
