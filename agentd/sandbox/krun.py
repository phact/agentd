"""libkrun backend: one microVM per sandbox.

The VM gets no network device and no TSI (libkrun's transparent socket
proxying), so it has no network at all. Its only channel is one vsock port
the *host* dials (``agentd-krun`` launcher, see ``launcher.c``). Every VM boots
the same base image shared read-only; ``sandboxd --overlay`` puts a
per-session tmpfs layer on top, so a session can write anywhere while the
base is never modified. Shared directories are virtiofs mounts.

Two ways to run it:

  * native: macOS libkrun on Hypervisor.framework (or Linux libkrun on KVM),
    the launcher started directly and its vsock socket dialed by the host;
  * ``colima="<profile>"``: Linux libkrun on KVM inside a Colima VM with
    nested virtualization (see :mod:`agentd.sandbox.colima`). The launcher
    runs in the VM under ``colima_relay``, started by the host with
    ``colima ssh``; that ssh process's stdio is the connection. Colima shares
    ``$HOME`` at the same path, so shared directories need no translation.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import platform
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentd.sandbox.base import HERE, SANDBOX_FILES, Sandbox
from agentd.sandbox.mux import MuxClosed

VSOCK_PORT = 1024
# Minimum boot timeout inside Colima: nested virtualization, and other
# sandboxes sharing the VM's CPUs, make boots slower and more variable.
COLIMA_BOOT_TIMEOUT = 60.0


def _default_launcher() -> Path:
    """Per platform, so a checkout shared by a Mac and Linux (Colima's $HOME
    mount, NFS) keeps both builds."""
    if sys.platform == "darwin":
        return HERE / "bin" / "agentd-krun"
    return HERE / "bin" / f"agentd-krun-linux-{platform.machine()}"


DEFAULT_LAUNCHER = _default_launcher()


def _default_net_tool() -> Path:
    """agentd-net (agentd/sandbox/net): always runs on the host, also for Colima."""
    if sys.platform == "darwin":
        return HERE / "bin" / "agentd-net"
    return HERE / "bin" / f"agentd-net-linux-{platform.machine()}"


DEFAULT_NET_TOOL = _default_net_tool()
# The sandbox's network, when it has one: a small private subnet with agentd-net
# as the gateway (and DNS) and fake IPs for names from 10.212.0.0/16.
GUEST_ADDRESS = "10.211.0.2/24"
GATEWAY = "10.211.0.1"


@dataclass(kw_only=True)
class KrunSandbox(Sandbox):
    # Base image directory: on this machine, or (with ``colima``) on the VM's disk.
    rootfs: Path | str
    launcher: Path | str = DEFAULT_LAUNCHER
    # Run inside this Colima profile's VM instead of natively.
    colima: str | None = None
    # Give the sandbox a network card: its connections reach agentd's egress
    # proxy on this host socket (see docs/egress-and-secrets.md). None: no
    # network at all (the default).
    net_streams: str | Path | None = None
    net_tool: Path | str = DEFAULT_NET_TOOL
    _proc: object = field(default=None, init=False, repr=False)
    _net_proc: object = field(default=None, init=False, repr=False)
    _net_relay: object = field(default=None, init=False, repr=False)
    _net_pumps: list = field(default_factory=list, init=False, repr=False)
    _net_sock: str | None = field(default=None, init=False, repr=False)

    backend = "krun"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.colima:
            from agentd.sandbox import colima

            self.sock_path = f"/tmp/agentd-{self.id}.sock"  # inside the Colima VM
            if self.launcher == DEFAULT_LAUNCHER:
                self.launcher = colima.VM_LAUNCHER
        else:
            self.sock_path = self.home / "run" / f"{self.id}.sock"

    # ------------------------------------------------------------------ #
    # Launch
    # ------------------------------------------------------------------ #

    def _launcher_argv(self, code_dir: Path, rootfs: str) -> list[str]:
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", **self.env}
        argv = [
            str(self.launcher),
            "--root", rootfs, "--root-ro",
            # agentd's sandbox-side code, injected at boot; /agentd/mnt is
            # where sandboxd builds the session overlay.
            "--overlay-dir", "agentd", "--overlay-dir", "agentd/mnt",
            *[a for name in SANDBOX_FILES for a in ("--inject", f"agentd/{name}={code_dir / name}")],
            "--vsock-port", str(VSOCK_PORT),
            "--vsock-sock", str(self.sock_path),
            "--cpus", str(self.cpus),
            "--mem", str(self.mem_mib),
        ]
        for tag, _, host, read_only in self._shares():
            argv += ["--share-ro" if read_only else "--share", f"{tag}={host}"]
        for k, v in env.items():
            argv += ["--env", f"{k}={v}"]
        if self._net_sock:
            argv += ["--net-sock", self._net_sock]
        return argv + ["--", self._python(), "-P", "/agentd/sandboxd.py", str(VSOCK_PORT), "--overlay"]

    def _python(self) -> str:
        """The image's python3, which runs sandboxd."""
        if self.colima:
            from agentd.sandbox import colima

            return colima.image_python(self.colima, Path(self.rootfs).name) or "/usr/local/bin/python3"
        for candidate in ("usr/local/bin/python3", "usr/bin/python3"):
            if (Path(self.rootfs) / candidate).exists() or (Path(self.rootfs) / candidate).is_symlink():
                return "/" + candidate
        return "/usr/local/bin/python3"

    async def _boot(self) -> None:
        if self.net_streams is not None:
            await self._start_net()
        if self.colima:
            await self._boot_in_colima()
        else:
            await self._boot_native()

    async def _start_net(self) -> None:
        """Start agentd-net on the host; natively the VM's network card connects
        to it directly, in Colima through a second `colima ssh` pipe."""
        tool = Path(self.net_tool)
        if not tool.exists():
            raise RuntimeError(f"{tool} is missing: build it with agentd/sandbox/build.sh (needs cargo)")
        frames = self.home / "run" / f"{self.id}-net.sock"
        frames.parent.mkdir(parents=True, exist_ok=True)
        frames.unlink(missing_ok=True)
        with open(self.session_dir / "net.log", "wb") as log:
            # agentd-net exits when its stdin closes: it never outlives us.
            self._net_proc = subprocess.Popen(
                [str(tool), "--frames", str(frames), "--streams", str(self.net_streams),
                 "--gateway", GATEWAY, "--prefix", GUEST_ADDRESS.split("/")[1]],
                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(200):
            if frames.exists() or self._net_proc.poll() is not None:
                break
            await asyncio.sleep(0.01)
        if not frames.exists():
            raise RuntimeError(f"agentd-net did not start: {(self.session_dir / 'net.log').read_text()[-500:]}")
        if not self.colima:
            self._net_sock = str(frames)
            return
        from agentd.sandbox import colima

        vm_sock = f"/tmp/agentd-{self.id}-net.sock"
        code_dir = self.session_dir / "agentd"
        code_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "colima_relay.py", code_dir / "colima_relay.py")
        relay = await asyncio.create_subprocess_exec(
            *colima.ssh_argv(self.colima, "python3", "-u", str(code_dir / "colima_relay.py"), "--net-pump", vm_sock),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)  # killed as a group: `colima ssh` leaves an ssh child holding the pipes
        self._net_relay = relay
        try:
            while True:  # the relay says when it listens, before the launcher may connect
                line = await asyncio.wait_for(relay.stderr.readline(), 30)
                if not line:
                    raise RuntimeError("the Colima network relay exited")
                if b"listening" in line:
                    break
        except asyncio.TimeoutError:
            raise RuntimeError("the Colima network relay did not start") from None
        reader, writer = await asyncio.open_unix_connection(str(frames))

        async def pump(src, dst) -> None:
            try:
                while chunk := await src.read(65536):
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        self._net_pumps = [asyncio.ensure_future(pump(relay.stdout, writer)),
                           asyncio.ensure_future(pump(reader, relay.stdin))]
        self._net_sock = vm_sock

    async def _stop_net(self) -> None:
        for task in self._net_pumps:
            task.cancel()
        self._net_pumps = []
        relay, self._net_relay = self._net_relay, None
        if relay is not None and relay.returncode is None:
            try:
                os.killpg(relay.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(relay.wait(), 5)
            except asyncio.TimeoutError:
                pass
        proc, self._net_proc = self._net_proc, None
        if proc is not None:
            try:
                proc.stdin.close()
                await asyncio.to_thread(proc.wait, 5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        (self.home / "run" / f"{self.id}-net.sock").unlink(missing_ok=True)

    async def _boot_native(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self.sock_path.unlink(missing_ok=True)
        argv = self._launcher_argv(HERE, str(Path(self.rootfs).resolve()))
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

    async def _boot_in_colima(self) -> None:
        from agentd.sandbox import colima

        # Read-only check; never changes the VM (setup does, with approval).
        await asyncio.to_thread(colima.ensure_ready, self.colima, cpus=self.cpus, mem_mib=self.mem_mib,
                                image=Path(self.rootfs).name)
        # sandboxd, its mux and the relay, from a host dir the VM sees at the same path.
        code_dir = self.session_dir / "agentd"
        code_dir.mkdir(parents=True, exist_ok=True)
        for name in (*SANDBOX_FILES, "colima_relay.py"):
            shutil.copy2(HERE / name, code_dir / name)
        self._probes = self._write_probes()
        # The relay runs in the Colima VM, where host directories appear at
        # their *host* paths (the sandbox paths only exist in the microVM).
        hosts = {target: host for _, target, host, _ in self._shares()}
        requires = [a for target, (name, _) in self._probes.items() for a in ("--require", f"{hosts[target]}/{name}")]
        requires += ["--require", str(code_dir / "sandboxd.py")]
        boot_timeout = max(self.boot_timeout, COLIMA_BOOT_TIMEOUT)  # nested virtualization boots slower
        argv = colima.ssh_argv(
            self.colima, "python3", "-u", str(code_dir / "colima_relay.py"),
            "--sock", str(self.sock_path), "--timeout", str(boot_timeout), *requires,
            "--", *self._launcher_argv(code_dir, str(self.rootfs)),
        )
        log = open(self.session_dir / "console.log", "wb")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log,
            )
        finally:
            log.close()
        try:
            await self._attach(self._proc.stdout, self._proc.stdin, timeout=boot_timeout + 15)
        except Exception as e:
            raise RuntimeError(f"sandbox {self.id} in Colima {self.colima!r} did not come up: {e}; "
                               f"{self._console_tail()}") from e

    async def _start(self):
        try:
            return await super()._start()
        finally:
            self._remove_markers()

    def _expected_user_ids(self) -> tuple[int, int]:
        if self.colima:
            from agentd.sandbox import colima

            ids = colima.vm_ids(self.colima)
            if ids is not None:
                return ids  # what files from the host carry inside the Colima VM
        return super()._expected_user_ids()

    # ------------------------------------------------------------------ #
    # Setup / teardown
    # ------------------------------------------------------------------ #

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
        if self.net_streams is not None:
            commands.append(f"{self._python()} -P /agentd/sandboxd.py --configure-net {GUEST_ADDRESS} {GATEWAY}")
        return commands

    async def _teardown(self) -> None:
        self._remove_markers()
        try:
            await self._teardown_vm()
        finally:
            await self._stop_net()

    async def _teardown_vm(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if self.colima:
            # sandboxd was asked to exit; the relay then exits, and so does ssh.
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()  # the relay sees EOF / SIGHUP and kills the launcher
                await proc.wait()
            return
        try:
            await asyncio.to_thread(proc.wait, 10)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait)
        self.sock_path.unlink(missing_ok=True)
