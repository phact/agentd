"""Native libkrun on a Linux host (KVM): status and setup.

    agentd-sandbox linux status [--image NAME]
    agentd-sandbox linux setup  [--image NAME [--image-dir DIR]] [--dry-run] [--yes]

What a Linux box needs for ``KrunExecutor()``:

  * ``/dev/kvm`` that this user can open (``kvm`` group, plus an ACL so it
    works in the current login too);
  * libkrunfw and libkrun (pinned versions, sha256-checked; libkrun built
    from source), the same versions agentd uses inside Colima;
  * the ``agentd-krun`` launcher (``agentd/sandbox/build.sh``);
  * a base image in ``~/.agentd/rootfs/NAME`` (built with Docker);
  * a hard open-files limit high enough for libkrun's file server (the
    launcher raises its soft limit to it).

Setup shows the plan and asks before changing anything; steps that need root
use ``sudo`` (it may ask for your password).
"""
from __future__ import annotations

import grp
import json
import os
import platform
import re
import resource
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agentd.sandbox.base import DEFAULT_HOME, HERE
from agentd.sandbox.colima import LIBKRUN_SHA256, LIBKRUN_URL, LIBKRUN_VERSION, LIBKRUNFW_VERSION, Step

LIBKRUNFW_ASSETS = {  # sha256 from the GitHub release's asset digests
    "aarch64": "dea7905a167eee17d482200ea2fe15871aceb293b0674ac825ed7ae69759399f",
    "x86_64": "016c32ddb2a28aa300382cab33352d63a41a901eb36ee8d1ca24b389791c8b91",
}
MIN_NOFILE = 65536
STATE = DEFAULT_HOME / "linux-state.json"
IMAGES_DIR = HERE / "images"


def libkrunfw_url(arch: str) -> str:
    return f"https://github.com/libkrun/libkrunfw/releases/download/v{LIBKRUNFW_VERSION}/libkrunfw-{arch}.tgz"


def _launcher() -> Path:
    from agentd.sandbox.krun import DEFAULT_LAUNCHER

    return DEFAULT_LAUNCHER


def launcher_sha() -> str:
    from agentd.sandbox.colima import launcher_sha as sha

    return sha()


@dataclass
class Status:
    issues: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    blocked: bool = False

    @property
    def ready(self) -> bool:
        return not self.issues

    def report(self) -> str:
        if self.ready:
            lines = ["This Linux host is ready for libkrun sandboxes."]
        else:
            lines = ["This Linux host is not ready for libkrun sandboxes:"] + [f"  - {i}" for i in self.issues]
            if not self.blocked:
                lines.append("Run `agentd-sandbox linux setup` to fix this (it shows the plan and asks first).")
        return "\n".join(lines + [f"note: {n}" for n in self.notes])


def _lib_version(name: str) -> str | None:
    """The installed version of ``name`` (e.g. libkrun.so.1 -> 1.19.6), via ldconfig."""
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(name + " "):
            real = os.path.realpath(line.rsplit("=>", 1)[-1].strip())
            m = re.search(re.escape(name.split(".so")[0]) + r"\.so\.([\d.]+)$", real)
            return m.group(1) if m else "unknown"
    return None


def _has_net() -> bool:
    """Whether the installed libkrun was built with its virtio-net device."""
    import ctypes

    try:
        return hasattr(ctypes.CDLL("libkrun.so.1"), "krun_add_net_unixstream")
    except OSError:
        return False


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _record(**updates) -> None:
    state = _state()
    state.update(updates)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def probe() -> dict:
    """Facts about this host, read-only."""
    user = os.environ.get("USER") or ""
    try:
        kvm_group_members = grp.getgrnam("kvm").gr_mem
        kvm_gid = grp.getgrnam("kvm").gr_gid
    except KeyError:
        kvm_group_members, kvm_gid = [], None
    return {
        "platform": sys.platform,
        "arch": platform.machine(),
        "kvm": os.path.exists("/dev/kvm"),
        "kvm_rw": os.access("/dev/kvm", os.R_OK | os.W_OK),
        "kvm_group_listed": user in kvm_group_members,
        "kvm_group_active": kvm_gid is not None and kvm_gid in os.getgroups(),
        "libkrun": _lib_version("libkrun.so.1"),
        "libkrun_net": _has_net(),
        "libkrunfw": _lib_version("libkrunfw.so.5"),
        "launcher": _launcher().exists(),
        "launcher_sha": _state().get("launcher"),
        "nofile_hard": resource.getrlimit(resource.RLIMIT_NOFILE)[1],
        "docker": shutil.which("docker") is not None,
        "cc": shutil.which("cc") is not None,
    }


