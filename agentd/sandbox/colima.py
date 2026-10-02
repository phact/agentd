"""Run the libkrun backend inside a Colima VM (macOS), via nested virtualization.

``KrunExecutor(colima=True)`` boots each sandbox microVM with Linux libkrun
on KVM *inside* a dedicated Colima VM instead of with macOS libkrun on
Hypervisor.framework. Everything else is the same: the host holds the one
connection (``colima ssh`` stdio, relayed to the microVM's vsock socket by
``colima_relay``), and Colima shares ``$HOME`` at the same path, so the
workspace, skills, transcripts and mounts resolve identically.

What it needs, all checked by :func:`status` (read-only):

  * an Apple M3 or later on macOS 15+ (nested virtualization),
  * a Colima profile (``agentd`` by default, so your other VMs are never
    touched) with ``vmType: vz``, nested virtualization on, and enough
    CPUs / memory / disk for the sandboxes,
  * inside it: access to ``/dev/kvm``, Linux libkrun + libkrunfw, the
    ``agentd-krun`` launcher built against them, and the base image
    exported onto the VM's own disk (so file ownership is real).

:func:`setup` makes it so, but only after showing the plan and getting
approval. agentd never changes a VM on its own: a sandbox that finds the VM
not ready fails with the problems and points at::

    agentd-sandbox colima status
    agentd-sandbox colima setup        # shows the plan and asks first
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agentd.sandbox.base import HERE

PROFILE = "agentd"
# Defaults for a VM agentd creates; status() requires at least what sandboxes need.
DEFAULT_CPUS, DEFAULT_MEMORY_GIB, DEFAULT_DISK_GIB = 4, 8, 60
MIN_DISK_GIB = 30
VM_OVERHEAD_MIB = 1536  # Colima's own Linux + docker

VM_LAUNCHER = "/opt/agentd/bin/agentd-krun"
VM_ROOTFS_ROOT = "/var/lib/agentd/rootfs"
VM_STATE = "/var/lib/agentd/state.json"
NOFILE = 1048576  # open files limit for sessions and services in the VM

LIBKRUN_VERSION = "1.19.6"
# Built with its virtio-net device (`make NET=1`), for sandboxes with a network card.
LIBKRUN_BUILD = f"{LIBKRUN_VERSION}+net"
LIBKRUN_URL = f"https://github.com/libkrun/libkrun/archive/refs/tags/v{LIBKRUN_VERSION}.tar.gz"
LIBKRUN_SHA256 = "7025d72208172dc06f791ad5af7ccaafeabdac9869f74ccb45fb6d4f6e5991cd"
LIBKRUNFW_VERSION = "5.6.2"
LIBKRUNFW_URL = f"https://github.com/libkrun/libkrunfw/releases/download/v{LIBKRUNFW_VERSION}/libkrunfw-aarch64.tgz"
LIBKRUNFW_SHA256 = "dea7905a167eee17d482200ea2fe15871aceb293b0674ac825ed7ae69759399f"

IMAGES_DIR = HERE / "images"


def vm_rootfs(name: str) -> str:
    return f"{VM_ROOTFS_ROOT}/{name}"


def launcher_sha() -> str:
    return hashlib.sha256((HERE / "launcher.c").read_bytes()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
# Any Dockerfile directory can be a sandbox image (``setup --image-dir DIR
# --image NAME``). agentd builds it in the VM as ``agentd-src-NAME``, adds a
# small layer that gives it an ``agent`` user with the VM user's ids (tagged
# ``agentd-sandbox-NAME``, so other images can build ``FROM`` it), checks it
# has python3 >= 3.9 and bash, and exports it to ``/var/lib/agentd/rootfs/NAME``.

WRAPPER_VERSION = "1"
_WRAPPER_DOCKERFILE = r"""ARG BASE
FROM ${BASE}
ARG AGENT_UID
ARG AGENT_GID
USER root
RUN set -e; \
    if id agent >/dev/null 2>&1; then \
      if [ "$(id -u agent):$(id -g agent)" != "$AGENT_UID:$AGENT_GID" ]; then \
        groupmod -o -g "$AGENT_GID" "$(id -gn agent)"; \
        usermod -o -u "$AGENT_UID" -g "$AGENT_GID" agent; \
        home="$(getent passwd agent | cut -d: -f6)"; [ ! -d "$home" ] || chown -R "$AGENT_UID:$AGENT_GID" "$home"; \
      fi; \
    elif command -v useradd >/dev/null 2>&1; then \
      getent group agent >/dev/null || groupadd -o -g "$AGENT_GID" agent; \
      useradd -o -u "$AGENT_UID" -g "$AGENT_GID" -m -s /bin/bash agent; \
    else \
      addgroup -g "$AGENT_GID" agent 2>/dev/null || true; \
      adduser -D -u "$AGENT_UID" -G agent -s /bin/sh agent; \
    fi
