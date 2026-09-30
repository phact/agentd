"""libkrun backend: one microVM per sandbox.

The VM gets no network device and no TSI (libkrun's transparent socket
proxying), so it has no network at all. Its only channel is one vsock port
the *host* dials (``agentd-krun`` launcher, see ``launcher.c``). Every VM boots
the same base image shared read-only; ``sandboxd --overlay`` puts a
per-session tmpfs layer on top, so a session can write anywhere while the
base is never modified. Shared directories are virtiofs mounts.
"""
from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentd.sandbox.base import HERE, SANDBOX_FILES, Sandbox
from agentd.sandbox.mux import MuxClosed

VSOCK_PORT = 1024
DEFAULT_LAUNCHER = HERE / "bin" / "agentd-krun"


@dataclass(kw_only=True)
class KrunSandbox(Sandbox):
    rootfs: Path
    launcher: Path = DEFAULT_LAUNCHER
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)

    backend = "krun"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.sock_path = self.home / "run" / f"{self.id}.sock"

    async def _boot(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self.sock_path.unlink(missing_ok=True)
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", **self.env}
        argv = [
            str(self.launcher),
            "--root", str(Path(self.rootfs).resolve()), "--root-ro",
            # agentd's sandbox-side code, injected from this checkout at boot;
            # /agentd/mnt is where sandboxd builds the session overlay.
            "--overlay-dir", "agentd", "--overlay-dir", "agentd/mnt",
            *[a for name in SANDBOX_FILES for a in ("--inject", f"agentd/{name}={HERE / name}")],
            "--vsock-port", str(VSOCK_PORT),
            "--vsock-sock", str(self.sock_path),
            "--cpus", str(self.cpus),
            "--mem", str(self.mem_mib),
        ]
        for tag, _, host, read_only in self._shares():
            argv += ["--share-ro" if read_only else "--share", f"{tag}={host}"]
        for k, v in env.items():
            argv += ["--env", f"{k}={v}"]
        argv += ["--", "/usr/local/bin/python3", "-P", "/agentd/sandboxd.py", str(VSOCK_PORT), "--overlay"]
        with open(self.session_dir / "console.log", "wb") as log:
            self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

        deadline = time.monotonic() + self.boot_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"sandbox exited during boot ({self._proc.returncode}): {self._console_tail()}")
            if self.sock_path.exists():
                try:
                    reader, writer = await asyncio.open_unix_connection(str(self.sock_path))
                    await self._attach(reader, writer, timeout=2)
                    return
                except (OSError, MuxClosed, asyncio.TimeoutError) as e:
                    # sandboxd not listening yet; libkrun drops the connection.
                    last_error = e
            await asyncio.sleep(0.02)
        raise TimeoutError(f"sandbox {self.id} did not come up: {last_error}; {self._console_tail()}")

    def _setup_commands(self) -> list[str]:
        user_home = f"/home/{self.user}/" if self.user else None
        commands = []
        for tag, target, _, read_only in self._shares():
            q = shlex.quote(target)
            # Mount points under the user's home are created as the user so
            # the parents (e.g. ~/.claude) stay writable for it.
            mkdir = f"runuser -u {self.user} -- mkdir -p {q}" if user_home and target.startswith(user_home) else f"mkdir -p {q}"
            # Read-only is enforced by the virtiofs device; -o ro also inside.
            opts = "-o ro " if read_only else ""
            commands.append(f"{mkdir} && mount -t virtiofs {opts}{tag} {q}")
        return commands

    async def _teardown(self) -> None:
        if self._proc is not None:
            try:
                await asyncio.to_thread(self._proc.wait, 10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                await asyncio.to_thread(self._proc.wait)
        self.sock_path.unlink(missing_ok=True)
