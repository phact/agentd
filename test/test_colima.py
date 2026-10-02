"""
libkrun-in-Colima support (agentd.sandbox.colima, colima_relay), without a real VM:
readiness detection, approval-gated setup, and the in-VM relay (run locally
against a fake launcher). Set AGENTD_LIVE=1 with a ready Colima profile to
also boot a real sandbox there.
"""
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from agentd.sandbox import colima

GIB = 2**30


class FakeColima:
    """Answers `colima list --json` and the VM probe; records every command."""

    def __init__(self, listing=None, probe=None):
        self.listing, self.probe, self.calls = listing, probe, []

    def __call__(self, argv, input=None, timeout=None):
        self.calls.append((argv, input))
        out = ""
        if argv[:3] == ["colima", "list", "--json"]:
            out = json.dumps(self.listing) + "\n" if self.listing else ""
        elif argv[:2] == ["colima", "ssh"] and input and "kvm_rw" in input:
            out = json.dumps(self.probe) + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def changes(self):
        """Commands other than the read-only list/probe."""
        return [c for c in self.calls if c[0][:3] != ["colima", "list", "--json"]
                and not (c[1] and "kvm_rw" in c[1])]


def _ready_probe():
    return {"kvm": "yes", "kvm_rw": "yes", "in_kvm_group": "no", "libkrun": "yes", "libkrunfw": "yes",
            "python3": "yes", "launcher": "yes", "uid": 501, "gid": 1000, "thp": "always", "nofile": str(colima.NOFILE),
            "state": {"libkrun": colima.LIBKRUN_BUILD, "libkrunfw": colima.LIBKRUNFW_VERSION,
                      "launcher": colima.launcher_sha(),
                      "rootfs": {"agents": {"fp": colima.image_fingerprint(colima.IMAGES_DIR / "agents", (501, 1000), []),
                                            "dir": str(colima.IMAGES_DIR / "agents"), "python": "/usr/local/bin/python3"}}}}


@pytest.fixture
def host_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(colima, "_host_issues", lambda: [])
    monkeypatch.setattr(colima.Path, "home", classmethod(lambda cls: tmp_path))

    def config(profile, **cfg):
        d = tmp_path / ".colima" / profile
        d.mkdir(parents=True, exist_ok=True)
        (d / "colima.yaml").write_text("\n".join(f"{k}: {json.dumps(v)}" for k, v in cfg.items()))
    return config


def _listing(status="Running", cpus=4, mem_gib=8, disk_gib=60):
    return {"name": "agentd", "status": status, "cpus": cpus, "memory": mem_gib * GIB, "disk": disk_gib * GIB}


def test_missing_profile_plans_a_new_vm(host_ok):
    st = colima.status("agentd", runner=FakeColima(listing=None))
    assert not st.ready and st.needs[0] == "create_vm"
    steps = colima.plan(st)
    assert not any(s.destructive for s in steps)
    assert "--nested-virtualization" in steps[0].commands[0]


def test_vm_without_nested_virtualization_must_be_recreated(host_ok):
    host_ok("agentd", vmType="vz", nestedVirtualization=False)
    st = colima.status("agentd", runner=FakeColima(listing=_listing()))
    assert any("without nested virtualization" in i for i in st.issues)
    steps = colima.plan(st)
    assert steps[0].key == "recreate_vm" and steps[0].destructive


def test_small_vm_is_resized_not_recreated(host_ok):
    host_ok("agentd", vmType="vz", nestedVirtualization=True)
    st = colima.status("agentd", cpus=2, mem_mib=2048, runner=FakeColima(listing=_listing(cpus=2, mem_gib=2),
                                                                           probe=_ready_probe()))
    assert any("needs at least" in i for i in st.issues)
    assert "resize_vm" in st.needs and "recreate_vm" not in st.needs


def test_ready_vm_and_stale_components(host_ok):
    host_ok("agentd", vmType="vz", nestedVirtualization=True)
    assert colima.status("agentd", runner=FakeColima(listing=_listing(), probe=_ready_probe())).ready

    stale = _ready_probe()
    stale["state"]["launcher"] = "old"
    stale["kvm_rw"] = "no"
    stale["thp"] = "madvise"
    stale["nofile"] = "1048576-not-configured"
    st = colima.status("agentd", runner=FakeColima(listing=_listing(), probe=stale))
    assert {"launcher", "kvm_access", "hugepages", "file_limits"} <= set(st.needs) and not st.ready