"""
_FROM_AGENTD = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*agentd-sandbox-([A-Za-z0-9_.-]+?)(?::\S+)?(?:\s|$)",
                          re.IGNORECASE | re.MULTILINE)
_IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
# Run inside a built image: it must have python3 >= 3.9 (for sandboxd) and bash
# (for shell sessions). Prints where python3 is.
_IMAGE_CHECK = ('p=$(command -v python3) && "$p" -c "import sys; assert sys.version_info >= (3, 9)" '
                '&& command -v bash >/dev/null && echo "$p"')
_SMALL_FILE = 1 << 20


class ImageError(ValueError):
    pass


def image_deps(image_dir: Path) -> list[str]:
    """agentd images ``image_dir``'s Dockerfile builds FROM (``agentd-sandbox-NAME``)."""
    try:
        text = (image_dir / "Dockerfile").read_text()
    except OSError:
        return []
    return list(dict.fromkeys(_FROM_AGENTD.findall(text)))


def _dir_digest(d: Path) -> bytes:
    h = hashlib.sha256()
    for path, rel in _context_files(d):
        if not path.is_file():
            continue
        stat = path.stat()
        h.update(rel.encode())
        # Content for small files; size and mtime for big ones (fast on large contexts).
        h.update(path.read_bytes() if stat.st_size <= _SMALL_FILE else f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    return h.digest()


def image_fingerprint(image_dir: Path, ids: tuple[int, int], dep_fps: list[str]) -> str:
    """Changes whenever the image would build differently: its files, the
    agent layer, the agent's ids, or any agentd base image it builds FROM."""
    h = hashlib.sha256(_dir_digest(image_dir))
    h.update(f"{WRAPPER_VERSION}:{ids[0]}:{ids[1]}:{','.join(dep_fps)}".encode())
    return h.hexdigest()[:16]


def resolve_images(image: str, image_dir: str | Path | None, state: dict) -> list[tuple[str, Path]]:
    """``image`` and the agentd images it builds FROM, bases first, with their
    source directories: an explicit ``image_dir``, else the directory it was
    last built from, else agentd's built-in ``images/NAME``."""
    recorded = {name: rec.get("dir") for name, rec in (state.get("rootfs") or {}).items() if isinstance(rec, dict)}

    def source(name: str) -> Path:
        if name == image and image_dir is not None:
            return Path(image_dir).expanduser().resolve()
        if recorded.get(name):
            return Path(recorded[name])
        if (IMAGES_DIR / name).is_dir():
            return IMAGES_DIR / name
        raise ImageError(f"don't know how to build image {name!r}: pass --image-dir DIR "
                         f"(a directory with a Dockerfile) the first time")

    order: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if name in stack:
            raise ImageError(f"images build FROM each other in a cycle: {' -> '.join(stack + (name,))}")
        if name in seen:
            return
        if not _IMAGE_NAME.match(name):
            raise ImageError(f"image names are lowercase letters, digits, '.', '_' and '-'; got {name!r}")
        d = source(name)
        for dep in image_deps(d):
            visit(dep, stack + (name,))
        seen.add(name)
        order.append((name, d))

    visit(image, ())
    return order


def _image_issues(image: str, image_dir, state: dict, ids: tuple[int, int] | None) -> tuple[list[str], list[str], dict]:
    """(issues, rootfs steps needed, info) for ``image`` and its bases."""
    try:
        order = resolve_images(image, image_dir, state)
    except ImageError as e:
        return [str(e)], [], {}
    issues, needs, info, fps = [], [], {}, {}
    for name, d in order:
        info[name] = {"dir": str(d), "deps": image_deps(d)}
        if not (d / "Dockerfile").is_file():
            issues.append(f"image {name!r}: {d} has no Dockerfile")
            continue
        rec = (state.get("rootfs") or {}).get(name)
        if ids is not None:
            fps[name] = image_fingerprint(d, ids, [fps.get(dep, "?") for dep in info[name]["deps"]])
            info[name]["fp"] = fps[name]
            if isinstance(rec, dict) and rec.get("fp") == fps[name]:
                continue
        issues.append(f"the {name!r} image in the VM is missing or out of date")
        needs.append(f"rootfs:{name}")
    return issues, needs, info


# Where python3 is in each built image, per (profile, image) (from the VM state).
_IMAGE_PYTHON: dict[tuple[str, str], str] = {}


def image_python(profile: str, image: str) -> str | None:
    return _IMAGE_PYTHON.get((profile, image))


# The VM user's (uid, gid) per profile. Inside the VM, files shared from the
# host carry these ids, and libkrun's file server runs as this user, so the
# sandbox's agent user must have exactly them to create files.
_VM_IDS: dict[str, tuple[int, int]] = {}


def vm_ids(profile: str) -> tuple[int, int] | None:
    return _VM_IDS.get(profile)


# --------------------------------------------------------------------------- #
# Running things
# --------------------------------------------------------------------------- #

Runner = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], *, input: str | None = None, timeout: float | None = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)


