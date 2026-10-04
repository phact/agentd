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

Values come from fnox on every call (``fnox get``, non-interactive), so fnox's
daemon is the only cache and a value changed in the vault is picked up at
once. A secret whose vault is locked raises :class:`agentd.fnox.SecretMissing`
(see :mod:`agentd.fnox` for unlocking). Everything the bridge returns to a
sandbox is scrubbed of every value read here (:func:`scrub`), so a tool that
echoes its own credential can't leak it.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from agentd import fnox as _fnox
from agentd.fnox import SecretMissing  # noqa: F401  (raised by secret())

_lock = threading.Lock()
_seen: set[str] = set()  # values read in this process, only for scrubbing


def secret(name: str, *, cwd: str | Path | None = None, profile: str | None = None, fnox: str = "fnox") -> str:
    """One secret's value from fnox, read now (:class:`SecretMissing` if its vault is locked)."""
    value = _fnox.get(name, Path(cwd or os.getcwd()).resolve(), fnox=fnox, profile=profile)
    remember(value)
    return value


def secret_env(names: list[str], **kwargs: Any) -> dict[str, str]:
    """``{NAME: value}`` for a host-side process's environment (e.g. an MCP server)."""
    return {name: secret(name, **kwargs) for name in names}


def remember(value: str) -> None:
    """Treat ``value`` as a secret for scrubbing (e.g. one obtained elsewhere)."""
    if value:
        with _lock:
            _seen.add(value)


def known_values() -> list[str]:
    with _lock:
        return sorted({v for v in _seen if len(v) >= 4}, key=len, reverse=True)


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
    """Forget every value read so far (e.g. in tests)."""
    with _lock:
        _seen.clear()
