"""sandboxd: the agentd helper that runs inside a libkrun sandbox.

It is the sandbox's main process. The host holds its one connection: with
libkrun it listens on a vsock port the host dials; with Docker (``--stdio``)
the connection is its own stdin/stdout, held by the host's ``docker run -i``.
That connection is the sandbox's only channel to anything. Over it the host
can:

  * ``exec``     run a command, streaming stdin/stdout, returning the exit code
  * ``shell``    run a command in a named persistent bash session, so ``cd``,
                 ``export``, ``pushd``/``popd`` and functions carry over
  * ``listen``   create a sandbox-side endpoint (a Unix socket or loopback TCP
                 port). Every connection to it becomes a ``connect`` stream back
                 to the host, which decides where (if anywhere) it goes.
  * ``shutdown`` sync and exit, which powers the microVM off.

The sandbox has no network device and no TSI, so these endpoints are how the
skills CLI reaches the bridge and how a harness reaches the model API.

Stdlib only: this file and ``mux.py`` are copied into the sandbox rootfs.
"""
from __future__ import annotations

import asyncio
import os
import pwd
import secrets
import shlex
import signal
import socket
import sys


def _load_mux():
    # Load mux.py by path: agentd injects these files into libkrun virtual
    # directories, which cannot be listed, so a normal import (which scans
    # the directory) fails there.
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mux.py")
    spec = importlib.util.spec_from_file_location("mux", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mux"] = module
    spec.loader.exec_module(module)
    return module


_mux = _load_mux()
Mux, Stream = _mux.Mux, _mux.Stream
pipe_reader_to_stream, pipe_stream_to_writer, splice = (
    _mux.pipe_reader_to_stream, _mux.pipe_stream_to_writer, _mux.splice,
)

OVERLAY_BASE = "/agentd/mnt"


def enter_overlay_root() -> None:
    """Make the read-only shared root writable for this session, then chroot.

    A tmpfs upper layer over the read-only base: writes anywhere (even
    /usr) work and vanish with the VM, and the base image is never touched.
    Everything sandboxd starts afterwards inherits the new root.
    """
    import subprocess

    def run(*argv: str) -> None:
        subprocess.run(argv, check=True)

    run("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", OVERLAY_BASE)
    for d in ("upper", "work", "root"):
        os.mkdir(os.path.join(OVERLAY_BASE, d))
    root = os.path.join(OVERLAY_BASE, "root")
    run("mount", "-t", "overlay", "overlay", "-o",
        f"lowerdir=/,upperdir={OVERLAY_BASE}/upper,workdir={OVERLAY_BASE}/work", root)
    for d in ("proc", "sys", "dev"):
        run("mount", "--rbind", f"/{d}", os.path.join(root, d))
    os.chroot(root)
    os.chdir("/")

VERSION = 1


SHELL_DIR = "/run/agentd/shell"


def _user_drop(user: str | None, env: dict[str, str]) -> dict:
    """subprocess kwargs that drop root completely (uid, gid, groups)."""
    if not user:
        return {}
    pw = pwd.getpwnam(user)
    env.update({"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name})
    return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}


class ShellSession:
    """A persistent bash whose state (cwd, env, functions, dirs) spans commands.

    Each command is written to a file and sourced with stdin from /dev/null,
    so it cannot swallow later commands, and a syntax error returns an error
    instead of leaving the shell waiting for input. Completion is detected by
    a per-command random marker carrying ``$?``. If a command times out, or
    ends the shell (``exit``), the shell is replaced and the result says so.
    """

    def __init__(self, user: str | None, env: dict[str, str]):
        self.user = user
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self.last_cwd: str | None = None
        self.lock = asyncio.Lock()

    async def _start(self) -> None:
        env = dict(os.environ)
        drop = _user_drop(self.user, env)
        env.update(self.env)
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "--norc", "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=env.get("HOME", "/"),
            env=env,
            start_new_session=True,
            **drop,
        )
        self.last_cwd = None

    def _kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.proc = None

    async def run(self, stream: Stream, command: str, cwd: str | None, timeout: float | None) -> dict:
        async with self.lock:
            fresh = self.proc is None or self.proc.returncode is not None
            if fresh:
                await self._start()
            nonce = secrets.token_hex(8)
            marker = f"__AGENTD_DONE_{nonce}__".encode()
            script = os.path.join(SHELL_DIR, f"{nonce}.sh")
            with open(script, "w") as f:
                f.write(command + "\n")
            os.chmod(script, 0o600)  # before chown: no CAP_FOWNER needed (Docker backend)
            if self.user:
                pw = pwd.getpwnam(self.user)
                os.chown(script, pw.pw_uid, pw.pw_gid)

            prefix = ""
            if cwd and cwd != self.last_cwd:
                prefix = f"builtin cd -- {shlex.quote(cwd)} || builtin printf 'cd failed: %s\\n' {shlex.quote(cwd)}\n"
                self.last_cwd = cwd
            self.proc.stdin.write(
                f"{prefix}builtin . {script} </dev/null 2>&1; __agentd_rc=$?; "
                f"command rm -f {script}; builtin printf '\\n{marker.decode()}%d\\n' \"$__agentd_rc\"\n".encode()
            )
            await self.proc.stdin.drain()

            try:
                code = await asyncio.wait_for(self._stream_until(stream, marker), timeout)
            except asyncio.TimeoutError:
                self._kill()
                _unlink(script)
                await stream.write(f"\n[timed out after {timeout}s; shell session restarted, state lost]".encode())
                return {"exit": 124, "restarted": True}
            if code is None:  # the command ended the shell (e.g. `exit 3`)
                rc = await self.proc.wait()
                self.proc = None
                _unlink(script)
                return {"exit": rc, "restarted": True}
            return {"exit": code, "fresh": fresh}

    async def _stream_until(self, stream: Stream, marker: bytes) -> int | None:
        """Forward shell output up to ``marker``; return the exit code, or None at EOF."""
        buf = b""
        hold = len(marker) + 1  # never forward a partial marker (+ its leading newline)
        while True:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                if buf:
                    await stream.write(buf)
                return None
            buf += chunk
            idx = buf.find(marker)
            if idx >= 0:
                out = buf[:idx]
                if out.endswith(b"\n"):
                    out = out[:-1]  # the newline we print before the marker
                if out:
                    await stream.write(out)
                rest = buf[idx + len(marker):]
                while b"\n" not in rest:
                    more = await self.proc.stdout.read(64)
                    if not more:
                        break
                    rest += more
                return int(rest.split(b"\n", 1)[0] or b"0")
            if len(buf) > hold:
                await stream.write(buf[:-hold])
                buf = buf[-hold:]


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class Sandboxd:
    def __init__(self, port: int):
        self.port = port
        self.mux: Mux | None = None
        self.listeners: dict[str, asyncio.base_events.Server] = {}
        self.shells: dict[str, ShellSession] = {}
        os.makedirs(SHELL_DIR, mode=0o1777, exist_ok=True)
        os.chmod(SHELL_DIR, 0o1777)

    async def serve(self) -> None:
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        sock.bind((socket.VMADDR_CID_ANY, self.port))
        sock.listen(4)
        sock.setblocking(False)
        server = await asyncio.start_server(self._on_host_connection, sock=sock)
        log(f"listening on vsock port {self.port}")
        async with server:
            await server.serve_forever()

    async def serve_stdio(self) -> None:
        """Serve the one host connection on stdin/stdout, then exit."""
        loop = asyncio.get_running_loop()
        # Keep the real stdout for the mux and point fd 1 at stderr, so stray
        # output from anything sandboxd starts can never corrupt the stream.
        mux_fd = os.dup(1)
        os.dup2(2, 1)
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(mux_fd, "wb", buffering=0))
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        log("serving on stdio")
        await self._on_host_connection(reader, writer)

    async def _on_host_connection(self, reader, writer) -> None:
        # The host owns the connection; a new one replaces the old (reconnect).
        if self.mux is not None:
            await self.mux.aclose()
        mux = Mux(reader, writer, is_host=False, on_open=self._on_open)
        self.mux = mux
        await mux.send_hello({"version": VERSION, "pid": os.getpid()})
        await mux.run()

    async def _on_open(self, stream: Stream) -> None:
        kind = stream.meta.get("kind")
        if kind == "exec":
            await self._exec(stream)
        elif kind == "shell":
            await self._shell(stream)
        elif kind == "listen":
            await self._listen(stream)
        elif kind == "shutdown":
            await stream.close({"ok": True})
            os.sync()
            os._exit(int(stream.meta.get("code", 0)))
        else:
            await stream.close({"error": f"unknown stream kind {kind!r}"})

    async def _shell(self, stream: Stream) -> None:
        """Run a command in a named persistent shell session.

        The session is created on first use with ``user`` and ``env``; later
        calls reuse its state and ignore those two. ``reset`` starts over.
        """
        meta = stream.meta
        name = meta.get("session", "default")
        session = self.shells.get(name)
        if session is not None and (meta.get("reset") or session.user != meta.get("user")):
            session._kill()
            session = None
        if session is None:
            session = self.shells[name] = ShellSession(meta.get("user"), meta.get("env") or {})
        result = await session.run(stream, meta["command"], meta.get("cwd"), meta.get("timeout"))
        await stream.close(result)

    async def _exec(self, stream: Stream) -> None:
        meta = stream.meta
        env = dict(os.environ)
        drop = _user_drop(meta.get("user"), env)
        env.update(meta.get("env") or {})
        try:
            proc = await asyncio.create_subprocess_exec(
                *meta["argv"],
                cwd=meta.get("cwd"),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                **drop,
            )
        except (OSError, KeyError) as e:
            await stream.close({"exit": 127, "error": str(e)})
            return

        async def feed_stdin() -> None:
            await pipe_stream_to_writer(stream, proc.stdin)
            try:
                proc.stdin.close()
            except Exception:
                pass

        async def kill_if_abandoned() -> None:
            # The host closed the stream before the command finished.
            await stream.wait_closed()
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        stdin_task = asyncio.ensure_future(feed_stdin())
        watchdog = asyncio.ensure_future(kill_if_abandoned())
        await pipe_reader_to_stream(proc.stdout, stream)
        code = await proc.wait()
        stdin_task.cancel()
        watchdog.cancel()
        await stream.close({"exit": code})

    async def _listen(self, stream: Stream) -> None:
        meta = stream.meta
        name = meta["name"]
        if name in self.listeners:
            await stream.close({"ok": True, "existing": True})
            return

        async def on_local(reader, writer) -> None:
            if self.mux is None:
                writer.close()
                return
            upstream = await self.mux.open_stream({"kind": "connect", "name": name})
            await splice(upstream, reader, writer)

        if "unix" in meta:
            path = meta["unix"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                os.unlink(path)
            server = await asyncio.start_unix_server(on_local, path=path)
            os.chmod(path, 0o777)
        else:
            host, port = meta["tcp"]
            server = await asyncio.start_server(on_local, host=host, port=int(port))
        self.listeners[name] = server
        await stream.close({"ok": True})


def log(msg: str) -> None:
    print(f"sandboxd: {msg}", file=sys.stderr, flush=True)


def main() -> None:
    args = sys.argv[1:]
    if "--overlay" in args:
        args.remove("--overlay")
        enter_overlay_root()
    if "--stdio" in args:
        asyncio.run(Sandboxd(0).serve_stdio())
        return
    port = int(args[0]) if args else 1024
    asyncio.run(Sandboxd(port).serve())


if __name__ == "__main__":
    main()
