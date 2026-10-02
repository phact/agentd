"""Client for ``agentd serve`` on this box or others (see docs/agentd-serve.md).

    fleet = Fleet.from_config()   # ~/.agentd/fleet.json
    box = fleet.box("sabik")
    sessions = await box.sessions()
    reply = await box.start("Fix the flaky test in ~/src/app", workspace="app")
    async for event in box.stream("and add a regression test", session_id=reply["agentd"]["session"]):
        ...

Transports:

  * :class:`LocalTransport` -- this box's ``serve.sock``.
  * :class:`P2clawTransport` -- another box's private ``agentd`` app through
    the local p2claw agent's Unix socket (``p2claw-agent-client``, an optional
    dependency). Never ``p2claw apps connect``, which would open a TCP port.

``fleet.json``::

    {"boxes": {
        "local": {"local": true, "drive": true},
        "sabik": {"peer": "<alias or peer id>", "drive": true},
        "vega":  {"peer": "<alias or peer id>"}
    }}

Boxes without ``"drive": true`` are read-only for fleet skills.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from agentd.sandbox.base import DEFAULT_HOME

FLEET_CONFIG = DEFAULT_HOME / "fleet.json"


class RemoteError(RuntimeError):
    def __init__(self, status: int, body: Any):
        message = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"agentd serve answered {status}: {message}")
        self.status, self.body = status, body


def parse_sse(buffer: bytes) -> tuple[list[dict[str, Any]], bytes]:
    """(complete events, leftover bytes) from a server-sent-events buffer."""
    events = []
    while b"\n\n" in buffer:
        block, buffer = buffer.split(b"\n\n", 1)
        data = [line[5:].strip() for line in block.split(b"\n") if line.startswith(b"data:")]
        if data:
            events.append(json.loads(b"\n".join(data)))
    return events, buffer


class Transport(Protocol):
    async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]: ...
    def stream(self, method: str, path: str, body: Any = None) -> AsyncIterator[dict[str, Any]]: ...


class LocalTransport:
    """``agentd serve`` on this box, over its local socket."""

    def __init__(self, socket_path: str | Path | None = None):
        self.socket_path = Path(socket_path or DEFAULT_HOME / "serve" / "serve.sock")

    def _session(self):
        import aiohttp

        return aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(self.socket_path)),
                                     timeout=aiohttp.ClientTimeout(total=None, sock_read=None))

    async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        async with self._session() as http:
            async with http.request(method, "http://agentd" + path, json=body) as r:
                return r.status, await r.json(content_type=None)

    async def stream(self, method: str, path: str, body: Any = None) -> AsyncIterator[dict[str, Any]]:
        async with self._session() as http:
            async with http.request(method, "http://agentd" + path, json=body) as r:
                if r.status != 200:
                    raise RemoteError(r.status, await r.json(content_type=None))
                buffer = b""
                async for chunk in r.content.iter_any():
                    events, buffer = parse_sse(buffer + chunk)
                    for event in events:
                        yield event


class P2clawTransport:
    """``agentd serve`` on another box, via p2claw's private route ``app``."""

    def __init__(self, peer: str, app: str = "agentd", client: Any = None, timeout: float = 3600):
        self.peer, self.app, self.timeout = peer, app, timeout
        if client is None:
            try:
                from p2claw_agent_client import AgentClient
            except ImportError:
                raise RuntimeError("reaching other boxes needs p2claw's client: pip install p2claw-agent-client") from None
            client = AgentClient()
        self.client = client

    async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        r = await asyncio.to_thread(self.client.fetch, self.peer, self.app, path, method=method,
                                    json_body=body, timeout=self.timeout)
        try:
            return r.status, r.json()
        except ValueError:
            return r.status, {"error": {"message": r.text()[:500]}}

    async def stream(self, method: str, path: str, body: Any = None) -> AsyncIterator[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        holder: dict[str, Any] = {}

        def pump() -> None:  # the SDK is synchronous: read in a thread
            try:
                r = self.client.fetch(self.peer, self.app, path, method=method, json_body=body,
                                      timeout=self.timeout, stream=True)
                holder["r"] = r
                if r.status != 200:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", (r.status, r.read())))
                    return
                for chunk in r.iter_chunks():
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("data", chunk))
            except Exception as e:  # noqa: BLE001 -- surfaced to the caller
                loop.call_soon_threadsafe(queue.put_nowait, ("exc", e))
            finally:
                if "r" in holder:
                    holder["r"].close()
                loop.call_soon_threadsafe(queue.put_nowait, ("end", None))

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        buffer = b""
        try:
            while True:
                kind, value = await queue.get()
                if kind == "data":
                    events, buffer = parse_sse(buffer + value)
                    for event in events:
                        yield event
                elif kind == "error":
                    status, raw = value
                    try:
                        raise RemoteError(status, json.loads(raw))
                    except ValueError:
                        raise RemoteError(status, raw.decode(errors="replace")) from None
                elif kind == "exc":
                    raise value
                else:
                    return
        finally:
            stop.set()  # hanging up closes the connection (a tied turn is then cancelled)
            if "r" in holder:
                holder["r"].close()


