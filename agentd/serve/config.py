"""``agentd serve`` settings (``~/.agentd/serve/config.json``; every key optional).

```json
{
  "box_name": "sabik",
  "identity_header": "X-P2claw-Peer",
  "workspace_roots": ["~/agentd-workspaces"],
  "idle_timeout": 600,
  "drivers": ["<peer id allowed to drive every session>"],
  "default_harness": "claude-code",
  "sandbox": {"backend": "auto", "image": "agents", "cpus": 2, "mem_mib": 2048,
              "mounts": {"~/data": "/data"}},
  "harness_options": {"codex": {"upstream": {"base_url": "http://10.0.2.58:8001/v1"},
                                "model": "Qwen/Qwen3.8-27B",
                                "config": {"web_search": "disabled"}}}
}
```
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentd.sandbox.base import DEFAULT_HOME, protected_host_paths

HARNESSES = ("claude-code", "codex", "opencode", "omp")
BACKENDS = ("auto", "krun", "krun-colima", "docker")


def _claude_projects() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


@dataclass
class ServeConfig:
    box_name: str = field(default_factory=lambda: socket.gethostname().split(".")[0])
    dir: Path = field(default_factory=lambda: DEFAULT_HOME / "serve")
    identity_header: str = "X-P2claw-Peer"
    workspace_roots: list[Path] = field(default_factory=lambda: [DEFAULT_HOME / "workspaces"])
    idle_timeout: float = 600.0
    drivers: list[str] = field(default_factory=list)
    default_harness: str = "claude-code"
    sandbox: dict[str, Any] = field(default_factory=dict)
    harness_options: dict[str, dict] = field(default_factory=dict)
    claude_projects: Path = field(default_factory=_claude_projects)
    transcripts_root: Path | None = None
    # Network access for sandboxes (libkrun only; see docs/egress-and-secrets.md):
    # {"allow": ["pypi.org", ...], "fnox": true, "fnox_profile": null,
    #  "approvals": {"webhook": "https://...", "secret_env": "AGENTD_WEBHOOK_SECRET",
    #                "hold": 25, "answer_url": "https://<this box>"}}
    # None: sandboxes have no network.
    egress: dict[str, Any] | None = None
    # Peers (besides local callers) allowed to decide approvals.
    approvers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.dir = Path(self.dir).expanduser()
        self.workspace_roots = [Path(r).expanduser().resolve() for r in self.workspace_roots]
        self.claude_projects = Path(self.claude_projects).expanduser()
        if self.transcripts_root is not None:
            self.transcripts_root = Path(self.transcripts_root).expanduser()
        if not self.workspace_roots:
            raise ValueError("workspace_roots must list at least one directory")
        if self.default_harness not in HARNESSES:
            raise ValueError(f"default_harness must be one of {HARNESSES}")
        backend = self.sandbox.get("backend", "auto")
        if backend not in BACKENDS:
            raise ValueError(f"sandbox.backend must be one of {BACKENDS}, not {backend!r}")
        protected = protected_host_paths() + [self.dir.resolve()]
        for root in self.workspace_roots:
            for p in protected:
                if root == p or p.is_relative_to(root) or root.is_relative_to(p):
                    raise ValueError(f"workspace root {root} would expose {p} to sandboxes")

    @classmethod
    def load(cls, path: str | Path | None = None, **overrides: Any) -> "ServeConfig":
        """Read ``path`` (default ``~/.agentd/serve/config.json``, if present)."""
        data: dict[str, Any] = {}
        path = Path(path).expanduser() if path else (overrides.get("dir") or DEFAULT_HOME / "serve") / "config.json"
        if Path(path).is_file():
            data = json.loads(Path(path).read_text())
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown agentd serve settings: {sorted(unknown)}")
        data.update(overrides)
        return cls(**data)

    @property
    def serve_socket(self) -> Path:
        return self.dir / "serve.sock"

    @property
    def peers_socket(self) -> Path:
        return self.dir / "peers.sock"

    def resolve_workspace(self, workspace: str | None, session_id: str) -> Path:
        """A session's workspace: under one of ``workspace_roots`` (relative names
        go under the first; none means a fresh directory named after the session)."""
        root = self.workspace_roots[0]
        path = root / session_id if not workspace else Path(workspace).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not any(path == r or path.is_relative_to(r) for r in self.workspace_roots):
            raise ValueError(f"workspace {path} is not under an allowed root "
                             f"({', '.join(str(r) for r in self.workspace_roots)})")
        return path

    def harness_kwargs(self, harness: str) -> dict[str, Any]:
        """Constructor options for a harness (an ``upstream`` dict becomes a ModelUpstream)."""
        options = dict(self.harness_options.get(harness, {}))
        upstream = options.get("upstream")
        if isinstance(upstream, dict):
            from agentd.model_proxy import ModelUpstream

            options["upstream"] = ModelUpstream(**upstream)
        return options

    def sandbox_target(self):
        """What :func:`agentd.available` needs to know about this box's sandbox,
        without starting one (None: the default sandbox)."""
        from types import SimpleNamespace

        from agentd.sandbox import executor as ex

        backend = self.sandbox.get("backend", "auto")
        image = self.sandbox.get("image")
        if backend == "auto":
            if image is None:
                return None
            backend = os.environ.get("AGENTD_SANDBOX") or (
                "krun" if ex.krun_available() else "krun-colima" if ex.colima_available() else "docker")
        if backend == "docker":
            name = f"agentd-sandbox-{image}" if image and "/" not in image and ":" not in image else image
            return SimpleNamespace(backend="docker", image=name or ex.DEFAULT_IMAGE)
        if backend == "krun-colima":
            from agentd.sandbox import colima

            profile = self.sandbox.get("colima_profile", True)
            profile = colima.PROFILE if profile is True else profile
            return SimpleNamespace(backend="krun", colima=profile, rootfs=colima.vm_rootfs(image or "agents"))
        return SimpleNamespace(backend="krun", colima=None, rootfs=str(ex.DEFAULT_ROOTFS.parent / (image or "agents")))

    def make_approvals(self):
        """The server's Approvals, if egress approvals are configured."""
        a = (self.egress or {}).get("approvals")
        if not a:
            return None
        from agentd.egress.approvals import Approvals

        secret = os.environ.get(a.get("secret_env", "AGENTD_WEBHOOK_SECRET"))
        return Approvals(a.get("webhook"), secret=secret, hold=float(a.get("hold", 25)),
                         answer_url=a.get("answer_url"))

    def make_egress(self, approvals=None):
        if self.egress is None:
            return None
        from agentd.egress import Egress

        e = self.egress
        return Egress(allow=tuple(e.get("allow", ())), fnox=bool(e.get("fnox", True)),
                      fnox_profile=e.get("fnox_profile"), approvals=approvals)

    def make_executor(self, workspace: Path, image: str | None = None, egress=None):
        """A sandbox executor for one workspace, per the ``sandbox`` settings."""
        from agentd.sandbox import executor as ex

        opts = dict(self.sandbox)
        backend = opts.pop("backend", "auto")
        image = image or opts.pop("image", None)
        opts.pop("image", None)
        profile = opts.pop("colima_profile", True)
        common = {k: opts[k] for k in ("cpus", "mem_mib", "timeout", "mounts", "env") if k in opts}
        common["transcripts_dir"] = self.transcripts_root
        if egress is not None:
            common["egress"] = egress
        if backend == "auto":
            backend = os.environ.get("AGENTD_SANDBOX") or (
                "krun" if ex.krun_available() else "krun-colima" if ex.colima_available()
                else "docker" if ex.docker_available() else None)
            if backend is None:
                raise RuntimeError("no sandbox is set up on this box (see `agentd-sandbox status`)")
        if backend == "krun":
            return ex.KrunExecutor(image=image, **common)
        if backend == "krun-colima":
            return ex.KrunExecutor(colima=profile, image=image, **common)
        docker_image = f"agentd-sandbox-{image}" if image and "/" not in image and ":" not in image else image
        return ex.DockerExecutor(image=docker_image or ex.DEFAULT_IMAGE, **common)