def status(image: str = "agents", image_dir: str | Path | None = None, *, facts: dict | None = None) -> Status:
    st = Status()
    p = facts or probe()
    st.info = p
    if p["platform"] != "linux":
        st.issues.append("this isn't Linux (on macOS use native libkrun or `agentd-sandbox colima setup`)")
        st.blocked = True
        return st
    if p["arch"] not in LIBKRUNFW_ASSETS:
        st.issues.append(f"no pinned libkrunfw for {p['arch']} (supported: {', '.join(LIBKRUNFW_ASSETS)})")
        st.blocked = True
        return st
    if not p["kvm"]:
        st.issues.append("/dev/kvm is missing: enable virtualization in the firmware and load the kvm module "
                         "(e.g. `sudo modprobe kvm_intel` or `kvm_amd`); agentd can't do this for you")
        st.blocked = True
    elif not p["kvm_rw"]:
        st.issues.append("this user can't open /dev/kvm")
        st.needs.append("kvm_access")
    elif not p["kvm_group_active"] and p["kvm_group_listed"]:
        st.notes.append("you're in the kvm group but this login predates it; /dev/kvm works now through an ACL")
    if p["nofile_hard"] < MIN_NOFILE:
        st.issues.append(f"the hard open-files limit is {p['nofile_hard']} (libkrun's file server needs "
                         f"{MIN_NOFILE}+)")
        st.needs.append("file_limits")
    if p["libkrunfw"] != LIBKRUNFW_VERSION:
        st.issues.append(f"libkrunfw {LIBKRUNFW_VERSION} is not installed (found: {p['libkrunfw'] or 'none'})")
        st.needs += ["build_deps", "libkrunfw"]
    if p["libkrun"] != LIBKRUN_VERSION or not p.get("libkrun_net", True):
        st.issues.append(f"libkrun {LIBKRUN_VERSION} with networking is not installed "
                         f"(found: {p['libkrun'] or 'none'}{'' if p.get('libkrun_net', True) else ', without networking'})")
        st.needs += ["build_deps", "libkrun"]
    if not p["launcher"] or p["launcher_sha"] != launcher_sha():
        st.issues.append(f"the agentd-krun launcher ({_launcher()}) is missing or out of date")
        st.needs += ["build_deps", "launcher"]
    rootfs = DEFAULT_HOME / "rootfs" / image
    if image_dir is not None or not (rootfs / "usr").is_dir():
        if image_dir is None and not (IMAGES_DIR / image).is_dir():
            st.issues.append(f"no base image {image!r}: pass --image-dir DIR (a directory with a Dockerfile)")
        elif not p["docker"]:
            st.issues.append(f"building the {image!r} base image needs Docker on this host")
            st.blocked = True
        else:
            st.issues.append(f"the {image!r} base image is missing ({rootfs})" if image_dir is None
                             else f"the {image!r} base image will be rebuilt from {image_dir}")
            st.needs.append(f"rootfs:{image}")
    st.needs = list(dict.fromkeys(st.needs))
    return st


def _sh(script: str, what: str) -> Callable[[], None]:
    def run() -> None:
        r = subprocess.run(["sh", "-c", script])  # inherits the terminal (sudo may prompt)
        if r.returncode != 0:
            raise RuntimeError(f"{what} failed (exit {r.returncode})")
    return run