@dataclass
class Box:
    """One box's ``agentd serve``."""

    name: str
    transport: Transport
    drive: bool = False   # whether fleet skills may start, send, schedule or cancel here

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        status, data = await self.transport.request(method, path, body)
        if status >= 400:
            raise RemoteError(status, data)
        return data

    async def info(self) -> dict[str, Any]:
        return await self._call("GET", "/v1/info")

    async def harnesses(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Per harness: ready (and why not), default model and models."""
        return (await self._call("GET", "/v1/harnesses" + ("?refresh=1" if refresh else "")))["data"]

    async def models(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Every model a ready harness can run there, with those harnesses."""
        return (await self._call("GET", "/v1/models" + ("?refresh=1" if refresh else "")))["data"]

    async def sessions(self, *, closed: bool = False) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/sessions" + ("?all=1" if closed else "")))["data"]

    async def session(self, session_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/sessions/{session_id}")

    async def transcript(self, session_id: str, *, cursor: str | None = None, limit: int = 50,
                         order: str = "asc", format: str = "messages") -> dict[str, Any]:
        q = f"?limit={limit}&order={order}&format={format}" + (f"&cursor={cursor}" if cursor else "")
        return await self._call("GET", f"/v1/sessions/{session_id}/transcript{q}")

    async def legacy_sessions(self) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/legacy/claude-code"))["data"]

    async def legacy_transcript(self, session_id: str, *, cursor: str | None = None, limit: int = 50,
                                order: str = "asc") -> dict[str, Any]:
        q = f"?limit={limit}&order={order}" + (f"&cursor={cursor}" if cursor else "")
        return await self._call("GET", f"/v1/legacy/claude-code/{session_id}{q}")

    @staticmethod
    def _turn(prompt: Any, **fields: Any) -> dict[str, Any]:
        return {"input": prompt, **{k: v for k, v in fields.items() if v is not None}}

    async def start(self, prompt: Any, *, harness: str | None = None, model: str | None = None,
                    workspace: str | None = None, instructions: str | None = None,
                    background: bool = False) -> dict[str, Any]:
        """A new session's first turn; returns the response (``agentd.session`` is the session id)."""
        return await self._call("POST", "/v1/responses", self._turn(
            prompt, harness=harness, model=model, workspace=workspace, instructions=instructions,
            background=background or None))

    async def send(self, session_id: str, prompt: Any, *, instructions: str | None = None,
                   harness: str | None = None, model: str | None = None,
                   background: bool = False) -> dict[str, Any]:
        return await self._call("POST", "/v1/responses", self._turn(
            prompt, session_id=session_id, instructions=instructions, harness=harness, model=model,
            background=background or None))

    def stream(self, prompt: Any, *, session_id: str | None = None, **fields: Any) -> AsyncIterator[dict[str, Any]]:
        """A turn's Responses events, as they happen (hanging up cancels the turn)."""
        return self.transport.stream("POST", "/v1/responses",
                                     self._turn(prompt, session_id=session_id, stream=True, **fields))

    async def response(self, response_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/responses/{response_id}")

    def attach(self, response_id: str, starting_after: int = -1) -> AsyncIterator[dict[str, Any]]:
        """Events of a running (or finished) turn after ``starting_after``."""
        return self.transport.stream("GET", f"/v1/responses/{response_id}?stream=true&starting_after={starting_after}")

    async def cancel(self, response_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/v1/responses/{response_id}/cancel")

    async def close(self, session_id: str) -> dict[str, Any]:
        return await self._call("DELETE", f"/v1/sessions/{session_id}")

    async def schedule(self, prompt: Any, *, every: str | None = None, at: Any = None,
                       session_id: str | None = None, timezone: str | None = None,
                       instructions: str | None = None, **target: Any) -> dict[str, Any]:
        return await self._call("POST", "/v1/schedules", self._turn(
            prompt, every=every, at=at, session_id=session_id, timezone=timezone,
            instructions=instructions, **target))

    async def schedules(self) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/schedules"))["data"]

    async def unschedule(self, schedule_id: str) -> dict[str, Any]:
        return await self._call("DELETE", f"/v1/schedules/{schedule_id}")


def output_text(response: dict[str, Any]) -> str:
    """The assistant text of a response."""
    parts = []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            parts += [c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"]
    return "\n\n".join(p for p in parts if p)


@dataclass
class Fleet:
    boxes: dict[str, Box] = field(default_factory=dict)

    @classmethod
    def from_config(cls, path: str | Path | None = None, *, p2claw_client: Any = None) -> "Fleet":
        path = Path(path or FLEET_CONFIG).expanduser()
        config = json.loads(path.read_text()) if path.is_file() else {"boxes": {"local": {"local": True, "drive": True}}}
        boxes = {}
        for name, spec in config.get("boxes", {}).items():
            if spec.get("local"):
                transport: Transport = LocalTransport(spec.get("socket"))
            elif spec.get("peer"):
                transport = P2clawTransport(spec["peer"], spec.get("app", "agentd"), client=p2claw_client)
            else:
                raise ValueError(f"fleet box {name!r} needs \"local\": true or a \"peer\"")
            boxes[name] = Box(name, transport, drive=bool(spec.get("drive")))
        return cls(boxes)

    def box(self, name: str) -> Box:
        if name not in self.boxes:
            raise KeyError(f"unknown box {name!r}; configured: {', '.join(self.boxes) or 'none'}")
        return self.boxes[name]


# --------------------------------------------------------------------------- #
# Fleet skills: host-side @tool functions (agents see them as skills)
# --------------------------------------------------------------------------- #

_FLEET: Fleet | None = None


def enable_fleet_skills(fleet: Fleet | None = None) -> Fleet:
    """Register the ``fleet_*`` tools for ``fleet`` (default: ``~/.agentd/fleet.json``).

    They run on the host, through agentd's MCP bridge, so the p2claw socket
    never enters a sandbox. Boxes without ``"drive": true`` are read-only."""
    global _FLEET
    _FLEET = fleet or Fleet.from_config()
    from agentd import remote_skills

    remote_skills.register()
    return _FLEET


def disable_fleet_skills() -> None:
    global _FLEET
    from agentd import remote_skills

    remote_skills.unregister()
    _FLEET = None


def fleet() -> Fleet:
    if _FLEET is None:
        raise RuntimeError("fleet skills aren't enabled (agentd.remote.enable_fleet_skills)")
    return _FLEET
