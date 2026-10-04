"""
agentd.fnox against a fake fnox with a locked vault (test/fake_fnox.py): reads
never prompt and raise SecretMissing while locked; fill() unlocks per name on
a pty with the host's password and zeroes it; no FNOX_* variables reach fnox
(they'd split its daemon cache); approvals that need an unlock take the
password with the decision.
"""
import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

from agentd import fnox
from agentd.egress.approvals import Approvals

HERE = Path(__file__).parent


@pytest.fixture
def vault(tmp_path):
    """(fnox binary, its directory) with secrets A, B, C behind master password "hunter2"."""
    script = tmp_path / "fnox"
    script.write_text(f"#!{sys.executable}\n" + (HERE / "fake_fnox.py").read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "fake-fnox.json").write_text(json.dumps(
        {"password": "hunter2", "secrets": {"A": "alpha-value", "B": "bravo-value", "C": "charlie-value"}}))
    return str(script), tmp_path


def _calls(d):
    return [json.loads(line) for line in (d / "calls.jsonl").read_text().splitlines()]


def test_reads_fill_and_environment(vault, monkeypatch):
    bin_, d = vault
    monkeypatch.setenv("FNOX_NON_INTERACTIVE", "1")
    monkeypatch.setenv("FNOX_PROFILE", "work")
    with pytest.raises(fnox.SecretMissing) as e:
        fnox.get("A", d, fnox=bin_)
    assert e.value.names == ["A"]
    with pytest.raises(RuntimeError, match="not found"):
        fnox.get("NOPE", d, fnox=bin_)  # undefined: an error, not something to unlock
    assert fnox.uncached(["A", "B", "A"], d, fnox=bin_) == ["A", "B"]

    pw = bytearray(b"wrong")
    assert fnox.fill(["A"], pw, cwd=d, fnox=bin_) == {"A": fnox.WRONG_PASSWORD}
    assert pw == bytearray(5), "zeroed"
    pw = bytearray(b"hunter2")
    assert fnox.fill(["A", "B"], pw, cwd=d, fnox=bin_) == {}
    assert pw == bytearray(7)
    assert fnox.get("A", d, fnox=bin_) == "alpha-value"
    assert fnox.uncached(["A", "B", "C"], d, fnox=bin_) == ["C"], "unlocked per name"

    calls = _calls(d)
    assert all(c["fnox_env"] == [] for c in calls), "no FNOX_* variable reaches fnox"
    assert all(c["argv"][:2] == ["-P", "work"] for c in calls), "FNOX_PROFILE becomes -P"
    assert any("--non-interactive" not in c["argv"] for c in calls) and \
        all("--non-interactive" in c["argv"] for c in calls if c["argv"][2:3] == ["--non-interactive"])


def test_fill_without_the_daemon_says_so(vault):
    bin_, d = vault
    conf = json.loads((d / "fake-fnox.json").read_text())
    (d / "fake-fnox.json").write_text(json.dumps({**conf, "daemon": False}))
    with pytest.raises(RuntimeError, match="daemon"):
        fnox.fill(["A"], bytearray(b"hunter2"), cwd=d, fnox=bin_)


def test_approval_takes_the_password(vault, tmp_path):
    bin_, d = vault
    a = Approvals(allow_file=tmp_path / "allow.toml")
    fill = lambda names, pw: fnox.fill(names, pw, cwd=d, fnox=bin_)  # noqa: E731
    missing = fnox.uncached(["A", "B"], d, fnox=bin_)
    details = {"secret": "A", "host": "api.example.com", "method": "", "path": "", "header": "authorization"}
    approval = a.request("secret", "", details, "x", unlock=(missing, fill))
    assert approval.details["unlock"] == ["A", "B"]
    assert "password" not in json.dumps(approval.public()).lower()

    with pytest.raises(ValueError, match="master password"):
        a.decide(approval.id, "once")  # allowing needs the password
    pw = bytearray(b"nope")
    with pytest.raises(fnox.WrongPassword, match="didn't unlock"):
        a.decide(approval.id, "once", password=pw)
    assert pw == bytearray(4) and approval.status == "pending", "still pending: the host can ask again"
    a.decide(approval.id, "session", password="hunter2")
    assert approval.status == "session" and approval.details["unlock"] == []
    assert fnox.uncached(["A", "B"], d, fnox=bin_) == []

    other = a.request("unlock", "", {"secrets": ["C"]}, "y", unlock=(["C"], fill))
    a.decide(other.id, "deny")  # no password needed to say no
    assert other.status == "deny" and fnox.uncached(["C"], d, fnox=bin_) == ["C"]


def test_deleted_item_is_not_a_wrong_password(vault, tmp_path):
    bin_, d = vault
    conf = json.loads((d / "fake-fnox.json").read_text())
    conf["secrets"]["GONE"] = None  # renamed or deleted in the vault
    (d / "fake-fnox.json").write_text(json.dumps(conf))
    failed = fnox.fill(["A", "GONE"], bytearray(b"hunter2"), cwd=d, fnox=bin_)
    assert failed == {"GONE": "Enpass: secret 'GONE' not found (fnox::provider::secret_not_found)"}
    assert fnox.uncached(["A"], d, fnox=bin_) == [], "the rest unlocked"

    a = Approvals(allow_file=tmp_path / "allow.toml")
    approval = a.request("unlock", "", {"secrets": ["GONE"]}, "x",
                         unlock=(["GONE"], lambda names, pw: fnox.fill(names, pw, cwd=d, fnox=bin_)))
    with pytest.raises(fnox.UnlockFailed, match="GONE: Enpass: secret 'GONE' not found.*renamed or deleted"):
        a.decide(approval.id, "once", password="hunter2")
    assert approval.status == "pending"


def test_clear_relocks_and_forgets(vault):
    bin_, d = vault
    fnox.fill(["A"], bytearray(b"hunter2"), cwd=d, fnox=bin_)
    assert fnox.uncached(["A"], d, fnox=bin_) == []

    class Holder:
        forgot = 0

        def forget_secrets(self):
            self.forgot += 1
    h = Holder()
    fnox.on_clear(h)
    fnox.clear(fnox=bin_)
    assert fnox.uncached(["A"], d, fnox=bin_) == ["A"] and h.forgot == 1
    assert _calls(d)[-2]["argv"] == ["daemon", "clear"] and _calls(d)[-2]["fnox_env"] == []


def test_real_fnox_enpass(tmp_path):
    """With AGENTD_FNOX_ENPASS=<fnox binary with the Enpass provider>:<dir with fnox.toml>
    (master password "password"), the same against real fnox."""
    import os

    spec = os.environ.get("AGENTD_FNOX_ENPASS")
    if not spec:
        pytest.skip("set AGENTD_FNOX_ENPASS=FNOX_BIN:DIR")
    bin_, d = spec.split(":", 1)
    names = ["LOGIN_PASSWORD", "SENSITIVE"]
    shutil.which(bin_)
    assert fnox.uncached(names, d, fnox=bin_) == names, "start with the daemon locked"
    assert fnox.fill(names, bytearray(b"wrong"), cwd=d, fnox=bin_) == names
    assert fnox.fill(names, bytearray(b"password"), cwd=d, fnox=bin_) == []
    assert fnox.get("SENSITIVE", d, fnox=bin_) == "sensitive"
