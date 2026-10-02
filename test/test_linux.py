"""
Linux host support: Claude Code credentials from the file (Linux, or macOS
without Keychain), native libkrun status/plan logic (fake probe), and the
per-platform launcher path. The live native-Linux sandbox run is
test_sandbox.py's krun tests on a Linux host.
"""
import json
import subprocess
import sys
import time

import pytest

from agentd.model_proxy import ClaudeCodeCredentials, CredentialError
from agentd.sandbox import linux


def _creds_file(tmp_path, token="tok-123", expires_in=3600):
    (tmp_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": (time.time() + expires_in) * 1000}}))


def test_claude_credentials_from_file_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(CredentialError, match="not found"):
        ClaudeCodeCredentials._read()
    _creds_file(tmp_path)
    token, expires = ClaudeCodeCredentials._read()
    assert token == "tok-123" and expires > time.time()


def test_macos_falls_back_to_the_file_without_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 44, "", "not found"))
    _creds_file(tmp_path, token="from-file")
    assert ClaudeCodeCredentials._read()[0] == "from-file"


def _facts(**over):
    facts = {"platform": "linux", "arch": "x86_64", "kvm": True, "kvm_rw": True, "kvm_group_listed": True,
             "kvm_group_active": True, "libkrun": linux.LIBKRUN_VERSION, "libkrun_net": True, "libkrunfw": linux.LIBKRUNFW_VERSION,
             "launcher": True, "launcher_sha": linux.launcher_sha(), "nofile_hard": 1048576, "docker": True,
             "cc": True}
    facts.update(over)
    return facts


def test_linux_status_and_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(linux, "DEFAULT_HOME", tmp_path)
    (tmp_path / "rootfs" / "agents" / "usr").mkdir(parents=True)
    assert linux.status(facts=_facts()).ready

    st = linux.status(facts=_facts(kvm_rw=False, libkrun=None, libkrunfw="5.5.0", launcher_sha="old",
                                   nofile_hard=4096))
    assert not st.ready and not st.blocked
    assert st.needs == ["kvm_access", "file_limits", "build_deps", "libkrunfw", "libkrun", "launcher"]
    steps = linux.plan(st)
    fw = next(s for s in steps if s.key == "libkrunfw")
    assert "libkrunfw-x86_64.tgz" in fw.commands[0] and linux.LIBKRUNFW_ASSETS["x86_64"] in fw.commands[0]
    assert "usermod -aG kvm" in next(s for s in steps if s.key == "kvm_access").commands[0]
    assert not any(s.destructive for s in steps)

    assert linux.status(facts=_facts(kvm=False)).blocked, "agentd can't enable KVM itself"
    assert linux.status(facts=_facts(arch="mips")).blocked
    assert linux.status(facts=_facts(platform="darwin")).blocked

    st = linux.status("rosey", tmp_path / "img", facts=_facts())
    assert st.needs == ["rootfs:rosey"]
    step = linux.plan(st, image="rosey", image_dir=tmp_path / "img")[0]
    assert "agentd.sandbox.rootfs build" in step.commands[0] and "real owners" in step.description
    assert linux.status("rosey", tmp_path / "img", facts=_facts(docker=False)).blocked


def test_launcher_path_is_per_platform(monkeypatch):
    from agentd.sandbox import krun

    monkeypatch.setattr(sys, "platform", "darwin")
    assert krun._default_launcher().name == "agentd-krun"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(krun.platform, "machine", lambda: "x86_64")
    assert krun._default_launcher().name == "agentd-krun-linux-x86_64"