def ssh_argv(profile: str, *argv: str) -> list[str]:
    return ["colima", "ssh", "-p", profile, "--", *argv]


def vm_sh(profile: str, script: str, *, runner: Runner | None = None, timeout: float | None = 60) -> subprocess.CompletedProcess:
    """Run a shell script in the VM (sent on stdin, so no quoting through ssh)."""
    return (runner or _run)(ssh_argv(profile, "sh", "-s"), input=script, timeout=timeout)


# --------------------------------------------------------------------------- #
# Status (read-only)
# --------------------------------------------------------------------------- #

@dataclass
class Status:
    profile: str
    issues: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)  # setup step keys, in order
    info: dict = field(default_factory=dict)
    blocked: bool = False  # cannot be fixed by setup (unsupported host, no colima)

    @property
    def ready(self) -> bool:
        return not self.issues

    def report(self) -> str:
        if self.ready:
            return f"Colima profile {self.profile!r} is ready for libkrun sandboxes."
        lines = [f"Colima profile {self.profile!r} is not ready for libkrun sandboxes:"]
        lines += [f"  - {issue}" for issue in self.issues]
        if not self.blocked:
            lines.append(f"Run `agentd-sandbox colima setup --profile {self.profile}` to fix this "
                         "(it shows the plan and asks before changing anything).")
        return "\n".join(lines)


def _host_issues() -> list[str]:
    issues = []
    if sys.platform != "darwin" or platform.machine() != "arm64":
        issues.append("Colima nested virtualization needs an Apple Silicon Mac")
        return issues
    try:
        brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout
        m = re.search(r"Apple M(\d+)", brand)
        if m and int(m.group(1)) < 3:
            issues.append(f"nested virtualization needs an Apple M3 or later (this is {brand.strip()})")
    except OSError:
        pass
    major = int(platform.mac_ver()[0].split(".")[0] or 0)
    if major and major < 15:
        issues.append(f"nested virtualization needs macOS 15 or later (this is {platform.mac_ver()[0]})")
    if shutil.which("colima") is None:
        issues.append("colima is not installed (brew install colima)")
    return issues


def _profile_config(profile: str) -> dict:
    import yaml

    path = Path.home() / ".colima" / profile / "colima.yaml"
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _profile_listing(profile: str, runner: Runner) -> dict | None:
    r = runner(["colima", "list", "--json"])
    for line in (r.stdout or "").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("name") == profile:
            return entry
    return None


_PROBE = r"""
kvm=no; kvm_rw=no
[ -e /dev/kvm ] && kvm=yes
[ -r /dev/kvm ] && [ -w /dev/kvm ] && kvm_rw=yes
in_kvm_group=no; id -nG | tr ' ' '\n' | grep -qx kvm && in_kvm_group=yes
libkrun=no; ldconfig -p 2>/dev/null | grep -q 'libkrun.so.1 ' && libkrun=yes
libkrunfw=no; ldconfig -p 2>/dev/null | grep -q 'libkrunfw.so.5 ' && libkrunfw=yes
thp=$(sed -n 's/.*\[\(.*\)\].*/\1/p' /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null)
[ -f /etc/tmpfiles.d/agentd-thp.conf ] || thp="$thp-not-persisted"
nofile=$(ulimit -Hn); [ -f %(limits)s ] && [ -f %(systemd_limits)s ] || nofile="$nofile-not-configured"
state=$(cat %(state)s 2>/dev/null || echo '{}')
printf '{"kvm":"%%s","kvm_rw":"%%s","in_kvm_group":"%%s","libkrun":"%%s","libkrunfw":"%%s","python3":"%%s","launcher":"%%s","uid":%%s,"gid":%%s,"thp":"%%s","nofile":"%%s","state":%%s}\n' \
  "$kvm" "$kvm_rw" "$in_kvm_group" "$libkrun" "$libkrunfw" "$(command -v python3 >/dev/null && echo yes || echo no)" \
  "$([ -x %(launcher)s ] && echo yes || echo no)" "$(id -u)" "$(id -g)" "$thp" "$nofile" "$state"
""" % {"state": VM_STATE, "launcher": VM_LAUNCHER, "limits": "/etc/security/limits.d/99-agentd.conf",
       "systemd_limits": "/etc/systemd/system.conf.d/99-agentd-nofile.conf"}