def test_stopped_vm_is_started(host_ok):
    host_ok("agentd", vmType="vz", nestedVirtualization=True)
    st = colima.status("agentd", runner=FakeColima(listing=_listing(status="Stopped")))
    assert st.needs[0] == "start_vm"


def test_setup_changes_nothing_without_approval(host_ok):
    host_ok("agentd", vmType="vz", nestedVirtualization=False)
    fake = FakeColima(listing=_listing())
    seen = []
    colima.setup("agentd", runner=fake, approve=lambda steps: seen.extend(steps) or False, log=lambda m: None)
    assert seen and fake.changes() == [], "a refused plan must not run anything"


def test_terminal_approval_requires_typing_the_profile_for_destructive_steps(host_ok, monkeypatch):
    host_ok("agentd", vmType="vz", nestedVirtualization=False)
    steps = colima.plan(colima.status("agentd", runner=FakeColima(listing=_listing())))
    answers = iter(["y", "wrong-name"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert colima._ask_on_terminal(steps, "agentd") is False
    answers = iter(["y", "agentd"])
    assert colima._ask_on_terminal(steps, "agentd") is True


def test_cli_yes_does_not_cover_destructive_steps(host_ok, monkeypatch, capsys):
    from agentd.sandbox import cli

    host_ok("agentd", vmType="vz", nestedVirtualization=False)
    fake = FakeColima(listing=_listing())
    monkeypatch.setattr(colima, "_run", fake)
    assert cli.main(["colima", "setup", "--yes"]) == 1
    assert "--recreate" in capsys.readouterr().err
    assert fake.changes() == [], "deleting a VM needs --recreate on top of --yes"


# --------------------------------------------------------------------------- #
# Custom images
# --------------------------------------------------------------------------- #

def _image(tmp_path, name, dockerfile):
    d = tmp_path / name
    d.mkdir()
    (d / "Dockerfile").write_text(dockerfile)
    return d


def test_image_deps_and_fingerprints(tmp_path):
    d = _image(tmp_path, "rosey", "FROM agentd-sandbox-agents\nRUN true\n# FROM agentd-sandbox-nope\n")
    assert colima.image_deps(d) == ["agents"]
    assert colima.image_deps(_image(tmp_path, "plain", "FROM --platform=linux/arm64 debian:12\n")) == []
    fp = colima.image_fingerprint(d, (501, 1000), ["base1"])
    assert fp == colima.image_fingerprint(d, (501, 1000), ["base1"])
    assert fp != colima.image_fingerprint(d, (501, 20), ["base1"]), "the agent's ids are baked in"
    assert fp != colima.image_fingerprint(d, (501, 1000), ["base2"]), "a changed base changes the image"
    (d / "extra.txt").write_text("x")
    assert fp != colima.image_fingerprint(d, (501, 1000), ["base1"]), "a changed file changes the image"


def test_resolve_images_sources_and_order(tmp_path, monkeypatch):
    rosey = _image(tmp_path, "rosey", "FROM agentd-sandbox-agents\n")
    order = colima.resolve_images("rosey", rosey, {})
    assert [n for n, _ in order] == ["agents", "rosey"], "bases first"
    assert order[0][1] == colima.IMAGES_DIR / "agents" and order[1][1] == rosey.resolve()
    # Later, the recorded directory is used without --image-dir.
    state = {"rootfs": {"rosey": {"dir": str(rosey), "fp": "x"}}}
    assert colima.resolve_images("rosey", None, state)[-1][1] == rosey
    with pytest.raises(colima.ImageError, match="--image-dir"):
        colima.resolve_images("unknown", None, {})
    a = _image(tmp_path, "aa", "FROM agentd-sandbox-bb\n")
    _image(tmp_path, "bb", "FROM agentd-sandbox-aa\n")
    with pytest.raises(colima.ImageError, match="cycle"):
        colima.resolve_images("aa", a, {"rootfs": {"bb": {"dir": str(tmp_path / "bb")}}})
    with pytest.raises(colima.ImageError, match="lowercase"):
        colima.resolve_images("Bad Name", a, {})


def test_status_rebuilds_a_custom_image_and_its_stale_base(host_ok, monkeypatch):
    host_ok("agentd", vmType="vz", nestedVirtualization=True)
    home = colima.Path.home()  # host_ok points it at tmp_path
    rosey = _image(home, "rosey", "FROM agentd-sandbox-agents\nRUN true\n")
    probe = _ready_probe()
    st = colima.status("agentd", image="rosey", image_dir=rosey, runner=FakeColima(listing=_listing(), probe=probe))
    assert st.needs == ["rootfs:rosey"], "agents is current; only rosey needs building"
    steps = colima.plan(st)
    assert [s.key for s in steps] == ["rootfs:rosey"] and not steps[0].destructive

    probe["state"]["rootfs"]["agents"]["fp"] = "stale"
    st = colima.status("agentd", image="rosey", image_dir=rosey, runner=FakeColima(listing=_listing(), probe=probe))
    assert st.needs == ["rootfs:agents", "rootfs:rosey"], "a stale base is rebuilt first"

    missing = home / "nodockerfile"
    missing.mkdir()
    st = colima.status("agentd", image="nodockerfile", image_dir=missing, runner=FakeColima(listing=_listing(), probe=probe))
    assert any("has no Dockerfile" in i for i in st.issues)


@pytest.mark.skipif(subprocess.run(["docker", "info"], capture_output=True).returncode != 0, reason="needs docker")
@pytest.mark.parametrize("base,setup", [
    ("debian:12-slim", ""),                                                              # no agent user
    ("debian:12-slim", "RUN useradd -u 1234 -m agent"),                                  # agent with wrong ids
    ("alpine:3.20", "RUN apk add --no-cache shadow >/dev/null || true"),                 # busybox / shadow
])
def test_agent_layer_gives_agent_the_vm_ids(base, setup, tmp_path):
    """The generated layer works whether the image has an agent user or not."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Dockerfile").write_text(f"FROM {base}\n{setup}\n")
    tag = f"agentd-test-src-{os.getpid()}"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(src)], check=True, capture_output=True)
    try:
        r = subprocess.run(["docker", "build", "-q", "-t", tag + "-w", "--build-arg", f"BASE={tag}",
                            "--build-arg", "AGENT_UID=501", "--build-arg", "AGENT_GID=1000", "-"],
                           input=colima._WRAPPER_DOCKERFILE, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
        out = subprocess.run(["docker", "run", "--rm", tag + "-w", "sh", "-c", "id -u agent; id -g agent"],
                             capture_output=True, text=True).stdout.split()
        assert out == ["501", "1000"]
    finally:
        subprocess.run(["docker", "rmi", "-f", tag, tag + "-w"], capture_output=True)


def test_executor_image_selection():
    from agentd.sandbox.executor import DEFAULT_ROOTFS, KrunExecutor

    with KrunExecutor(colima=True, image="rosey") as ex:
        assert str(ex.rootfs) == "/var/lib/agentd/rootfs/rosey" and ex.colima == "agentd"
    with KrunExecutor(image="rosey") as ex:
        assert Path(ex.rootfs) == DEFAULT_ROOTFS.parent / "rosey"
    with pytest.raises(ValueError):
        KrunExecutor(rootfs="/x", image="y")


# --------------------------------------------------------------------------- #
# The in-VM relay, run locally against a fake launcher
# --------------------------------------------------------------------------- #

FAKE_LAUNCHER = textwrap.dedent('''
    import socket, struct, sys, os, time, json
    sock_path = sys.argv[1]
    time.sleep(0.3)  # like a VM booting: the socket appears later
    srv = socket.socket(socket.AF_UNIX); srv.bind(sock_path); srv.listen(4)
    open(sys.argv[2], "w").write(str(os.getpid()))
    while True:
        conn, _ = srv.accept()
        hello = json.dumps({"version": 1}).encode()
        conn.sendall(struct.pack(">IBI", 0, 1, len(hello)) + hello)
        while True:
            data = conn.recv(4096)
            if not data:
                break
            conn.sendall(data.upper())
''')


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_relay_forwards_hello_pumps_both_ways_and_kills_launcher_on_eof(tmp_path):
    relay = Path(colima.HERE / "colima_relay.py")
    launcher = tmp_path / "fake_launcher.py"
    launcher.write_text(FAKE_LAUNCHER)
    short = Path(tempfile.mkdtemp(prefix="relay-", dir="/tmp"))  # AF_UNIX paths are capped at 104 bytes
    sock, pidfile = short / "s.sock", tmp_path / "pid"
    proc = subprocess.Popen([sys.executable, str(relay), "--sock", str(sock), "--",
                             sys.executable, str(launcher), str(sock), str(pidfile)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    header = proc.stdout.read(9)
    assert len(header) == 9, proc.stderr.read().decode()
    stream_id, ftype, length = struct.unpack(">IBI", header)
    assert (stream_id, ftype) == (0, 1) and json.loads(proc.stdout.read(length)) == {"version": 1}
    proc.stdin.write(b"ping")
    proc.stdin.flush()
    assert proc.stdout.read(4) == b"PING"
    launcher_pid = int(pidfile.read_text())
    proc.stdin.close()  # the host side goes away
    proc.wait(timeout=10)
    time.sleep(0.2)
    assert not _alive(launcher_pid), "the relay must kill the launcher when the host disconnects"


def test_relay_rejects_unshared_paths(tmp_path):
    relay = Path(colima.HERE / "colima_relay.py")
    r = subprocess.run([sys.executable, str(relay), "--sock", str(tmp_path / "s"), "--require",
                        str(tmp_path / "missing" / "marker"), "--", "true"], capture_output=True, text=True)
    assert r.returncode == 3 and "is not shared with the Colima VM" in r.stderr


def test_krun_sandbox_in_colima_uses_vm_paths(tmp_path):
    from agentd.sandbox.krun import KrunSandbox

    sb = KrunSandbox(rootfs=colima.vm_rootfs("agents"), colima="agentd", read_only_mounts={"/opt/m": tmp_path})
    argv = sb._launcher_argv(tmp_path / "code", str(sb.rootfs))
    assert argv[0] == colima.VM_LAUNCHER
    assert argv[argv.index("--root") + 1] == "/var/lib/agentd/rootfs/agents"
    assert argv[argv.index("--vsock-sock") + 1].startswith("/tmp/agentd-")
    assert f"share0={tmp_path.resolve()}" in argv and argv[argv.index(f"share0={tmp_path.resolve()}") - 1] == "--share-ro"


# --------------------------------------------------------------------------- #
# Live: a real sandbox inside a ready Colima profile
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.environ.get("AGENTD_LIVE") != "1", reason="set AGENTD_LIVE=1 with a ready Colima profile")
def test_live_sandbox_in_colima():
    from agentd.sandbox.base import DEFAULT_HOME
    from agentd.sandbox.executor import KrunExecutor

    if not colima.status(colima.PROFILE).ready:
        pytest.skip("Colima profile not set up (agentd-sandbox colima setup)")
    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp") as ws, tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp") as data:
        ws, data = Path(ws), Path(data)
        (data / "ro.txt").write_text("read only\n")
        with KrunExecutor(colima=True, mounts=[data]) as ex:
            out, rc = ex.execute_bash(
                "uname -r; id -un; cat /proc/net/dev | tail -n +3 | cut -d: -f1 | tr -d ' ' | sort | tr '\\n' ' '; "
                f"echo; cat {data}/ro.txt; touch {data}/x 2>&1 | tail -1; echo from-sandbox > out.txt", ws)
            assert rc == 0, out
            lines = out.splitlines()
            assert lines[1] == "agent" and lines[2].split() == ["dummy0", "lo"]
            assert lines[3] == "read only" and "Read-only file system" in lines[4]
        assert (ws / "out.txt").read_text().strip() == "from-sandbox"
        assert not (data / "x").exists()
