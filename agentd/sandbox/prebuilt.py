"""Binaries that ship in agentd's platform wheels.

Release builds (``scripts/build-binaries.sh``, run by the release workflow)
put these in ``agentd/sandbox/bin`` along with ``prebuilt.json``:

  * ``agentd-krun`` / ``agentd-net``: the launcher (ad-hoc signed) and network
    card for macOS;
  * ``agentd-krun-linux-ARCH`` / ``agentd-net-linux-ARCH``: the same for Linux
    (also what runs inside Colima);
  * ``libkrun-linux-ARCH.so``: libkrun built with networking, installed by
    ``agentd-sandbox linux setup`` / ``colima setup`` instead of building it.

``prebuilt.json`` records the ``launcher.c`` and libkrun they were built from,
so a stale binary (a checkout with edited sources) is never used. A checkout
without them builds everything from source as before.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from agentd.sandbox.base import HERE

BIN = HERE / "bin"
MANIFEST = BIN / "prebuilt.json"


def manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


def launcher(target: str) -> Path | None:
    """The prebuilt launcher for ``target`` ("darwin" or "linux-ARCH"), if built from this launcher.c."""
    from agentd.sandbox.colima import launcher_sha

    path = BIN / ("agentd-krun" if target == "darwin" else f"agentd-krun-{target}")
    m = manifest()
    if path.exists() and target in m.get("targets", []) and m.get("launcher") == launcher_sha():
        return path
    return None


def libkrun(arch: str) -> Path | None:
    """The prebuilt libkrun (with networking) for Linux on ``arch``, if it's the version agentd pins."""
    from agentd.sandbox.colima import LIBKRUN_BUILD

    path = BIN / f"libkrun-linux-{arch}.so"
    m = manifest()
    if path.exists() and f"linux-{arch}" in m.get("targets", []) and m.get("libkrun") == LIBKRUN_BUILD:
        return path
    return None


def install_libkrun_script(src: str, version: str) -> str:
    """Shell that installs a libkrun .so (from ``src``, a path or "-" for stdin) where a source build would."""
    lib = f"/usr/local/lib64/libkrun.so.{version}"
    read = f"sudo install -D -m 755 {shlex.quote(src)} {lib}" if src != "-" else \
        f"sudo mkdir -p /usr/local/lib64 && sudo tee {lib} >/dev/null && sudo chmod 755 {lib}"
    return (f"set -e; {read}; sudo ln -sf libkrun.so.{version} /usr/local/lib64/libkrun.so.1; "
            "echo /usr/local/lib64 | sudo tee /etc/ld.so.conf.d/agentd-libkrun.conf >/dev/null; sudo ldconfig")