def status(
    profile: str = PROFILE,
    *,
    cpus: int = 2,
    mem_mib: int = 2048,
    image: str = "agents",
    image_dir: str | Path | None = None,
    runner: Runner | None = None,
) -> Status:
    """Everything a libkrun-in-Colima sandbox needs, checked without changing anything.

    ``image`` is the sandbox image to check (and the agentd images it builds
    FROM); ``image_dir`` its Dockerfile directory if not built before.

    ``cpus`` / ``mem_mib`` are what one sandbox uses; the VM must have at
    least that plus room for its own Linux."""
    runner = runner or _run
    st = Status(profile)
    host = _host_issues()
    if host:
        st.issues += host
        st.blocked = True
        return st

    listing = _profile_listing(profile, runner)
    config = _profile_config(profile)
    st.info.update(listing=listing, config={k: config.get(k) for k in ("vmType", "nestedVirtualization", "cpu", "memory", "disk")})
    if listing is None:
        st.issues.append(f"there is no Colima profile {profile!r}")
        st.needs += ["create_vm", "kvm_access", "hugepages", "file_limits", "build_deps", "libkrunfw", "libkrun", "launcher"]
        issues, needs, info = _image_issues(image, image_dir, {}, None)
        st.issues += [i for i in issues if "missing or out of date" not in i]
        st.needs += needs
        st.info["images"] = info
        return st

    need_recreate = config.get("vmType") not in (None, "vz") or not config.get("nestedVirtualization")
    disk_gib = (listing.get("disk") or 0) / 2**30
    need_mib = mem_mib + VM_OVERHEAD_MIB
    small = (listing.get("cpus") or 0) < cpus or (listing.get("memory") or 0) / 2**20 < need_mib
    if config.get("vmType") not in (None, "vz"):
        st.issues.append(f"profile {profile!r} uses vmType {config.get('vmType')!r}; nested virtualization needs 'vz'")
    if not config.get("nestedVirtualization"):
        st.issues.append(f"profile {profile!r} was created without nested virtualization (no /dev/kvm inside)")
    if small:
        st.issues.append(
            f"profile {profile!r} has {listing.get('cpus')} CPUs / {(listing.get('memory') or 0) / 2**30:.0f} GiB; "
            f"a sandbox needs at least {cpus} CPUs / {need_mib / 1024:.1f} GiB")
    if disk_gib and disk_gib < MIN_DISK_GIB:
        st.issues.append(f"profile {profile!r} has a {disk_gib:.0f} GiB disk; at least {MIN_DISK_GIB} GiB is needed")
    if need_recreate:
        st.needs.append("recreate_vm")
        st.needs += ["kvm_access", "hugepages", "file_limits", "build_deps", "libkrunfw", "libkrun", "launcher"]
        issues, needs, info = _image_issues(image, image_dir, {}, None)
        st.issues += [i for i in issues if "missing or out of date" not in i]
        st.needs += needs
        st.info["images"] = info
        return st
    if small or (disk_gib and disk_gib < MIN_DISK_GIB):
        st.needs.append("resize_vm")
    if listing.get("status") != "Running":
        st.issues.append(f"profile {profile!r} is not running")
        if "resize_vm" not in st.needs:
            st.needs.append("start_vm")
        st.needs += ["kvm_access", "hugepages", "file_limits", "build_deps", "libkrunfw", "libkrun", "launcher"]
        issues, needs, info = _image_issues(image, image_dir, {}, None)
        st.issues += [i for i in issues if "missing or out of date" not in i]
        st.needs += needs
        st.info["images"] = info
        return st

    r = vm_sh(profile, _PROBE, runner=runner)
    try:
        probe = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        st.issues.append(f"could not inspect the VM: {(r.stderr or r.stdout or '').strip()[:300]}")
        return st
    st.info["vm"] = probe
    ids = (int(probe["uid"]), int(probe["gid"]))
    _VM_IDS[profile] = ids
    state = probe.get("state") or {}
    if probe["kvm"] != "yes":
        st.issues.append("/dev/kvm is missing inside the VM (nested virtualization is not active)")
        st.needs.insert(0, "recreate_vm")
    elif probe["kvm_rw"] != "yes":
        st.issues.append("the VM user cannot open /dev/kvm")
        st.needs.append("kvm_access")
    if probe.get("thp") != "always":  # set now *and* persisted across VM restarts
        # Without huge pages every microVM page is faulted in 4 KiB at a time
        # through two levels of virtualization: boots take ~4-5 s per GiB of
        # sandbox memory and every process start is slow. With them: ~1 s.
        st.issues.append("transparent huge pages are not 'always' in the VM "
                         "(sandbox boots take ~5 s per GiB of memory without them)")
        st.needs.append("hugepages")
    if probe.get("nofile") != str(NOFILE):
        # libkrun's file server keeps a descriptor per open file in shared dirs.
        st.issues.append(f"the VM's open files limit is not raised to {NOFILE} for sessions and services")
        st.needs.append("file_limits")
    if probe["python3"] != "yes":
        st.issues.append("python3 is missing inside the VM")
        st.needs.append("build_deps")
    if probe["libkrunfw"] != "yes" or state.get("libkrunfw") != LIBKRUNFW_VERSION:
        st.issues.append(f"libkrunfw {LIBKRUNFW_VERSION} is not installed in the VM")
        st.needs += ["build_deps", "libkrunfw"]
    if probe["libkrun"] != "yes" or state.get("libkrun") != LIBKRUN_BUILD:
        st.issues.append(f"libkrun {LIBKRUN_VERSION} (with networking) is not installed in the VM")
        st.needs += ["build_deps", "libkrun"]
    if probe["launcher"] != "yes" or state.get("launcher") != launcher_sha():
        st.issues.append("the agentd-krun launcher in the VM is missing or out of date")
        st.needs.append("launcher")
    issues, needs, info = _image_issues(image, image_dir, state, ids)
    st.issues += issues
    st.needs += needs
    st.info["images"] = info
    for name, rec in (state.get("rootfs") or {}).items():
        if isinstance(rec, dict) and rec.get("python"):
            _IMAGE_PYTHON[(profile, name)] = rec["python"]
    st.needs = list(dict.fromkeys(st.needs))  # dedupe, keep order
    return st


