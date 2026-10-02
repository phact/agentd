"""PTC executors backed by a sandbox session.

:class:`KrunExecutor` (libkrun microVM) and :class:`DockerExecutor` (Docker
container) are the same executor over different backends: one sandbox per
executor (a :class:`~agentd.sandbox.session.SandboxSession`), booted on
first use and kept, so files, installed packages and background
processes persist between calls. Bash and Python run in one persistent shell
session, so ``cd``, ``export``, ``pushd``/``popd`` and shell functions carry
over too (Python sees the shell's cwd and exported env). The first call's
``cwd`` becomes the workspace, shared at the same absolute path inside the
sandbox, so host paths work unchanged.

The sandbox has no network. The skills CLI reaches the MCP bridge through a
sandbox-side socket tunneled to the host bridge socket that PTC starts at
:attr:`bridge_socket_path`. Execs get only an explicit environment; nothing
from the host's ``os.environ`` leaks in. Harnesses (``agentd.harness``) run in
the same session via :meth:`ensure_session` / :meth:`run`.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import threading
from pathlib import Path, PurePosixPath

from typing import Any, AsyncIterator

from agentd.sandbox.base import DEFAULT_HOME
from agentd.sandbox.docker import DEFAULT_IMAGE
from agentd.sandbox.krun import DEFAULT_LAUNCHER
from agentd.sandbox.mux import MAX_FRAME
from agentd.sandbox.session import DEFAULT_ROOTFS, SANDBOX_USER, SandboxSession

__all__ = ["SandboxExecutor", "KrunExecutor", "DockerExecutor", "default_executor", "read_only_mounts",
           "DEFAULT_ROOTFS"]

Mounts = "list[str | Path] | dict[str | Path, str] | None"


def read_only_mounts(mounts) -> dict[str, Path]:
    """Normalize ``mounts=`` to {sandbox_path: host_dir}.

    ``["~/data", "/opt/models"]`` shares each directory at the same absolute
    path inside the sandbox; ``{"~/notes": "/home/agent/notes"}`` picks the
    sandbox path. Host paths must be existing directories.
    """
    if not mounts:
        return {}
    pairs = mounts.items() if isinstance(mounts, dict) else [(m, None) for m in mounts]
    result: dict[str, Path] = {}
    for host, target in pairs:
        host_path = Path(host).expanduser().resolve()
        if not host_path.is_dir():
            raise NotADirectoryError(f"mount source {host} is not an existing directory")
        target = str(target) if target is not None else str(host_path)
        if not target.startswith("/"):
            raise ValueError(f"sandbox path for {host} must be absolute, got {target!r}")
        if target in result:
            raise ValueError(f"two mounts at sandbox path {target}")
        result[target] = host_path
    return result


class SandboxExecutor:
    """Executor protocol implementation that runs everything in a sandbox."""

    backend = "krun"

    def __init__(
        self,
        *,
        rootfs: Path | str = DEFAULT_ROOTFS,
        image: str = DEFAULT_IMAGE,
        colima: str | None = None,
        timeout: int = 60,
        user: str = SANDBOX_USER,
        cpus: int = 2,
        mem_mib: int = 2048,
        env: dict[str, str] | None = None,
        transcripts_dir: Path | str | None = None,
        sync_transcripts: bool = True,
        mounts: Mounts = None,
        egress: Any = None,
    ):
        """``transcripts_dir``: root for harness transcript stores (default
        ``~/.agentd/transcripts``). ``sync_transcripts``: also copy them to
        each CLI's usual place (``~/.claude/projects``, ``~/.codex/sessions``).
        ``mounts``: extra host directories the sandbox can read but never
        change, as a list (each at the same path inside) or a
        ``{host_path: sandbox_path}`` dict (see :func:`read_only_mounts`)."""
        self.rootfs = Path(rootfs) if not colima else rootfs
        self.image = image
        self.colima = colima
        self.timeout = timeout
        self.user = user
        self.cpus = cpus
        self.mem_mib = mem_mib
        self.extra_env = dict(env or {})
        self.transcripts_dir = Path(transcripts_dir).resolve() if transcripts_dir else None
        self.sync_transcripts = sync_transcripts
        self.read_only_mounts = read_only_mounts(mounts)
        self.egress = egress  # agentd.egress.Egress: network access (libkrun only)
        self._id = os.urandom(4).hex()
        self._bridge_socket = DEFAULT_HOME / "run" / f"{self._id}-bridge.sock"
        self.session: SandboxSession | None = None
        self._boot_lock = asyncio.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name=f"krun-{self._id}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    # Bridge wiring (read by PTC's setup_skills_directory)
    # ------------------------------------------------------------------ #

    @property
    def bridge_socket_path(self) -> Path:
        self._bridge_socket.parent.mkdir(parents=True, exist_ok=True)
        return self._bridge_socket

    # ------------------------------------------------------------------ #
    # Executor protocol
    # ------------------------------------------------------------------ #

    def execute_bash(self, command: str, cwd: Path) -> tuple[str, int]:
        return self._run_sync(self._guard(self._bash(command, Path(cwd))))

    async def execute_bash_async(self, command: str, cwd: Path) -> tuple[str, int]:
        return await self.run(self._guard(self._bash(command, Path(cwd))))

    def execute_python(self, code: str, cwd: Path, pythonpath: Path | None = None) -> tuple[str, int]:
        return self._run_sync(self._guard(self._python(code, Path(cwd), pythonpath)))

    async def execute_python_async(
        self, code: str, cwd: Path, pythonpath: Path | None = None
    ) -> tuple[str, int]:
        return await self.run(self._guard(self._python(code, Path(cwd), pythonpath)))

    def create_file(self, filename: str, content: str, cwd: Path) -> str:
        return self._run_sync(self._create_file(filename, content, Path(cwd)))

    def close(self) -> None:
        if self.session is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.session.stop(), self._loop).result(30)
            except Exception:
                pass
            self.session = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._bridge_socket.unlink(missing_ok=True)

    def __enter__(self) -> "SandboxExecutor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Session access for harnesses
    # ------------------------------------------------------------------ #

    async def run(self, coro):
        """Run a coroutine on the session's event loop (from any loop)."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    async def stop_session(self) -> None:
        """Stop the sandbox but keep the executor: the next use boots a fresh one."""
        session, self.session = self.session, None
        if session is not None:
            await self.run(session.stop())

    async def ensure_session(self, cwd: Path) -> SandboxSession:
        """Boot the sandbox (if needed) with ``cwd`` as its workspace."""
        return await self.run(self._ensure_session(Path(cwd)))

    async def stream_exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes = b"",
    ) -> AsyncIterator[tuple[str, Any]]:
        """Run ``argv`` in the (started) session, from any event loop.

        Yields ``("line", bytes)`` for each output line (stdout and stderr
        merged) as it arrives, then ``("exit", meta)``. If the caller stops
        early, the command's process group is killed in the sandbox.
        """
        session = self.session
        assert session is not None, "call ensure_session() first"
        stream = await self.run(session.sandbox.open_exec(
            argv, cwd=cwd, env={**session.base_env(), **self.extra_env, **(env or {})}
        ))
        finished = False
        try:
            if stdin:
                await self.run(stream.write(stdin))
            await self.run(stream.write_eof())
            buf = b""
            while chunk := await self.run(stream.read(MAX_FRAME)):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    yield "line", line
            if buf:
                yield "line", buf
            meta = await self.run(stream.wait_closed()) or {}
            finished = True
            yield "exit", meta
        finally:
            if not finished:
                await self.run(stream.close({"signal": "cancel"}))

    # ------------------------------------------------------------------ #
    # Implementation (runs on the executor's loop)
    # ------------------------------------------------------------------ #

    async def _bash(self, command: str, cwd: Path) -> tuple[str, int]:
        session = await self._ensure_session(cwd)
        out, code, _ = await session.sandbox.shell(
            command, cwd=self._to_sandbox(cwd), env=self._env(), timeout=self.timeout
        )
        return self._decode(out, code)

    async def _python(self, code: str, cwd: Path, pythonpath: Path | None) -> tuple[str, int]:
        session = await self._ensure_session(cwd)
        # Run through the shell session so Python sees its cwd and exported env.
        delim = f"__AGENTD_PY_{os.urandom(8).hex()}__"
        prefix = ""
        if pythonpath is not None:
            prefix = f"PYTHONPATH={shlex.quote(self._to_sandbox(Path(pythonpath)))}${{PYTHONPATH:+:$PYTHONPATH}} "
        command = f"{prefix}python3 - <<'{delim}'\n{code}\n{delim}"
        out, rc, _ = await session.sandbox.shell(
            command, cwd=self._to_sandbox(cwd), env=self._env(), timeout=self.timeout
        )
        return self._decode(out, rc)

    async def _create_file(self, filename: str, content: str, cwd: Path) -> str:
        session = await self._ensure_session(cwd)
        try:
            target = str(PurePosixPath(self._to_sandbox(cwd)) / filename)
        except ValueError as e:
            return f"Error creating file {filename}: {e}"
        script = f"mkdir -p {shlex.quote(str(PurePosixPath(target).parent))} && cat > {shlex.quote(target)}"
        out, code = await session.sandbox.exec(["/bin/sh", "-c", script], env=self._env(), stdin=content.encode())
        if code != 0:
            return f"Error creating file {filename}: {out.decode(errors='replace').strip()}"
        return f"Created file: {filename}"

    async def _ensure_session(self, cwd: Path) -> SandboxSession:
        if self.session is not None:
            return self.session
        async with self._boot_lock:
            if self.session is None:
                skills = self._skills_dir()
                session = SandboxSession(
                    cwd,
                    backend=self.backend,
                    rootfs=self.rootfs,
                    image=self.image,
                    colima=self.colima,
                    user=self.user,
                    cpus=self.cpus,
                    mem_mib=self.mem_mib,
                    skills_dir=Path(skills).resolve() if skills else None,
                    bridge_socket_path=self.bridge_socket_path,
                    transcripts_root=self.transcripts_dir,
                    sync_transcripts=self.sync_transcripts,
                    read_only_mounts=self.read_only_mounts,
                    egress=self.egress,
                )
                await session.start()
                self.session = session
        return self.session

    def _to_sandbox(self, host_path: Path) -> str:
        """Check a host path is shared with the sandbox; it has the same path there."""
        resolved = host_path.resolve()
        session = self.session
        roots = (session.workspace, session.skills_dir) if session else ()
        for root in roots:
            if root is not None and resolved.is_relative_to(root):
                return str(resolved)
        raise ValueError(f"{host_path} is outside the sandbox workspace {session and session.workspace}")

    @staticmethod
    def _skills_dir() -> str | None:
        """PTC's skills dir (set in os.environ by setup_skills_directory), if it exists."""
        skills = os.environ.get("PTC_SKILLS_DIR")
        return skills if skills and Path(skills).is_dir() else None

    def _env(self) -> dict[str, str]:
        return {**self.session.base_env(), **self.extra_env}

    @staticmethod
    def _decode(out: bytes, code: int) -> tuple[str, int]:
        return out.decode("utf-8", errors="replace").rstrip("\n"), code

    # ------------------------------------------------------------------ #
    # Loop plumbing
    # ------------------------------------------------------------------ #

    def _run_sync(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _guard(self, coro):
        try:
            return await coro
        except ValueError as e:
            return str(e), 1


class KrunExecutor(SandboxExecutor):
    """Sandbox executor on a libkrun microVM (the strongest isolation).

    ``colima=True`` (or a profile name) runs the microVMs with Linux libkrun
    inside a Colima VM with nested virtualization instead of natively; the
    base image then lives on that VM's disk. ``agentd-sandbox colima setup``
    prepares the VM (it asks before changing anything).

    ``image`` picks a base image by name (``~/.agentd/rootfs/NAME`` natively,
    or one built into the Colima VM with ``agentd-sandbox colima setup
    --image-dir DIR --image NAME``); ``rootfs`` gives a path instead."""

    backend = "krun"

    def __init__(self, rootfs: Path | str | None = None, *, colima: bool | str | None = None,
                 image: str | None = None, **kwargs):
        from agentd.sandbox import colima as colima_mod

        profile = colima_mod.PROFILE if colima is True else (colima or None)
        if rootfs is not None and image is not None:
            raise ValueError("pass rootfs= or image=, not both")
        if rootfs is None:
            name = image or "agents"
            rootfs = colima_mod.vm_rootfs(name) if profile else DEFAULT_ROOTFS.parent / name
        super().__init__(rootfs=rootfs, colima=profile, **kwargs)


class DockerExecutor(SandboxExecutor):
    """Sandbox executor on a Docker container (``--network none``, one per session)."""

    backend = "docker"

    def __init__(self, image: str = DEFAULT_IMAGE, **kwargs):
        super().__init__(image=image, **kwargs)


def krun_available(rootfs: Path | str = DEFAULT_ROOTFS) -> bool:
    """Native libkrun: the signed launcher and a local base image exist."""
    return DEFAULT_LAUNCHER.exists() and (Path(rootfs) / "usr").is_dir()


def docker_available(image: str = DEFAULT_IMAGE) -> bool:
    import subprocess

    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return r.returncode == 0


def colima_available(profile: str | None = None) -> bool:
    """True if a Colima profile is already set up for libkrun (read-only check)."""
    from agentd.sandbox import colima

    profile = profile or colima.PROFILE
    if shutil.which("colima") is None or not (Path.home() / ".colima" / profile).is_dir():
        return False
    return colima.status(profile).ready


def default_executor() -> SandboxExecutor:
    """The best sandbox that is already set up: native libkrun, libkrun in
    Colima, then Docker. ``AGENTD_SANDBOX`` = ``krun`` | ``krun-colima`` |
    ``docker`` forces one. Nothing is set up implicitly."""
    choice = os.environ.get("AGENTD_SANDBOX")
    if choice == "krun":
        return KrunExecutor()
    if choice == "krun-colima":
        return KrunExecutor(colima=True)
    if choice == "docker":
        return DockerExecutor()
    if choice:
        raise ValueError(f"AGENTD_SANDBOX must be krun, krun-colima or docker, not {choice!r}")
    if krun_available():
        return KrunExecutor()
    if colima_available():
        return KrunExecutor(colima=True)
    if docker_available():
        return DockerExecutor()
    raise RuntimeError(
        "No sandbox available for agentd. Set one up:\n"
        "  libkrun (macOS arm64 / Linux KVM): agentd/sandbox/build.sh && "
        "python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents\n"
        "  libkrun in Colima (macOS, M3+): agentd-sandbox colima setup\n"
        "  Docker: python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents "
        "(builds the agentd-sandbox-agents image too)\n"
        "or pass executor=KrunExecutor(...) / DockerExecutor(...) explicitly."
    )

