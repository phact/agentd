"""``agentd serve``: this box's sandboxed agent sessions over HTTP on Unix sockets.

Two sockets, never TCP (see docs/agentd-serve.md):

  * ``serve.sock`` for local callers: its permissions are the gate.
  * ``peers.sock`` for p2claw's private route: every request must carry the
    caller identity header (``X-P2claw-Peer``), used for ownership.

Turns use the OpenAI Responses API (agentd.harness.responses), extended with
session, schedule and legacy-transcript endpoints.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aiohttp import web

from agentd.serve import legacy
from agentd.serve.config import HARNESSES, ServeConfig
from agentd.serve.paging import page_jsonl, page_list
from agentd.serve.pool import SandboxPool
from agentd.serve.store import Session, Store, new_id
from agentd.serve.turns import Turn, Turns

logger = logging.getLogger(__name__)

LOCAL = "local"


class HTTPError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.kind = status, message, kind


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("agentd")
    except Exception:
        return "unknown"


def _input_title(input_data: Any) -> str | None:
    if isinstance(input_data, str):
        return input_data.strip()[:120] or None
    for item in input_data or []:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()[:120]
        if isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
            if text:
                return text[:120]
    return None


class Server:
    def __init__(self, config: ServeConfig, *, pool: SandboxPool | None = None):
        self.config = config
        self.store = Store(config.dir)
        self.approvals = config.make_approvals()
        self.pool = pool or SandboxPool(config, self.approvals)
        self.turns = Turns()
        self.running: dict[str, Turn] = {}   # session id -> its running turn
        self.schedules = None                # agentd.serve.schedules.Scheduler, set by start()
        self._runners: list[web.AppRunner] = []

    # ------------------------------------------------------------------ #
    # Permissions
    # ------------------------------------------------------------------ #

    def can_drive(self, who: str, session: Session) -> bool:
        return who == LOCAL or who == session.owner or who in self.config.drivers

    def _drivable(self, who: str, session: Session) -> None:
        if not self.can_drive(who, session):
            raise HTTPError(403, f"session {session.id} belongs to {session.owner}; you can read it but not drive it",
                            "permission_error")

    def _session(self, session_id: str) -> Session:
        session = self.store.get(session_id)
        if session is None:
            raise HTTPError(404, f"no session {session_id}", "not_found_error")
        return session

    # ------------------------------------------------------------------ #
    # Turns
    # ------------------------------------------------------------------ #

    def _resolve_turn(self, who: str, body: dict[str, Any]) -> tuple[Session, dict[str, Any]]:
        """The session a turn runs in (new or existing) and the harness call's kwargs."""
        if body.get("tools"):
            raise HTTPError(400, "client-side tools aren't supported; harnesses get tools as skills")
        if "input" not in body:
            raise HTTPError(400, "input is required")
        harness = body.get("harness")
        if harness is not None and harness not in HARNESSES:
            raise HTTPError(400, f"harness must be one of {HARNESSES}")
        previous = body.get("previous_response_id")
        session = None
        if body.get("session_id"):
            session = self._session(body["session_id"])
        elif previous:
            session = self.store.session_for_response(previous)
        if session is not None:
            self._drivable(who, session)
            if session.closed:
                raise HTTPError(409, f"session {session.id} is closed")
            if body.get("workspace") and Path(self.config.resolve_workspace(body["workspace"], session.id)) != Path(session.workspace):
                raise HTTPError(400, "a session's workspace can't change")
        else:
            sid = new_id("ses")
            try:
                workspace = self.config.resolve_workspace(body.get("workspace"), sid)
            except ValueError as e:
                raise HTTPError(400, str(e)) from None
            session = Session(id=sid, owner=who, harness=harness or self.config.default_harness,
                              workspace=str(workspace), model=body.get("model"), image=body.get("image"),
                              title=_input_title(body["input"]))
        kwargs = {}
        if body.get("instructions") is not None:
            kwargs["instructions"] = body["instructions"]
        prev = previous or session.last_response_id
        if prev:
            kwargs["previous_response_id"] = prev
        return session, kwargs

    def start_turn(self, who: str, body: dict[str, Any], *, background: bool) -> Turn:
        session, kwargs = self._resolve_turn(who, body)
        if session.id in self.running:
            raise HTTPError(409, f"session {session.id} already has a turn running "
                                 f"({self.running[session.id].response_id})", "conflict_error")
        if self.store.get(session.id) is None:
            self.store.add(session)
        harness = body.get("harness") or session.harness
        model = body.get("model") or session.model
        return self._run(session, who, harness, model, body["input"], kwargs, background)

    def _run(self, session: Session, who: str, harness: str, model: str | None, input_data: Any,
             kwargs: dict[str, Any], background: bool) -> Turn:
        from agentd.harness import responses as harness_responses

        entry = self.pool.acquire(Path(session.workspace), session.image)
        turn = Turn(session.id, who, background)
        self.running[session.id] = turn
        events = harness_responses.stream_response(
            client_obj=entry.client, harness_name=harness, model=model, input_data=input_data, kwargs=kwargs,
            mcp_servers=None, cwd=Path(session.workspace), executor=entry.executor, server_cache={},
            bridge_cache={}, skills_override=None,
        )

        def on_event(turn: Turn, data: dict) -> None:
            response = data.get("response")
            if isinstance(response, dict):
                response.setdefault("agentd", {})["session"] = session.id
                if turn.response_id is None:
                    self.store.link_response(response["id"], session.id)

        def on_done(turn: Turn) -> None:
            self.pool.release(entry)
            if self.running.get(session.id) is turn:
                del self.running[session.id]
            final = turn.final
            if final is None:
                return
            self.store.save_response(final)
            session.last_activity = time.time()
            if final.get("status") == "completed":
                session.last_response_id = final["id"]
                session.harness = harness
                if model:
                    session.model = model
                native = (final.get("agentd") or {}).get("session_id")
                if native:
                    session.native_session_id = native
            if session.title is None:
                session.title = _input_title(input_data)
            self.store.save()

        self.turns.start(turn, events, on_event=on_event, on_done=on_done)
        return turn

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def app(self, *, peers: bool) -> web.Application:
        header = self.config.identity_header

        @web.middleware
        async def identity(request: web.Request, handler):
            if peers:
                who = (request.headers.get(header) or "").strip()
                if not who:
                    logger.warning("agentd serve: refused %s %s on the peers socket: no %s header",
                                   request.method, request.path, header)
                    return _error(403, f"missing {header}", "permission_error")
                request["who"] = who
            else:
                request["who"] = LOCAL
            try:
                return await handler(request)
            except HTTPError as e:
                return _error(e.status, e.message, e.kind)
            except (ValueError, json.JSONDecodeError) as e:
                return _error(400, str(e))

        app = web.Application(middlewares=[identity], client_max_size=32 * 1024 * 1024)
        r = app.router
        r.add_get("/v1/info", self.h_info)
        r.add_get("/v1/harnesses", self.h_harnesses)
        r.add_get("/v1/models", self.h_models)
        r.add_post("/v1/responses", self.h_create_response)
        r.add_get("/v1/responses/{id}", self.h_get_response)
        r.add_post("/v1/responses/{id}/cancel", self.h_cancel_response)
        r.add_get("/v1/sessions", self.h_sessions)
        r.add_get("/v1/sessions/{id}", self.h_session)
        r.add_get("/v1/sessions/{id}/transcript", self.h_transcript)
        r.add_delete("/v1/sessions/{id}", self.h_close_session)
        r.add_post("/v1/schedules", self.h_create_schedule)
        r.add_get("/v1/schedules", self.h_schedules)
        r.add_get("/v1/schedules/{id}", self.h_schedule)
        r.add_delete("/v1/schedules/{id}", self.h_delete_schedule)
        r.add_get("/v1/approvals", self.h_approvals)
        r.add_get("/v1/approvals/{id}", self.h_approval)
        r.add_post("/v1/approvals/{id}", self.h_decide)
        r.add_get("/v1/legacy/claude-code", self.h_legacy_list)
        r.add_get("/v1/legacy/claude-code/{id}", self.h_legacy_transcript)
        return app

    async def h_info(self, request: web.Request) -> web.Response:
        from agentd.sandbox.base import DEFAULT_HOME

        rootfs = DEFAULT_HOME / "rootfs"
        images = sorted(p.name for p in rootfs.iterdir() if p.is_dir()) if rootfs.is_dir() else []
        return web.json_response({
            "box": self.config.box_name,
            "agentd_version": _version(),
            "harnesses": list(HARNESSES),
            "ready": {name: st.ready for name, st in (await self.available()).items() if name in HARNESSES},
            "default_harness": self.config.default_harness,
            "sandbox": {k: v for k, v in self.config.sandbox.items() if k in ("backend", "image", "cpus", "mem_mib")},
            "images": images,
            "workspace_roots": [str(r) for r in self.config.workspace_roots],
            "you": request["who"],
        })

    async def available(self, refresh: bool = False):
        from agentd.availability import available_async

        return await available_async(self.config.sandbox_target(),
                                     harness_options=self.config.harness_options, refresh=refresh)

    async def h_harnesses(self, request: web.Request) -> web.Response:
        """Per harness: ready (and why not), default model and models."""
        status = await self.available(request.query.get("refresh") in ("1", "true"))
        return web.json_response({"data": [status[h].to_dict() for h in HARNESSES if h in status]})

    async def h_models(self, request: web.Request) -> web.Response:
        """OpenAI-style model list: every model a ready harness can run, with those harnesses."""
        status = await self.available(request.query.get("refresh") in ("1", "true"))
        models: dict[str, dict[str, Any]] = {}
        for name in HARNESSES:
            st = status.get(name)
            if st is None or not st.ready:
                continue
            for m in st.models:
                entry = models.setdefault(m.id, {"id": m.id, "object": "model", "created": 0, "owned_by": m.source,
                                                 "name": m.name, "harnesses": []})
                entry["harnesses"].append(name)
        return web.json_response({"object": "list", "data": list(models.values())})

    async def h_create_response(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        who = request["who"]
        background = bool(body.get("background"))
        turn = self.start_turn(who, body, background=background)
        await turn.started.wait()
        if body.get("stream"):
            return await self._sse(request, turn, after=-1, cancel_on_disconnect=not background)
        if background:
            return web.json_response(turn.snapshot)
        try:
            await turn.done.wait()
        except asyncio.CancelledError:
            turn.cancel()  # the caller went away
            raise
        return web.json_response(turn.final)

    async def _sse(self, request: web.Request, turn: Turn, *, after: int,
                   cancel_on_disconnect: bool) -> web.StreamResponse:
        response = web.StreamResponse(headers={"content-type": "text/event-stream", "cache-control": "no-cache"})
        await response.prepare(request)
        finished = False
        try:
            async for event in turn.follow(after):
                await response.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            finished = True
            await response.write_eof()
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        finally:
            if not finished and cancel_on_disconnect:
                turn.cancel()
        return response

    async def h_get_response(self, request: web.Request) -> web.StreamResponse:
        rid = request.match_info["id"]
        turn = self.turns.get(rid)
        stream = request.query.get("stream") in ("1", "true")
        if turn is not None:
            if stream:
                after = int(request.query.get("starting_after", "-1"))
                return await self._sse(request, turn, after=after, cancel_on_disconnect=False)
            return web.json_response(turn.snapshot)
        final = self.store.load_response(rid)
        if final is None:
            raise HTTPError(404, f"no response {rid}", "not_found_error")
        if stream:
            done = Turn(final.get("agentd", {}).get("session", ""), "", True)
            await done._add({"type": "response.completed" if final.get("status") == "completed" else "response.failed",
                             "response": final, "sequence_number": 0})
            await done._finish()
            return await self._sse(request, done, after=int(request.query.get("starting_after", "-1")),
                                   cancel_on_disconnect=False)
        return web.json_response(final)

    async def h_cancel_response(self, request: web.Request) -> web.Response:
        rid = request.match_info["id"]
        turn = self.turns.get(rid)
        if turn is None:
            final = self.store.load_response(rid)
            if final is None:
                raise HTTPError(404, f"no response {rid}", "not_found_error")
            return web.json_response(final)  # already finished
        self._drivable(request["who"], self._session(turn.session_id))
        turn.cancel()
        await turn.done.wait()
        return web.json_response(turn.final)

    def _session_view(self, session: Session) -> dict[str, Any]:
        running = self.running.get(session.id)
        return {**session.public(),
                "sandbox": self.pool.state(session.workspace, session.image),
                "running_response_id": running.response_id if running else None}

    async def h_sessions(self, request: web.Request) -> web.Response:
        everything = request.query.get("all") in ("1", "true")
        sessions = sorted((s for s in self.store.sessions.values() if everything or not s.closed),
                          key=lambda s: s.last_activity, reverse=True)
        return web.json_response({"data": [self._session_view(s) for s in sessions]})

    async def h_session(self, request: web.Request) -> web.Response:
        return web.json_response(self._session_view(self._session(request.match_info["id"])))

    async def h_close_session(self, request: web.Request) -> web.Response:
        session = self._session(request.match_info["id"])
        self._drivable(request["who"], session)
        turn = self.running.get(session.id)
        if turn is not None:
            turn.cancel()
            await turn.done.wait()
        session.closed = True
        self.store.save()
        if self.schedules is not None:
            self.schedules.drop_session(session.id)
        entry = self.pool.entries.get((session.workspace, session.image))
        others = any(s.workspace == session.workspace and s.image == session.image and not s.closed
                     and s.id in self.running for s in self.store.sessions.values())
        if entry is not None and not entry.active and not others:
            await self.pool.stop(entry)
        return web.json_response(self._session_view(session))

    async def h_transcript(self, request: web.Request) -> web.Response:
        from agentd.harness import responses as harness_responses
        from agentd.harness import transcripts

        session = self._session(request.match_info["id"])
        q = request.query
        fmt = q.get("format", "messages")
        paging = dict(cursor=q.get("cursor"), limit=int(q.get("limit", "50")), order=q.get("order", "asc"))
        if fmt == "messages":
            record = harness_responses._load(SimpleNamespace(), session.last_response_id) \
                if session.last_response_id else None
            messages = (record or {}).get("messages", [])
            page, nxt = page_list(messages, **paging)
        elif fmt == "raw":
            if not session.native_session_id:
                page, nxt = [], None
            else:
                store = transcripts.store_dir(session.harness, session.workspace, self.config.transcripts_root)
                files = [f for f in transcripts.session_files(session.harness, store, session.native_session_id)
                         if f.suffix == ".jsonl"]
                page, nxt = page_jsonl(files[0], **paging) if files else ([], None)
        else:
            raise HTTPError(400, "format must be 'messages' or 'raw'")
        return web.json_response({"data": page, "next_cursor": nxt, "format": fmt})

    # Schedules (agentd.serve.schedules) ------------------------------------

    def _scheduler(self):
        if self.schedules is None:
            raise HTTPError(503, "the scheduler isn't running")
        return self.schedules

    async def h_create_schedule(self, request: web.Request) -> web.Response:
        schedule = self._scheduler().create(request["who"], await request.json())
        return web.json_response(self._scheduler().view(schedule))

    async def h_schedules(self, request: web.Request) -> web.Response:
        s = self._scheduler()
        return web.json_response({"data": [s.view(x) for x in s.all()]})

    async def h_schedule(self, request: web.Request) -> web.Response:
        s = self._scheduler()
        return web.json_response(s.view(s.get(request.match_info["id"])))

    async def h_delete_schedule(self, request: web.Request) -> web.Response:
        s = self._scheduler()
        return web.json_response(s.view(s.delete(request["who"], request.match_info["id"])))

    # Approvals (agentd.egress.approvals) -----------------------------------

    def _approvals(self):
        if self.approvals is None:
            raise HTTPError(404, "approvals aren't configured on this box", "not_found_error")
        return self.approvals

    async def h_approvals(self, request: web.Request) -> web.Response:
        pending = request.query.get("pending") in ("1", "true")
        return web.json_response({"data": [a.public() for a in self._approvals().list(pending_only=pending)]})

    async def h_approval(self, request: web.Request) -> web.Response:
        a = self._approvals().items.get(request.match_info["id"])
        if a is None:
            raise HTTPError(404, f"no approval {request.match_info['id']}", "not_found_error")
        return web.json_response(a.public())

    async def h_decide(self, request: web.Request) -> web.Response:
        who = request["who"]
        if who != LOCAL and who not in self.config.approvers:
            raise HTTPError(403, "only local callers and configured approvers can decide approvals",
                            "permission_error")
        body = await request.json()
        try:
            a = self._approvals().decide(request.match_info["id"], str(body.get("decision")), by=who)
        except KeyError:
            raise HTTPError(404, f"no approval {request.match_info['id']}", "not_found_error") from None
        return web.json_response(a.public())

    # Legacy Claude Code sessions -------------------------------------------

    def _legacy_exclude(self) -> set[str]:
        return legacy.agentd_claude_ids(self.config.transcripts_root, self.store.native_ids())

    async def h_legacy_list(self, request: web.Request) -> web.Response:
        q = request.query
        sessions = await asyncio.to_thread(legacy.list_sessions, self.config.claude_projects, self._legacy_exclude())
        page, nxt = page_list(sessions, cursor=q.get("cursor"), limit=int(q.get("limit", "100")))
        return web.json_response({"data": page, "next_cursor": nxt})

    async def h_legacy_transcript(self, request: web.Request) -> web.Response:
        q = request.query
        path = legacy.find_session(self.config.claude_projects, request.match_info["id"], self._legacy_exclude())
        if path is None:
            raise HTTPError(404, f"no legacy Claude Code session {request.match_info['id']}", "not_found_error")
        page, nxt = await asyncio.to_thread(page_jsonl, path, cursor=q.get("cursor"),
                                            limit=int(q.get("limit", "50")), order=q.get("order", "asc"))
        return web.json_response({"data": page, "next_cursor": nxt})

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Listen on both sockets (in a 0700 directory) and start background work."""
        from agentd.serve.schedules import Scheduler

        d = self.config.dir
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        for path, peers in ((self.config.serve_socket, False), (self.config.peers_socket, True)):
            runner = web.AppRunner(self.app(peers=peers), handler_cancellation=True, access_log=None)
            await runner.setup()
            path.unlink(missing_ok=True)
            old = os.umask(0o177)  # sockets are created 0600
            try:
                await web.UnixSite(runner, str(path)).start()
            finally:
                os.umask(old)
            os.chmod(path, 0o600)
            self._runners.append(runner)
        self.pool.start()
        self.schedules = Scheduler(self)
        self.schedules.start()
        logger.info("agentd serve: listening on %s (local) and %s (peers)",
                    self.config.serve_socket, self.config.peers_socket)

    async def stop(self) -> None:
        if self.schedules is not None:
            self.schedules.stop()
        for turn in list(self.running.values()):
            turn.cancel()
        for turn in list(self.running.values()):
            await turn.done.wait()
        for runner in self._runners:
            await runner.cleanup()
        self._runners.clear()
        for path in (self.config.serve_socket, self.config.peers_socket):
            path.unlink(missing_ok=True)
        await self.pool.close()


def _error(status: int, message: str, kind: str = "invalid_request_error") -> web.Response:
    return web.json_response({"error": {"message": message, "type": kind}}, status=status)