_READY: dict[tuple, bool] = {}


def ensure_ready(profile: str, *, cpus: int, mem_mib: int, image: str = "agents") -> None:
    """Raise :class:`ColimaNotReady` unless the VM can run sandboxes (checked once per process)."""
    key = (profile, cpus, mem_mib, image)
    if _READY.get(key):
        return
    st = status(profile, cpus=cpus, mem_mib=mem_mib, image=image)
    if not st.ready:
        raise ColimaNotReady(st)
    _READY[key] = True


class ColimaNotReady(RuntimeError):
    def __init__(self, st: Status):
        super().__init__(st.report())
        self.status = st


# --------------------------------------------------------------------------- #
# Setup (only with approval)
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    key: str
    description: str
    commands: list[str]  # shown to the user; host commands, or VM scripts prefixed "[vm] "
    destructive: bool = False
    run: Callable[[], None] | None = None


def _install_state(profile: str, updates: dict, runner: Runner | None) -> None:
    script = (
        f"sudo mkdir -p {shlex.quote(str(Path(VM_STATE).parent))} && "
        f"python3 - <<'EOF'\n"
        f"import json\n"
        f"p = {VM_STATE!r}\n"
        f"try:\n    s = json.load(open(p))\nexcept Exception:\n    s = {{}}\n"
        f"u = json.loads({json.dumps(json.dumps(updates))})\n"
        f"for k, v in u.items():\n"
        f"    if isinstance(v, dict):\n        s.setdefault(k, {{}}).update(v)\n    else:\n        s[k] = v\n"
        f"open('/tmp/agentd-state.json', 'w').write(json.dumps(s))\n"
        f"EOF\n"
        f"sudo mv /tmp/agentd-state.json {VM_STATE}"
    )
    _check(vm_sh(profile, script, runner=runner), "recording install state")


def _docker_context(runner: Runner) -> str | None:
    if shutil.which("docker") is None:
        return None
    r = runner(["docker", "context", "show"])
    return (r.stdout or "").strip() or None if r.returncode == 0 else None


def _probe_ids(profile: str, runner: Runner | None) -> tuple[int, int]:
    r = vm_sh(profile, "echo $(id -u) $(id -g)", runner=runner)
    _check(r, "reading the VM user's ids")
    uid, gid = (int(x) for x in r.stdout.split()[:2])
    _VM_IDS[profile] = (uid, gid)
    return uid, gid


def _check(r: subprocess.CompletedProcess, what: str) -> None:
    if r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip()[-2000:]
        raise RuntimeError(f"{what} failed (exit {r.returncode}):\n{tail}")


