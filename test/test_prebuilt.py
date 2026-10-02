"""
Prebuilt binaries (agentd.sandbox.prebuilt): only used when built from this
launcher.c and the pinned libkrun; Linux and Colima setup install them
instead of building (and then need no build tools).
"""
import json

import pytest

from agentd.sandbox import colima, linux, prebuilt


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(prebuilt, "BIN", tmp_path)
    monkeypatch.setattr(prebuilt, "MANIFEST", tmp_path / "prebuilt.json")
    for name in ("agentd-krun", "agentd-krun-linux-x86_64", "agentd-krun-linux-aarch64",
                 "libkrun-linux-x86_64.so", "libkrun-linux-aarch64.so"):
        (tmp_path / name).write_bytes(b"\x7fELF")

    def write(**over):
        m = {"launcher": colima.launcher_sha(), "libkrun": colima.LIBKRUN_BUILD,
             "targets": ["darwin", "linux-aarch64", "linux-x86_64"], **over}
        (tmp_path / "prebuilt.json").write_text(json.dumps(m))
    write()
    return tmp_path, write


def test_only_current_binaries_are_used(bundle):
    d, write = bundle
    assert prebuilt.launcher("darwin") == d / "agentd-krun"
    assert prebuilt.launcher("linux-x86_64") == d / "agentd-krun-linux-x86_64"
    assert prebuilt.libkrun("aarch64") == d / "libkrun-linux-aarch64.so"
    assert prebuilt.launcher("linux-riscv64") is None
    write(launcher="stale")
    assert prebuilt.launcher("darwin") is None, "built from another launcher.c"
    write(libkrun="1.0.0+net")
    assert prebuilt.libkrun("aarch64") is None, "not the pinned libkrun"
    write(targets=["darwin"])
    assert prebuilt.libkrun("x86_64") is None
    (d / "prebuilt.json").unlink()
    assert prebuilt.launcher("darwin") is None and prebuilt.manifest() == {}


def test_install_script():
    s = prebuilt.install_libkrun_script("/x y/libkrun.so", "1.19.6")
    assert "sudo install -D -m 755 '/x y/libkrun.so' /usr/local/lib64/libkrun.so.1.19.6" in s
    assert "ln -sf libkrun.so.1.19.6 /usr/local/lib64/libkrun.so.1" in s and "ldconfig" in s
    assert "sudo tee /usr/local/lib64/libkrun.so.1.19.6" in prebuilt.install_libkrun_script("-", "1.19.6")


def test_linux_setup_installs_prebuilt(bundle, tmp_path, monkeypatch):
    d, _ = bundle
    home = tmp_path / "home"
    monkeypatch.setattr(linux, "DEFAULT_HOME", home)
    (home / "rootfs" / "agents" / "usr").mkdir(parents=True)
    monkeypatch.setattr(linux.platform, "machine", lambda: "x86_64")
    facts = {"platform": "linux", "arch": "x86_64", "kvm": True, "kvm_rw": True, "kvm_group_listed": True,
             "kvm_group_active": True, "libkrun": None, "libkrun_net": False, "libkrunfw": None,
             "launcher": True, "launcher_sha": None, "launcher_prebuilt": True, "libkrun_prebuilt": True,
             "nofile_hard": 1048576, "docker": True, "cc": False}
    st = linux.status(facts=facts)
    assert st.needs == ["libkrunfw", "libkrun"], "no build tools, no launcher build"
    krun = next(s for s in linux.plan(st) if s.key == "libkrun")
    assert "prebuilt" in krun.description and str(d / "libkrun-linux-x86_64.so") in krun.commands[0]
    assert "make" not in krun.commands[0]


def test_colima_installs_prebuilt(bundle, monkeypatch):
    d, write = bundle
    assert colima._installs() == ["libkrunfw", "libkrun", "launcher"]
    st = colima.Status("agentd")
    st.needs = ["libkrun", "launcher"]
    steps = {s.key: s for s in colima.plan(st)}
    assert "prebuilt" in steps["libkrun"].description and str(d / "libkrun-linux-aarch64.so") in steps["libkrun"].commands[0]
    assert "prebuilt" in steps["launcher"].description and "cc " not in steps["launcher"].commands[0]
    write(targets=["darwin"])
    assert colima._installs()[0] == "build_deps", "no Linux binaries: build in the VM"
    steps = {s.key: s for s in colima.plan(st)}
    assert "from source" in steps["libkrun"].description and "cc -O2" in steps["launcher"].commands[0]
