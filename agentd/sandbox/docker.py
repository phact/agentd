"""Docker backend: one long-lived container per sandbox.

Same model as the libkrun backend, with a container as the boundary:

  * ``--network none``: the container has only loopback, no network.
  * The channel is the host-held ``docker run -i`` process itself: sandboxd
    speaks the mux protocol on its stdin/stdout (``--stdio``), so the host
    owns the only connection, as with libkrun's host-dialed vsock.
  * Minimal privileges: every capability dropped except the few sandboxd
    needs to drop to the sandbox user and run endpoints, ``no-new-privileges``,
    and ``--init`` to reap orphans.

Shared directories are bind mounts. With Docker Desktop / Colima on macOS
they must be inside a directory shared with the Docker VM (``$HOME`` by
default); agentd checks each one is really visible before using it.

Isolation is a container (a shared kernel), weaker than a microVM; prefer
the libkrun backend where available.
"""
from __future__ import annotations

import asyncio
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from agentd.sandbox.base import HERE, SANDBOX_FILES, Sandbox

DEFAULT_IMAGE = "agentd-sandbox-agents"
# DAC_OVERRIDE: sandboxd (root) enters the user's 0700 home before dropping to it.
_CAPS = ("SETUID", "SETGID", "CHOWN", "KILL", "NET_BIND_SERVICE", "DAC_OVERRIDE")


@dataclass(kw_only=True)
class DockerSandbox(Sandbox):
    image: str = DEFAULT_IMAGE
    docker: str = "docker"
    extra_run_args: list[str] = field(default_factory=list)
    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)

    backend = "docker"

    @property
    def container_name(self) -> str:
        return f"agentd-{self.id}"

    async def _boot(self) -> None:
        # sandboxd and its mux, copied per session (bind-mounted read-only).
        code_dir = self.session_dir / "agentd"
        code_dir.mkdir(parents=True, exist_ok=True)
        for name in SANDBOX_FILES:
            shutil.copy2(HERE / name, code_dir / name)
        self._probes = self._write_probes()

        argv = [
            self.docker, "run", "-i", "--rm", "--name", self.container_name,
            "--network", "none", "--init",
            "--cap-drop", "ALL", *[a for cap in _CAPS for a in ("--cap-add", cap)],
            "--security-opt", "no-new-privileges",
            "--cpus", str(self.cpus), "--memory", f"{self.mem_mib}m",
            "--user", "0:0",
            "-v", f"{code_dir}:/agentd:ro",
        ]
        for _, target, host, read_only in self._shares():
            argv += ["-v", f"{host}:{target}:ro" if read_only else f"{host}:{target}"]
        # Files shared from the host can appear root-owned inside the
        # container; tell git not to refuse them as "dubious ownership".
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*", **self.env}
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
        argv += [*self.extra_run_args, self.image, "python3", "-P", "/agentd/sandboxd.py", "--stdio"]

        log = open(self.session_dir / "console.log", "wb")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log,
            )
        finally:
            log.close()
        try:
            await self._attach(self._proc.stdout, self._proc.stdin, timeout=self.boot_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"container {self.container_name} did not come up: {self._console_tail()}") from None
        except Exception as e:
            raise RuntimeError(f"container {self.container_name} failed to start: {e}; {self._console_tail()}") from e

    def _setup_commands(self) -> list[str]:
        commands = []
        for target, (name, _) in getattr(self, "_probes", {}).items():
            q = shlex.quote(f"{target}/{name}")
            hint = shlex.quote(f"{target} is not shared with the Docker VM (put it under a directory "
                               f"Docker can see, e.g. {Path.home()})")
            commands.append(f"{{ [ -e {q} ] || {{ echo {hint}; exit 1; }}; }}")
        if self.user:
            # Docker creates missing mount points as root; give the user the
            # ones it creates inside its home (e.g. ~/.claude/projects).
            home = Path(f"/home/{self.user}")
            dirs = set()
            for _, target, _, _ in self._shares():
                path = Path(target)
                if path.is_relative_to(home):
                    parent = path.parent
                    while parent != home and parent.is_relative_to(home):
                        dirs.add(str(parent))
                        parent = parent.parent
            if dirs:
                commands.append(f"chown {self.user}: " + " ".join(shlex.quote(d) for d in sorted(dirs)))
        return commands

    async def _start(self):
        try:
            return await super()._start()
        finally:
            self._remove_markers()

    async def _teardown(self) -> None:
        self._remove_markers()
        if self._proc is None:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), 10)
        except asyncio.TimeoutError:
            kill = await asyncio.create_subprocess_exec(
                self.docker, "kill", self.container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await kill.wait()
            await self._proc.wait()