def plan(st: Status, *, cpus: int = DEFAULT_CPUS, memory_gib: int = DEFAULT_MEMORY_GIB,
         disk_gib: int = DEFAULT_DISK_GIB, image: str = "agents", runner: Runner | None = None) -> list[Step]:
    """The steps :func:`setup` would run for ``st``, without running anything."""
    runner = runner or _run
    p = st.profile
    q = shlex.quote
    create = ["colima", "start", p, "--vm-type", "vz", "--nested-virtualization", "--cpus", str(cpus),
              "--memory", str(memory_gib), "--disk", str(disk_gib), "--mount-type", "virtiofs", "--runtime", "docker"]
    vm = lambda script: f"[vm] {script}"  # noqa: E731

    def host(argv, what, timeout=900):
        def do():
            _check(runner(argv, timeout=timeout), what)
        return do

    def in_vm(script, what, timeout=3600):
        def do():
            _check(vm_sh(p, script, runner=runner, timeout=timeout), what)
        return do

    steps: dict[str, Step] = {}
    steps["create_vm"] = Step("create_vm", f"Create Colima VM {p!r} with nested virtualization "
                              f"({cpus} CPUs, {memory_gib} GiB memory, {disk_gib} GiB disk)",
                              [" ".join(create)], run=host(create, "creating the VM"))

    def recreate():
        _check(runner(["colima", "delete", p, "--force"], timeout=300), "deleting the VM")
        _check(runner(create, timeout=900), "creating the VM")
    steps["recreate_vm"] = Step(
        "recreate_vm",
        f"DELETE Colima VM {p!r} (everything in it: containers, images, volumes) and recreate it "
        f"with nested virtualization ({cpus} CPUs, {memory_gib} GiB, {disk_gib} GiB disk)",
        [f"colima delete {p} --force", " ".join(create)], destructive=True, run=recreate)
    current_disk = int(((st.info.get("listing") or {}).get("disk") or 0) / 2**30)
    resize = ["colima", "start", p, "--cpus", str(cpus), "--memory", str(memory_gib),
              "--disk", str(max(disk_gib, current_disk))]  # disks can grow, never shrink

    def do_resize():
        _check(runner(["colima", "stop", p], timeout=300), "stopping the VM")
        _check(runner(resize, timeout=900), "restarting the VM")
    steps["resize_vm"] = Step("resize_vm", f"Restart Colima VM {p!r} with {cpus} CPUs / {memory_gib} GiB / "
                              f"{disk_gib} GiB disk (stops anything running in it)",
                              [f"colima stop {p}", " ".join(resize)], run=do_resize)
    steps["start_vm"] = Step("start_vm", f"Start Colima VM {p!r}", [f"colima start {p}"],
                             run=host(["colima", "start", p], "starting the VM"))

    # The VM is dedicated to agentd, so /dev/kvm is opened up to its user, persistently
    # (udev rule) and now (chmod); a group change would not reach Colima's
    # long-lived ssh connection.
    kvm = ("echo 'KERNEL==\"kvm\", MODE=\"0666\"' | sudo tee /etc/udev/rules.d/99-agentd-kvm.rules >/dev/null && "
           "sudo chmod 666 /dev/kvm")
    steps["kvm_access"] = Step("kvm_access", "Let the VM user open /dev/kvm (udev rule, mode 0666)", [vm(kvm)],
                               run=in_vm(kvm, "granting /dev/kvm access"))
    thp = ("echo 'w /sys/kernel/mm/transparent_hugepage/enabled - - - - always' | "
           "sudo tee /etc/tmpfiles.d/agentd-thp.conf >/dev/null && "
           "echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled >/dev/null")
    steps["hugepages"] = Step("hugepages", "Back microVM memory with transparent huge pages "
                              "(THP 'always', persisted with tmpfiles.d): ~1 s sandbox boots instead of ~5 s per GiB",
                              [vm(thp)], run=in_vm(thp, "enabling transparent huge pages"))
    limits = (f"printf '* soft nofile {NOFILE}\\n* hard nofile {NOFILE}\\n' | "
              "sudo tee /etc/security/limits.d/99-agentd.conf >/dev/null && "
              "sudo mkdir -p /etc/systemd/system.conf.d && "
              f"printf '[Manager]\\nDefaultLimitNOFILE={NOFILE}:{NOFILE}\\n' | "
              "sudo tee /etc/systemd/system.conf.d/99-agentd-nofile.conf >/dev/null && "
              "sudo systemctl daemon-reexec")
    steps["file_limits"] = Step("file_limits", f"Raise the open files limit in the VM to {NOFILE} "
                                "(limits.d for sessions, systemd DefaultLimitNOFILE for services)",
                                [vm(limits)], run=in_vm(limits, "raising the open files limit"))
    deps = ("sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "build-essential curl ca-certificates patchelf pkg-config libclang-dev python3 && "
            "{ command -v cargo >/dev/null || [ -x ~/.cargo/bin/cargo ] || "
            "curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal; }")
    steps["build_deps"] = Step("build_deps", "Install build tools in the VM (apt: build-essential, patchelf, ...; Rust via rustup)",
                               [vm(deps)], run=in_vm(deps, "installing build tools"))
    fw = (f"set -e; cd /tmp; curl -fsSL -o libkrunfw.tgz {q(LIBKRUNFW_URL)}; "
          f"echo '{LIBKRUNFW_SHA256}  libkrunfw.tgz' | sha256sum -c -; "
          f"sudo tar -xzf libkrunfw.tgz -C /usr/local; sudo ldconfig; rm libkrunfw.tgz")

    def do_fw():
        in_vm(fw, "installing libkrunfw")()
        _install_state(p, {"libkrunfw": LIBKRUNFW_VERSION}, runner)
    steps["libkrunfw"] = Step("libkrunfw", f"Install libkrunfw {LIBKRUNFW_VERSION} (prebuilt, sha256-pinned)",
                              [vm(fw)], run=do_fw)
    krun = (f"set -e; export PATH=$HOME/.cargo/bin:$PATH; rm -rf /tmp/libkrun-build; mkdir -p /tmp/libkrun-build; "
            f"cd /tmp/libkrun-build; curl -fsSL -o src.tgz {q(LIBKRUN_URL)}; "
            f"echo '{LIBKRUN_SHA256}  src.tgz' | sha256sum -c -; tar -xzf src.tgz; cd libkrun-{LIBKRUN_VERSION}; "
            f"make NET=1 -j$(nproc); sudo make install PREFIX=/usr/local; "
            f"echo /usr/local/lib64 | sudo tee /etc/ld.so.conf.d/agentd-libkrun.conf >/dev/null; sudo ldconfig; "
            f"rm -rf /tmp/libkrun-build")

    def do_krun():
        in_vm(krun, "building libkrun")()
        _install_state(p, {"libkrun": LIBKRUN_BUILD}, runner)
    steps["libkrun"] = Step("libkrun", f"Build and install libkrun {LIBKRUN_VERSION} from source (sha256-pinned)",
                            [vm(krun)], run=do_krun)
    launcher_src = HERE / "launcher.c"
    build_launcher = (f"set -e; sudo mkdir -p {q(str(Path(VM_LAUNCHER).parent))}; "
                      f"cc -O2 -Wall -o /tmp/agentd-krun {q(str(launcher_src))} -I/usr/local/include "
                      f"-L/usr/local/lib64 -lkrun -Wl,-rpath,/usr/local/lib64; "
                      f"sudo install -m 755 /tmp/agentd-krun {VM_LAUNCHER}; rm /tmp/agentd-krun")

    def do_launcher():
        in_vm(build_launcher, "building the launcher")()
        _install_state(p, {"launcher": launcher_sha()}, runner)
    steps["launcher"] = Step("launcher", f"Build the agentd-krun launcher in the VM ({VM_LAUNCHER})",
                             [vm(build_launcher)], run=do_launcher)
    images = st.info.get("images") or {}
    for need in st.needs:
        if need.startswith("rootfs:"):
            name = need.split(":", 1)[1]
            steps[need] = _image_step(p, name, images, runner, vm)
    return [steps[k] for k in st.needs if k in steps]


