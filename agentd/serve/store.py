"""Persistent state of ``agentd serve``: sessions and final responses."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{os.urandom(10).hex()}"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@dataclass
class Session:
    """A conversation on this box. Durable; its sandbox comes and goes."""

    id: str
    owner: str                 # "local", or the caller's peer id
    harness: str
    workspace: str
    model: str | None = None
    image: str | None = None
    title: str | None = None   # the first prompt
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    last_response_id: str | None = None   # turns continue from here
    native_session_id: str | None = None  # the harness's own session id
    closed: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    """``sessions.json`` (sessions plus which session each response belongs to)
    and ``responses/<id>.json`` (final responses)."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.path = directory / "sessions.json"
        self.sessions: dict[str, Session] = {}
        self.response_session: dict[str, str] = {}
        if self.path.is_file():
            data = json.loads(self.path.read_text())
            self.sessions = {s["id"]: Session(**s) for s in data.get("sessions", [])}
            self.response_session = dict(data.get("responses", {}))

    def save(self) -> None:
        _atomic_write(self.path, json.dumps({
            "sessions": [s.public() for s in self.sessions.values()],
            "responses": self.response_session,
        }))

    def add(self, session: Session) -> None:
        self.sessions[session.id] = session
        self.save()

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def session_for_response(self, response_id: str) -> Session | None:
        sid = self.response_session.get(response_id)
        return self.sessions.get(sid) if sid else None

    def link_response(self, response_id: str, session_id: str) -> None:
        self.response_session[response_id] = session_id
        self.save()

    def native_ids(self) -> set[str]:
        return {s.native_session_id for s in self.sessions.values() if s.native_session_id}

    # Final responses -------------------------------------------------------

    def _response_path(self, response_id: str) -> Path | None:
        if "/" in response_id or response_id.startswith("."):
            return None
        return self.dir / "responses" / f"{response_id}.json"

    def save_response(self, response: dict[str, Any]) -> None:
        path = self._response_path(response["id"])
        if path is not None:
            _atomic_write(path, json.dumps(response))

    def load_response(self, response_id: str) -> dict[str, Any] | None:
        path = self._response_path(response_id)
        if path is None or not path.is_file():
            return None
        return json.loads(path.read_text())
