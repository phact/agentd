"""One sandbox session: the sandbox plus everything wired to it on the host.

A ``SandboxSession`` is what PTC's executor (:mod:`agentd.sandbox.executor`)
and the harnesses (Claude Code, Codex, ...) share, so switching harness keeps
the same sandbox: files, installed packages and running processes survive.
The sandbox is a libkrun microVM (``backend="krun"``) or a Docker container
(``backend="docker"``); everything else is identical.

It boots lazily with:
  * the workspace (and PTC's skills dir if outside it) at their host paths,
  * each harness's transcript folder from the host (see ``transcripts``),
  * sandbox-side endpoints: ``bridge`` (MCP bridge, for the skills CLI),
    ``anthropic`` and ``openai`` (model APIs, each via a host
    :class:`ModelProxy` that adds the real credential),

All of it lives on one event loop (the executor's), because the mux
connection is bound to the loop it was opened on.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from agentd.model_proxy import Credentials, ModelProxy, default_anthropic_credentials, default_openai_upstream
from agentd.sandbox.base import DEFAULT_HOME, Endpoint, Sandbox
from agentd.sandbox.docker import DEFAULT_IMAGE, DockerSandbox
from agentd.sandbox.krun import KrunSandbox

DEFAULT_ROOTFS = Path(os.environ.get("AGENTD_ROOTFS", DEFAULT_HOME / "rootfs" / "agents"))
BACKENDS = ("krun", "docker")
SANDBOX_USER = "agent"
SANDBOX_HOME = f"/home/{SANDBOX_USER}"
SANDBOX_BRIDGE_SOCKET = "/run/agentd/bridge.sock"
ANTHROPIC_ENDPOINT = ("tcp", "127.0.0.1", 8080)
OPENAI_ENDPOINT = ("tcp", "127.0.0.1", 8081)
# Real hostnames served inside the sandbox over TLS (agentd.sandbox.tls), for
# clients that require them: hostname -> sandbox loopback address.
TLS_HOSTS = {"chatgpt.com": "127.0.0.2"}
SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass
class TranscriptMount:
    """A harness's session-log store: a host dir shared at a sandbox path,
    synced to ``native_dir`` (where the CLI keeps sessions on the host)."""

    sandbox_path: str
    host_dir: Path
    native_dir: Path | None = None


@dataclass
class SandboxSession:
    workspace: Path
    backend: str = "krun"
    rootfs: Path = DEFAULT_ROOTFS  # krun: base image directory
    image: str = DEFAULT_IMAGE      # docker: image name
    user: str = SANDBOX_USER
    cpus: int = 2
    mem_mib: int = 2048
    skills_dir: Path | None = None
    anthropic_credentials: Credentials | None = None
    # harness name -> where its transcripts go; filled in by the harnesses'
    # defaults (see agentd.harness) unless given explicitly.
    transcripts: dict[str, TranscriptMount] = field(default_factory=dict)
    # Host socket the MCP bridge listens on (PTC may pick it before boot).
    bridge_socket_path: Path | None = None
    # Root of the transcript stores (default ~/.agentd/transcripts).
    transcripts_root: Path | None = None
    # Copy transcripts to each CLI's native path after every turn.
    sync_transcripts: bool = True
    # Extra host directories shared read-only, as {sandbox_path: host_path}.
    read_only_mounts: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError(f"unknown sandbox backend {self.backend!r}; expected one of {BACKENDS}")
        self.workspace = Path(self.workspace).resolve()
        self.tag = os.urandom(4).hex()
        run_dir = DEFAULT_HOME / "run"
        self.bridge_socket_path = self.bridge_socket_path or run_dir / f"{self.tag}-bridge.sock"
        self.sandbox: Sandbox | None = None
        self._proxies: list[ModelProxy] = []
        self.openai_credentials: Credentials | None = None

    async def start(self) -> "SandboxSession":
        if self.sandbox is not None:
            return self
        from agentd.harness import default_transcript_mounts

        for name, mount in default_transcript_mounts(self.workspace, self.transcripts_root).items():
            self.transcripts.setdefault(name, mount)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.bridge_socket_path.parent.mkdir(parents=True, exist_ok=True)

        run_dir = DEFAULT_HOME / "run"
        anthropic = ModelProxy(
            run_dir / f"{self.tag}-anthropic.sock",
            self.anthropic_credentials or default_anthropic_credentials(),
        )
        upstream, self.openai_credentials, prefixes = default_openai_upstream()
        openai = ModelProxy(run_dir / f"{self.tag}-openai.sock", self.openai_credentials, upstream, prefixes)
        proxies = [anthropic, openai]
        tls_endpoints: dict[str, Endpoint] = {}
        if upstream.startswith("https://chatgpt.com"):
            from agentd.sandbox.tls import server_context

            chatgpt = ModelProxy(run_dir / f"{self.tag}-chatgpt.sock", self.openai_credentials, upstream,
                                 prefixes, ssl_context=server_context("chatgpt.com"))
            proxies.append(chatgpt)
            tls_endpoints["chatgpt.com"] = Endpoint(
                ("tcp", TLS_HOSTS["chatgpt.com"], 443), ("unix", str(chatgpt.socket_path)))
        # Credentials are only read when a request arrives, so starting them is free.
        for proxy in proxies:
            await proxy.start()
            self._proxies.append(proxy)

        mounts: dict[str, Path] = {}
        if self.skills_dir is not None and self.skills_dir.is_dir() and not self.skills_dir.is_relative_to(self.workspace):
            mounts[str(self.skills_dir)] = self.skills_dir
        for mount in self.transcripts.values():
            mount.host_dir.mkdir(parents=True, exist_ok=True)
            mounts[mount.sandbox_path] = mount.host_dir

        common = dict(
            workspace=self.workspace,
            mounts=mounts,
            read_only_mounts=dict(self.read_only_mounts),
            endpoints={
                "bridge": Endpoint(("unix", SANDBOX_BRIDGE_SOCKET), ("unix", str(self.bridge_socket_path))),
                "anthropic": Endpoint(ANTHROPIC_ENDPOINT, ("unix", str(anthropic.socket_path))),
                "openai": Endpoint(OPENAI_ENDPOINT, ("unix", str(openai.socket_path))),
                **tls_endpoints,
            },
            cpus=self.cpus,
            mem_mib=self.mem_mib,
            user=self.user,
        )
        if self.backend == "krun":
            sandbox: Sandbox = KrunSandbox(rootfs=self.rootfs, **common)
        else:
            sandbox = DockerSandbox(image=self.image, **common)
        try:
            await sandbox.start()
            if tls_endpoints:
                await self._trust_tls_hosts(sandbox, list(tls_endpoints))
        except BaseException:
            await self._stop_proxies()
            raise
        self.sandbox = sandbox
        return self

    async def stop(self) -> None:
        if self.sandbox is not None:
            await self.sandbox.stop()
            self.sandbox = None
            await asyncio.to_thread(self.sync_transcripts_out)
        await self._stop_proxies()
        self.bridge_socket_path.unlink(missing_ok=True)

    async def _trust_tls_hosts(self, sandbox: Sandbox, hosts: list[str]) -> None:
        """Point ``hosts`` at their sandbox endpoints and trust agentd's CA (this sandbox only)."""
        from agentd.sandbox.tls import ensure_ca

        ca_cert, _ = ensure_ca()
        entries = "".join(f"{TLS_HOSTS[h]} {h}\\n" for h in hosts)
        out, code = await sandbox.exec(
            ["/bin/sh", "-c", f"printf '{entries}' >> /etc/hosts && cat >> /etc/ssl/certs/ca-certificates.crt"],
            stdin=ca_cert.read_bytes(), user="root",
        )
        if code != 0:
            raise RuntimeError(f"sandbox TLS setup failed: {out.decode(errors='replace')}")

    async def _stop_proxies(self) -> None:
        for proxy in self._proxies:
            await proxy.stop()
        self._proxies.clear()

    def sync_transcripts_out(self, harness: str | None = None) -> list[Path]:
        """Copy new/changed transcripts to the CLIs' native paths (blocking I/O)."""
        from agentd.harness.transcripts import sync_out

        if not self.sync_transcripts:
            return []
        copied: list[Path] = []
        for name, mount in self.transcripts.items():
            if mount.native_dir is not None and (harness is None or name == harness):
                copied += sync_out(mount.host_dir, mount.native_dir)
        return copied

    def pull_transcript(self, harness: str, session_id: str) -> list[Path]:
        """Bring a session from the native path into the store if newer there."""
        from agentd.harness.transcripts import pull_in

        mount = self.transcripts.get(harness)
        if mount is None or mount.native_dir is None or not self.sync_transcripts:
            return []
        return pull_in(harness, mount.host_dir, mount.native_dir, session_id)

    def base_env(self) -> dict[str, str]:
        """Environment every exec, shell and harness CLI gets in the sandbox."""
        skills = self.skills_dir or (self.workspace / "skills")
        return {
            "PATH": f"{skills}:{SANDBOX_PATH}",
            "PTC_SKILLS_DIR": str(skills),
            "MCP_BRIDGE_SOCKET": SANDBOX_BRIDGE_SOCKET,
        }