def _context_files(d: Path):
    """The build context: everything under ``d`` but VCS and bytecode dirs
    (docker build still applies the directory's .dockerignore)."""
    for path in sorted(d.rglob("*")):
        rel = path.relative_to(d)
        if any(part in (".git", "__pycache__") for part in rel.parts):
            continue
        yield path, rel.as_posix()


def _stream_context(profile: str, script: str, src_dir: Path, timeout: float = 3600) -> subprocess.CompletedProcess:
    """Run ``script`` in the VM with ``src_dir`` as a tar stream on its stdin.

    Streaming means the directory needn't be shared with the VM (Colima only
    shares $HOME by default, and agentd itself may be installed elsewhere)."""
    import tarfile
    import tempfile

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(ssh_argv(profile, "sh", "-c", script), stdin=subprocess.PIPE, stdout=out, stderr=err)
        try:
            with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                for path, arcname in _context_files(src_dir):
                    tar.add(path, arcname=arcname, recursive=False)
        except BrokenPipeError:
            pass  # the script failed early; its exit status says why
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        code = proc.wait(timeout=timeout)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(proc.args, code, out.read().decode(errors="replace"),
                                           err.read().decode(errors="replace"))


def _image_step(profile: str, name: str, images: dict, runner: Runner | None, vm) -> Step:
    q = shlex.quote
    src_dir = Path(images[name]["dir"])
    dest = vm_rootfs(name)
    # Runs in the VM, with the build context as a tar stream on stdin. The
    # agent user gets the VM user's ids (see _VM_IDS).
    script = (
        "set -e; ctx=$(mktemp -d); trap 'rm -rf \"$ctx\"' EXIT; tar -x -C \"$ctx\"; "
        "ids=\"--build-arg AGENT_UID=$(id -u) --build-arg AGENT_GID=$(id -g)\"; "
        f"docker build -q -t agentd-src-{name} $ids \"$ctx\" >/dev/null; "
        f"printf '%s' {q(_WRAPPER_DOCKERFILE)} | docker build -q -t agentd-sandbox-{name} "
        f"--build-arg BASE=agentd-src-{name} $ids - >/dev/null; "
        f"py=$(docker run --rm --network none --entrypoint sh agentd-sandbox-{name} -c {q(_IMAGE_CHECK)}) "
        f"|| {{ echo 'image {name} must provide python3 >= 3.9 and bash' >&2; exit 1; }}; "
        f"cid=$(docker create agentd-sandbox-{name}); sudo rm -rf {dest}.tmp; sudo mkdir -p {dest}.tmp; "
        f"docker export \"$cid\" | sudo tar -x -C {dest}.tmp; docker rm \"$cid\" >/dev/null; "
        f"sudo rm -rf {dest}; sudo mv {dest}.tmp {dest}; echo \"AGENTD_PYTHON=$py\""
    )

    def run() -> None:
        r = _stream_context(profile, script, src_dir)
        _check(r, f"building image {name!r}")
        python = next((line.split("=", 1)[1] for line in (r.stdout or "").splitlines()
                       if line.startswith("AGENTD_PYTHON=")), "/usr/local/bin/python3")
        ids = _VM_IDS.get(profile) or _probe_ids(profile, runner)
        # Bases were built (and recorded) earlier in this run, in dependency order.
        dep_fps = [images[dep].get("fp") or _recorded_fp(profile, dep, runner) for dep in images[name]["deps"]]
        fp = image_fingerprint(src_dir, ids, dep_fps)
        images[name]["fp"] = fp
        _IMAGE_PYTHON[(profile, name)] = python
        _install_state(profile, {"rootfs": {name: {"fp": fp, "dir": str(src_dir), "python": python}}}, runner)

    summary = [
        vm(f"docker build -t agentd-src-{name} {src_dir}   (sent to the VM as a tar stream; "
           "AGENT_UID/AGENT_GID = the VM user's ids)"),
        vm(f"docker build -t agentd-sandbox-{name}   (adds the 'agent' user with those ids)"),
        vm(f"check it has python3 >= 3.9 and bash, then export it to {dest}"),
    ]
    return Step(f"rootfs:{name}", f"Build image {name!r} from {src_dir} onto the VM's disk ({dest})",
                summary, run=run)


