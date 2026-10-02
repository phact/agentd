"""The host side of an agentd sandbox, independent of how it is isolated.

A sandbox runs ``sandboxd`` (agentd's helper) in an isolated environment with
no network, and the host holds the one connection to it, multiplexed (see
``mux``). Everything goes over that connection:

  * ``exec()`` / ``shell()`` run commands (``shell`` keeps state across calls).
  * ``endpoints`` are sandbox-side sockets. A connection to one becomes a
    stream to the host, which connects it to that endpoint's host target;
    there is no other way out. The host opened the connection, so it knows
    which sandbox every stream came from without tokens.

Backends differ only in how they start that environment and reach sandboxd:

  * :class:`~agentd.sandbox.krun.KrunSandbox`    libkrun microVM, vsock
  * :class:`~agentd.sandbox.docker.DockerSandbox` container, ``docker run -i`` stdio

Shared host directories (the workspace, ``mounts``) appear at the same
absolute path inside the sandbox as on the host, so paths, and tools that key
state by cwd (e.g. Claude Code's transcripts), line up on both sides.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentd.sandbox.mux import Mux, MuxClosed, Stream, splice

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SANDBOX_FILES = ("mux.py", "sandboxd.py")  # copied/injected into every sandbox at /agentd

# Sandbox paths a host directory may not be mounted over.
_RESERVED = {"", "agentd", "bin", "boot", "dev", "etc", "lib", "lib64", "proc", "run", "sbin", "sys", "usr"}

# agentd's runtime state (sockets, sessions, base images, CA, transcripts).
# macOS limits AF_UNIX paths to 104 bytes, so keep it short.
DEFAULT_HOME = Path(os.environ.get("AGENTD_HOME", Path.home() / ".agentd"))


def protected_host_paths() -> list[Path]:
    """Host paths no sandbox may see: agentd's host-side sockets (MCP bridge,
    model proxies that add credentials, the serve API) and its CA key, plus the
    host logins those proxies read. A share that is, contains or sits inside
    one of these is refused (e.g. mounting ``~`` read-only)."""
    home = Path.home()
    paths = [DEFAULT_HOME / "run", DEFAULT_HOME / "ca", DEFAULT_HOME / "serve",
             Path(os.environ.get("CODEX_HOME", home / ".codex")) / "auth.json",
             home / ".claude" / ".credentials.json"]
    return [p.expanduser().resolve() for p in paths]


@dataclass
class Endpoint:
    """A sandbox-side socket and the host target its connections go to.

    ``sandbox`` is ``("unix", "/run/agentd/bridge.sock")`` or
    ``("tcp", "127.0.0.1", 8080)``. ``host`` is ``("unix", path)`` or
    ``("tcp", host, port)``.
    """

    sandbox: tuple
    host: tuple


@dataclass(kw_only=True)
class Sandbox:
    # Shared at the same absolute path inside the sandbox.
    workspace: Path | None = None
    # Extra host directories to share, as {sandbox_path: host_path}. Use the
    # host path as the sandbox path unless a tool needs a fixed location.
    mounts: dict[str, Path] = field(default_factory=dict)
    # Host directories shared read-only, as {sandbox_path: host_path}. The
    # sandbox can read them but never change them (enforced by the backend).
    read_only_mounts: dict[str, Path] = field(default_factory=dict)
    endpoints: dict[str, Endpoint] = field(default_factory=dict)
    cpus: int = 2
    mem_mib: int = 1024
    env: dict[str, str] = field(default_factory=dict)
    # Non-root user that execs run as by default. The image creates it with
    # the host user's uid/gid (agentd.sandbox.rootfs) so shared files match.
    user: str | None = None
    home: Path = DEFAULT_HOME
    boot_timeout: float = 15.0

    backend = "base"

    def __post_init__(self) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.session_dir = self.home / "sessions" / self.id
        self._mux: Mux | None = None
        self._mux_task: asyncio.Task | None = None
        self.timings: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Backend hooks
    # ------------------------------------------------------------------ #

    async def _boot(self) -> None:
        """Start the environment and :meth:`_attach` to its sandboxd."""
        raise NotImplementedError

    def _setup_commands(self) -> list[str]:
        """Root shell commands run once sandboxd is up (e.g. mounting shares)."""
        return []

    def _expected_user_ids(self) -> tuple[int, int]:
        """The (uid, gid) shared files have inside the sandbox; the user must match."""
        return os.getuid(), os.getgid()

    async def _teardown(self) -> None:
        """Make sure the environment is gone (after sandboxd was asked to exit)."""

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> "Sandbox":
        try:
            return await self._start()
        except BaseException:
            await self.stop()  # tear down whatever came up, remove the session dir
            raise

    async def _start(self) -> "Sandbox":
        t0 = time.monotonic()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        for _, target, host, _ in self._shares():
            if not host.is_dir():
                raise FileNotFoundError(f"cannot share {host} at {target}: not a directory")
        await self._boot()
        self.timings["boot"] = time.monotonic() - t0

        setup = []
        if self.user is not None:
            uid, gid = self._expected_user_ids()
            setup.append(
                f'[ "$(id -u {self.user}):$(id -g {self.user})" = "{uid}:{gid}" ] || '
                f'{{ echo "sandbox user {self.user} is $(id -u {self.user}):$(id -g {self.user}), host user is {uid}:{gid};'
                f' rebuild the image with python -m agentd.sandbox.rootfs"; exit 1; }}'
            )
        setup += self._setup_commands()
        if setup:
            out, code = await self.exec(["/bin/sh", "-c", " && ".join(setup)], user="root")
            if code != 0:
                raise RuntimeError(f"sandbox setup failed: {out.decode(errors='replace')}")
        for name, ep in self.endpoints.items():
            await self._listen(name, ep)
        self.timings["ready"] = time.monotonic() - t0
        return self

    async def stop(self) -> None:
        if self._mux is not None and not self._mux.closed.is_set():
            try:
                stream = await self._mux.open_stream({"kind": "shutdown"})
                await asyncio.wait_for(stream.wait_closed(), 5)
            except (MuxClosed, asyncio.TimeoutError):
                pass
            await self._mux.aclose()
        await self._teardown()
        if self._mux_task is not None:
            self._mux_task.cancel()
        shutil.rmtree(self.session_dir, ignore_errors=True)

    async def __aenter__(self) -> "Sandbox":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    @property
    def workspace_path(self) -> str | None:
        """The workspace's path inside the sandbox (same as on the host)."""
        return str(Path(self.workspace).resolve()) if self.workspace is not None else None

    # ------------------------------------------------------------------ #
    # Operations
    # ------------------------------------------------------------------ #

    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
        user: str | None = None,
    ) -> tuple[bytes, int]:
        """Run ``argv`` in the sandbox; returns (stdout+stderr, exit code).

        Runs as ``self.user`` unless ``user`` is given (``"root"`` for root).
        """
        stream = await self.open_exec(argv, cwd=cwd, env=env, user=user)
        if stdin:
            await stream.write(stdin)
        await stream.write_eof()
        try:
            output = await asyncio.wait_for(stream.read_all(), timeout)
            meta = await asyncio.wait_for(stream.wait_closed(), timeout)
        except asyncio.TimeoutError:
            await stream.close({"signal": "timeout"})
            return b"timed out", 124
        meta = meta or {}
        if "exit" not in meta:
            raise RuntimeError(f"exec failed: {meta.get('error', meta)}")
        return output, int(meta["exit"])

    async def shell(
        self,
        command: str,
        *,
        session: str = "default",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        user: str | None = None,
        reset: bool = False,
    ) -> tuple[bytes, int, dict[str, Any]]:
        """Run ``command`` in a persistent shell session in the sandbox.

        State (cwd, exported env, functions, dir stack) carries over between
        calls with the same ``session``. ``env`` and ``user`` apply when the
        session is created. ``cwd`` moves the shell only when it differs from
        the ``cwd`` given on the previous call, so the command's own ``cd``
        sticks. Returns (output, exit code, info); ``info["restarted"]`` is
        set when a timeout or ``exit`` replaced the shell and state was lost.
        """
        user = user or self.user
        stream = await self._require_mux().open_stream({
            "kind": "shell", "session": session, "command": command, "cwd": cwd,
            "env": env or {}, "timeout": timeout, "reset": reset,
            "user": None if user == "root" else user,
        })
        await stream.write_eof()
        output = await stream.read_all()
        meta = await stream.wait_closed() or {}
        if "exit" not in meta:
            raise RuntimeError(f"shell failed: {meta.get('error', meta)}")
        return output, int(meta["exit"]), meta

    async def open_exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ) -> Stream:
        """Start ``argv`` and return its stream (write = stdin, read = output)."""
        user = user or self.user
        return await self._require_mux().open_stream({
            "kind": "exec", "argv": argv, "cwd": cwd, "env": env or {},
            "user": None if user == "root" else user,
        })

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float) -> None:
        """Run the mux over the host's connection to sandboxd; wait for HELLO."""
        mux = Mux(reader, writer, is_host=True, on_open=self._on_sandbox_stream)
        task = asyncio.ensure_future(mux.run())
        try:
            hello = await asyncio.wait_for(mux.wait_hello(), timeout)
        except BaseException:
            task.cancel()
            raise
        self._mux, self._mux_task = mux, task
        logger.debug("sandbox %s (%s) ready: %s", self.id, self.backend, hello)

    async def add_endpoint(self, name: str, ep: Endpoint) -> None:
        """Add a sandbox-side endpoint to a running sandbox."""
        if name in self.endpoints:
            raise ValueError(f"endpoint {name!r} already exists")
        self.endpoints[name] = ep
        try:
            await self._listen(name, ep)
        except BaseException:
            del self.endpoints[name]
            raise

    async def _listen(self, name: str, ep: Endpoint) -> None:
        kind, *addr = ep.sandbox
        meta: dict[str, Any] = {"kind": "listen", "name": name}
        if kind == "unix":
            meta["unix"] = addr[0]
        else:
            meta["tcp"] = [addr[0], int(addr[1])]
        stream = await self._require_mux().open_stream(meta)
        result = await stream.wait_closed() or {}
        if not result.get("ok"):
            raise RuntimeError(f"endpoint {name!r} failed: {result}")

    async def _on_sandbox_stream(self, stream: Stream) -> None:
        """A sandbox-side endpoint got a connection; route it on the host."""
        if stream.meta.get("kind") != "connect":
            await stream.close({"error": "sandbox may only open connect streams"})
            return
        ep = self.endpoints.get(stream.meta.get("name", ""))
        if ep is None:
            await stream.close({"error": "no such endpoint"})
            return
        kind, *addr = ep.host
        try:
            if kind == "unix":
                reader, writer = await asyncio.open_unix_connection(addr[0])
            else:
                reader, writer = await asyncio.open_connection(addr[0], int(addr[1]))
        except OSError as e:
            await stream.close({"error": f"host target unavailable: {e}"})
            return
        await splice(stream, reader, writer)

    def _shares(self) -> list[tuple[str, str, Path, bool]]:
        """(tag, sandbox path, host path, read-only) for every shared directory.

        Sorted by sandbox path so a parent is mounted before anything in it.
        """
        entries = []
        if self.workspace is not None:
            entries.append((self.workspace_path, Path(self.workspace).resolve(), False))
        entries += [(str(t), Path(h).expanduser().resolve(), False) for t, h in self.mounts.items()]
        entries += [(str(t), Path(h).expanduser().resolve(), True) for t, h in self.read_only_mounts.items()]
        targets = [t for t, _, _ in entries]
        if len(set(targets)) != len(targets):
            raise ValueError(f"two shared directories at the same sandbox path: {sorted(targets)}")
        shares = []
        protected = protected_host_paths()
        for i, (target, host, read_only) in enumerate(sorted(entries)):
            top = Path(target).parts[1] if len(Path(target).parts) > 1 else ""
            if not target.startswith("/") or top in _RESERVED:
                raise ValueError(f"cannot mount {host} over sandbox path {target!r}")
            for p in protected:
                if host == p or p.is_relative_to(host) or host.is_relative_to(p):
                    raise ValueError(f"cannot share {host} with a sandbox: it would expose {p} "
                                     "(agentd's sockets, CA key or host credentials)")
            shares.append((f"share{i}", target, host, read_only))
        return shares

    def _write_probes(self) -> dict[str, tuple[str, Path | None]]:
        """Something to look for in each share, to check the bind mount is real.

        (Docker Desktop / Colima silently show an empty directory for host
        paths not shared with their VM.) Used by the Docker and Colima backends. Writable shares get a marker file;
        read-only shares are never written to, so an existing entry is used
        instead (an empty read-only dir can't be checked and is skipped).

        Returns {sandbox path: (name to look for, marker to delete or None)}."""
        probes = {}
        for _, target, host, read_only in self._shares():
            if read_only:
                entry = next(iter(sorted(host.iterdir())), None)
                if entry is not None:
                    probes[target] = (entry.name, None)
            else:
                marker = host / f".agentd-probe-{self.id}"
                marker.touch()
                probes[target] = (marker.name, marker)
        return probes

    def _remove_markers(self) -> None:
        for _, marker in getattr(self, "_probes", {}).values():
            if marker is not None:
                marker.unlink(missing_ok=True)

    def _require_mux(self) -> Mux:
        if self._mux is None or self._mux.closed.is_set():
            raise MuxClosed(f"sandbox {self.id} is not connected")
        return self._mux

    def _console_tail(self) -> str:
        try:
            return (self.session_dir / "console.log").read_text(errors="replace")[-2000:]
        except OSError:
            return ""