def plan(st: Status, *, image: str = "agents", image_dir: str | Path | None = None) -> list[Step]:
    q = shlex.quote
    arch = st.info.get("arch", platform.machine())
    steps: dict[str, Step] = {}
    kvm = ('sudo usermod -aG kvm "$USER" && '
           '{ command -v setfacl >/dev/null && sudo setfacl -m "u:$USER:rw" /dev/kvm || true; }')
    steps["kvm_access"] = Step("kvm_access", "Let this user open /dev/kvm (kvm group; an ACL for the current login)",
                               [kvm], run=_sh(kvm, "granting /dev/kvm access"))
    limits = (f"printf '%s soft nofile {MIN_NOFILE * 16}\\n%s hard nofile {MIN_NOFILE * 16}\\n' \"$USER\" \"$USER\" | "
              "sudo tee /etc/security/limits.d/99-agentd.conf >/dev/null")
    steps["file_limits"] = Step("file_limits", "Raise this user's open-files limit (limits.d; applies to new logins)",
                                [limits], run=_sh(limits, "raising the open-files limit"))
    deps = ("set -e; if command -v apt-get >/dev/null; then sudo apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential curl ca-certificates "
            "patchelf pkg-config libclang-dev acl; "
            "elif command -v dnf >/dev/null; then sudo dnf install -y gcc make curl patchelf pkgconf clang-devel "
            "glibc-static acl; else echo 'install a C compiler, make, curl, patchelf and libclang' >&2; exit 1; fi; "
            "command -v cargo >/dev/null || [ -x ~/.cargo/bin/cargo ] || "
            "curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal")
    steps["build_deps"] = Step("build_deps", "Install build tools (apt or dnf: compiler, patchelf, libclang; Rust via rustup)",
                               [deps], run=_sh(deps, "installing build tools"))
    sha = LIBKRUNFW_ASSETS.get(arch, "")
    fw = (f"set -e; t=$(mktemp -d); cd $t; curl -fsSL -o libkrunfw.tgz {q(libkrunfw_url(arch))}; "
          f"echo '{sha}  libkrunfw.tgz' | sha256sum -c -; sudo tar -xzf libkrunfw.tgz -C /usr/local; "
          "sudo ldconfig; rm -rf $t")
    steps["libkrunfw"] = Step("libkrunfw", f"Install libkrunfw {LIBKRUNFW_VERSION} for {arch} (prebuilt, sha256-pinned)",
                              [fw], run=_sh(fw, "installing libkrunfw"))
    krun = (f"set -e; export PATH=$HOME/.cargo/bin:$PATH; t=$(mktemp -d); cd $t; "
            f"curl -fsSL -o src.tgz {q(LIBKRUN_URL)}; echo '{LIBKRUN_SHA256}  src.tgz' | sha256sum -c -; "
            f"tar -xzf src.tgz; cd libkrun-{LIBKRUN_VERSION}; make NET=1 -j$(nproc); sudo make install PREFIX=/usr/local; "
            "echo /usr/local/lib64 | sudo tee /etc/ld.so.conf.d/agentd-libkrun.conf >/dev/null; sudo ldconfig; "
            "rm -rf $t")
    steps["libkrun"] = Step("libkrun", f"Build and install libkrun {LIBKRUN_VERSION} from source (sha256-pinned)",
                            [krun], run=_sh(krun, "building libkrun"))
    build = "PATH=$HOME/.cargo/bin:$PATH " + q(str(HERE / "build.sh"))  # also builds agentd-net

    def do_launcher() -> None:
        _sh(build, "building the launcher")()
        _record(launcher=launcher_sha())
    steps["launcher"] = Step("launcher", f"Build the agentd-krun launcher ({_launcher()})", [build], run=do_launcher)
    for need in st.needs:
        if need.startswith("rootfs:"):
            name = need.split(":", 1)[1]
            src = Path(image_dir).expanduser().resolve() if image_dir and name == image else IMAGES_DIR / name
            cmd = [sys.executable, "-m", "agentd.sandbox.rootfs", "build", str(src), name]
            steps[need] = Step(need, f"Build base image {name!r} from {src} into {DEFAULT_HOME / 'rootfs' / name} "
                               "(extracted with sudo, so its files keep their real owners)",
                               [" ".join(q(c) for c in cmd)], run=_sh(" ".join(q(c) for c in cmd), f"building {name}"))
    return [steps[k] for k in st.needs if k in steps]


def setup(*, image: str = "agents", image_dir: str | Path | None = None,
          approve: Callable[[list[Step]], bool] | None = None, log: Callable[[str], None] = print) -> Status:
    st = status(image, image_dir)
    if st.ready:
        log(st.report())
        return st
    if st.blocked:
        log(st.report())
        return st
    steps = plan(st, image=image, image_dir=image_dir)
    if approve is None:
        from agentd.sandbox.colima import _ask_on_terminal

        approve = lambda steps: _ask_on_terminal(steps, "local")  # noqa: E731
    if not approve(steps):
        log("Nothing was changed.")
        return st
    for step in steps:
        log(f"==> {step.description}")
        step.run()
    final = status(image)
    log(final.report())
    return final