def _recorded_fp(profile: str, name: str, runner: Runner | None) -> str:
    return ((vm_state(profile, runner).get("rootfs") or {}).get(name) or {}).get("fp", "?")


def vm_state(profile: str, runner: Runner | None = None) -> dict:
    """agentd's install record in the VM (versions, built images)."""
    r = vm_sh(profile, f"cat {VM_STATE} 2>/dev/null || echo '{{}}'", runner=runner)
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


def setup(
    profile: str = PROFILE,
    *,
    cpus: int = DEFAULT_CPUS,
    memory_gib: int = DEFAULT_MEMORY_GIB,
    disk_gib: int = DEFAULT_DISK_GIB,
    image: str = "agents",
    image_dir: str | Path | None = None,
    approve: Callable[[list[Step]], bool] | None = None,
    log: Callable[[str], None] = print,
    runner: Runner | None = None,
) -> Status:
    """Bring ``profile`` to ready, after ``approve(steps)`` says yes.

    Nothing runs without approval; ``approve`` defaults to asking on the
    terminal, with a separate confirmation for destructive steps. Steps are
    idempotent, so setup can be re-run after a failure."""
    runner = runner or _run
    st = status(profile, image=image, image_dir=image_dir, runner=runner)
    if st.ready:
        log(st.report())
        return st
    if st.blocked:
        raise ColimaNotReady(st)
    steps = plan(st, cpus=cpus, memory_gib=memory_gib, disk_gib=disk_gib, image=image, runner=runner)
    approve = approve or (lambda steps: _ask_on_terminal(steps, profile))
    if not approve(steps):
        log("Nothing was changed.")
        return st
    # Starting a Colima VM makes it the active Docker context; put back the
    # one the user had, so `docker` keeps pointing where it did.
    context = _docker_context(runner)
    try:
        for step in steps:
            log(f"==> {step.description}")
            step.run()
    finally:
        if context and _docker_context(runner) != context:
            runner(["docker", "context", "use", context])
            log(f"(restored the Docker context to {context!r})")
    _READY.clear()
    final = status(profile, image=image, image_dir=image_dir, runner=runner)
    log(final.report())
    return final


def _ask_on_terminal(steps: list[Step], profile: str) -> bool:
    print("agentd will make these changes:\n")
    for i, step in enumerate(steps, 1):
        print(f"{i}. {step.description}")
        for command in step.commands:
            print(f"     {command if len(command) < 160 else command[:157] + '...'}")
    print()
    if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        return False
    destructive = [s for s in steps if s.destructive]
    if destructive:
        for step in destructive:
            print(f"!! {step.description}")
        if input(f"This deletes Colima VM {profile!r}. Type its name to confirm: ").strip() != profile:
            return False
    return True
