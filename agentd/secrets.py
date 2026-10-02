"""Secrets for host-side tools, from fnox; never handed to a sandbox.

Host-side code (``@tool`` functions, MCP servers behind agentd's bridge)
sometimes needs a credential the sandbox must never see:

    from agentd.secrets import secret, secret_env

    @tool
    def query_db(sql: str) -> list:
        conn = psycopg.connect(secret("DATABASE_URL"))   # resolved on the host
        ...

    MCPServerStdio(params={"command": "github-mcp-server", "args": ["stdio"],
                           "env": secret_env(["GITHUB_TOKEN"])})

Values come from fnox (``fnox get``, non-interactive) and stay in this
process's memory. Everything the bridge returns to a sandbox is scrubbed of
every value resolved here (:func:`scrub`), so a tool that echoes its own
credential can't leak it.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from agentd.egress.policy import fnox_get

_lock = threading.Lock()
_values: dict[tuple[str, str, str | None], str] = {}


def secret(name: str, *, cwd: str | Path | None = None, profile: str | None = None, fnox: str = "fnox") -> str:
    """One secret's value from fnox (cached for this process)."""
    key = (name, str(Path(cwd or os.getcwd()).resolve()), profile)
    with _lock:
        if key in _values:
            return _values[key]
    value = fnox_get(name, Path(key[1]), fnox=fnox, profile=profile)
    with _lock:
        _values[key] = value
    return value


def secret_env(names: list[str], **kwargs: Any) -> dict[str, str]:
    """``{NAME: value}`` for a host-side process's environment (e.g. an MCP server)."""
    return {name: secret(name, **kwargs) for name in names}


def remember(value: str) -> None:
    """Treat ``value`` as a secret for scrubbing (e.g. one obtained elsewhere)."""
    if value:
        with _lock:
            _values[("_remembered", value[:8], None)] = value


def known_values() -> list[str]:
    with _lock:
        return sorted({v for v in _values.values() if len(v) >= 4}, key=len, reverse=True)


def scrub(obj: Any) -> Any:
    """``obj`` with every known secret value replaced (strings, lists, dicts)."""
    values = known_values()
    if not values:
        return obj

    def walk(x: Any) -> Any:
        if isinstance(x, str):
            for v in values:
                if v in x:
                    x = x.replace(v, "[secret redacted by agentd]")
            return x
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, tuple):
            return tuple(walk(i) for i in x)
        if isinstance(x, dict):
            return {walk(k): walk(v) for k, v in x.items()}
        return x

    return walk(obj)


def forget() -> None:
    """Drop every cached value (e.g. in tests)."""
    with _lock:
        _values.clear()
