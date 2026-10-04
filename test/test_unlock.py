"""
Secrets locked in fnox (fake fnox, test/fake_fnox.py): approvals carry what
needs unlocking and take the master password with the decision; uses that were
approved ahead (a rule, an "always" login) ask with an "unlock" approval and
continue once it's unlocked. Never up front: nothing is read before it's needed.
"""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from agentd import fnox
from agentd.egress import Egress, EgressSession
from agentd.egress import approvals as ap
from agentd.egress.approvals import Approvals
from agentd.egress.proxy import Refused
from test.test_fnox import vault  # noqa: F401  (fixture)


def _config(d: Path, rules: str = "") -> None:
    (d / "fnox.toml").write_text('[secrets]\nA = { provider = "x", value = "a" }\n'
                                 'B = { provider = "x", value = "b", description = "bravo" }\n'
                                 'C = { provider = "x", value = "c" }\n' + rules)


async def _decide_when_asked(a: Approvals, kind: str, decision: str, password=None, delay=0.1):
    for _ in range(200):
        pending = [x for x in a.list(pending_only=True) if x.kind == kind]
        if pending:
            await asyncio.sleep(delay)
            return a.decide(pending[0].id, decision, password=password)
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {kind} approval")


def test_proxy_asks_to_unlock_a_ruled_secret(vault, tmp_path):  # noqa: F811
    bin_, d = vault
    _config(d, '[[proxy.rules]]\nsecret = "A"\ndomain = "api.example.com"\n')
    a = Approvals(hold=5, allow_file=tmp_path / "allow.toml")
    sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="ul-")) / "s.sock"

    async def main():
        s = EgressSession(Egress(approvals=a, audit=None, fnox_bin=bin_), d, sock, session="s1")
        await s.start()
        try:
            assert not s.loaded("A") and s.placeholders["A"].startswith("agentd_ph_"), "locked: nothing read"
            assert s.policy.connect("api.example.com", "", 443) == "intercept", "its rule still applies"
            auth = [(b"authorization", b"Bearer " + s.placeholders["A"].encode())]
            # Wrong password first: the approval stays pending, then the right one.
            async def human():
                approval = None
                for _ in range(200):
                    approval = next((x for x in a.list(pending_only=True) if x.kind == "unlock"), None)
                    if approval:
                        break
                    await asyncio.sleep(0.02)
                assert approval.details == {"secrets": ["A"], "host": "api.example.com", "unlock": ["A"]}
                with pytest.raises(ValueError):
                    a.decide(approval.id, "once", password="nope")
                a.decide(approval.id, "once", password=bytearray(b"hunter2"))
            (headers, used), _ = await asyncio.gather(s.proxy.inject_or_ask("api.example.com", "GET", "/x", auth),
                                                      human())
            assert headers == [(b"authorization", b"Bearer alpha-value")] and used == ["A"]
            assert s.proxy.masks[b"alpha-value"], "responses are scrubbed of it"
            # The vault changed: fnox.clear() re-locks it and the proxy forgets what it read.
            fnox.clear(fnox=bin_)
            await asyncio.sleep(0)
            assert not s.loaded("A") and s.proxy.masks[b"alpha-value"], "forgotten, still scrubbed"
            with pytest.raises(Refused) as e:
                a.hold = 0.2
                await s.proxy.inject_or_ask("api.example.com", "GET", "/x", auth)
            assert e.value.detail["locked"], "the next use asks to unlock again"
        finally:
            await s.stop()
    asyncio.run(main())


def test_proxy_unlock_denied_or_undecided(vault, tmp_path):  # noqa: F811
    bin_, d = vault
    _config(d, '[[proxy.rules]]\nsecret = "A"\ndomain = "api.example.com"\n')
    a = Approvals(hold=0.3, allow_file=tmp_path / "allow.toml")
    sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="ul-")) / "s.sock"

    async def main():
        s = EgressSession(Egress(approvals=a, audit=None, fnox_bin=bin_), d, sock, session="s1")
        await s.start()
        try:
            auth = [(b"authorization", b"Bearer " + s.placeholders["A"].encode())]
            with pytest.raises(Refused) as e:
                await s.proxy.inject_or_ask("api.example.com", "GET", "/x", auth)
            assert e.value.detail["locked"] and e.value.approval_status == "pending"
            body = json.loads(s.proxy.refusal_body(e.value))["error"]
            assert "locked in fnox" in body["message"] and body["approval"]["status"] == "pending"
            a.decide(e.value.approval_id, "deny")
            with pytest.raises(Refused) as e:  # a new ask: the denied one is done
                await s.proxy.inject_or_ask("api.example.com", "GET", "/x", auth)
        finally:
            await s.stop()
    asyncio.run(main())


def test_request_access_for_a_locked_secret(vault, tmp_path):  # noqa: F811
    bin_, d = vault
    _config(d)
    a = Approvals(hold=5, allow_file=tmp_path / "allow.toml")
    sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="ul-")) / "s.sock"
    from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY

    async def main():
        s = EgressSession(Egress(approvals=a, audit=None, fnox_bin=bin_), d, sock, session="s1")
        await s.start()
        ap.enable_access_skill(a)
        try:
            listed = {x["name"]: x for x in await FUNCTION_REGISTRY["list_secrets"]()}
            assert set(listed) == {"A", "B", "C"} and listed["B"]["description"] == "bravo"
            r, decided = await asyncio.gather(
                FUNCTION_REGISTRY["request_access"](host="api.example.com", reason="open a PR", secret="B",
                                                   method="POST", path="/pulls"),
                _decide_when_asked(a, "secret", "session", password="hunter2"))
            assert decided.details["unlock"] == [] and r["status"] == "session"
            auth = [(b"authorization", b"token " + s.placeholders["B"].encode())]
            headers, _ = await s.proxy.inject_or_ask("api.example.com", "POST", "/pulls", auth)
            assert headers == [(b"authorization", b"token bravo-value")]
            assert fnox.uncached(["A", "B", "C"], d, fnox=bin_) == ["A", "C"], "only what was asked for"
        finally:
            await s.stop()
            for name in ("list_secrets", "request_access", "access_status"):
                FUNCTION_REGISTRY.pop(name, None)
                SCHEMA_REGISTRY.pop(name, None)
    asyncio.run(main())


def test_browser_logins_unlock(vault, tmp_path, monkeypatch):  # noqa: F811
    from agentd.devices import browser as br

    bin_, d = vault
    monkeypatch.setattr(fnox, "_base", lambda f, profile: [bin_])  # the browser uses the default fnox
    a = Approvals(hold=5, allow_file=tmp_path / "allow.toml")
    logins = {"gh": {"url": "https://github.com/login", "username": "A", "password": "B"},
              "bank": {"url": "https://bank.example/login", "password": "C"}}
    b = br.Browser(chrome="/bin/true", allowed=True, approvals=a, logins=logins, fnox_cwd=d,
                   logins_allowed={"bank"})

    # Asking to log in: the approval says what's locked, and allowing it takes the password.
    req = b.request_login("gh", "open a PR")
    approval = a.items[req["id"]]
    assert approval.details["unlock"] == ["A", "B"] and approval.details["secrets"] == ["A", "B"]
    with pytest.raises(ValueError, match="master password"):
        a.decide(req["id"], "once")
    a.decide(req["id"], "once", password="hunter2")
    b._use_login("gh", spend=False)
    assert fnox.uncached(["A", "B"], d, fnox=bin_) == []

    # Approved ahead (host code), locked now: an unlock approval, then on.
    async def main():
        _, decided = await asyncio.gather(b._ensure_unlocked("bank"),
                                          _decide_when_asked(a, "unlock", "once", password="hunter2"))
        assert decided.details["secrets"] == ["C"] and decided.details["site"] == "bank"
        assert fnox.uncached(["C"], d, fnox=bin_) == []
        await b._ensure_unlocked("bank")  # nothing to ask now
    asyncio.run(main())


def test_serve_decide_with_password(vault, monkeypatch):  # noqa: F811
    from test.test_serve import Client, FakePool, _short_tmp
    from agentd import availability as av
    from agentd.serve import Server, ServeConfig

    bin_, d = vault

    async def fake_available(target, harness_options=None, refresh=False):
        return {}
    monkeypatch.setattr(av, "available_async", fake_available)
    root = _short_tmp()

    async def main():
        cfg = ServeConfig(dir=root / "s", workspace_roots=[root / "ws"], egress={"approvals": {"hold": 1}})
        server = Server(cfg, pool=FakePool(cfg))
        await server.start()
        try:
            a = server.approvals
            fill = lambda names, pw: fnox.fill(names, pw, cwd=d, fnox=bin_)  # noqa: E731
            approval = a.request("unlock", "", {"secrets": ["A"]}, "x", unlock=(["A"], fill))
            local = Client(cfg.serve_socket)
            status, body = await local.call("POST", f"/v1/approvals/{approval.id}", json={"decision": "once"})
            assert status == 400 and "master password" in json.dumps(body)
            status, body = await local.call("POST", f"/v1/approvals/{approval.id}",
                                            json={"decision": "once", "password": "nope"})
            assert status == 400 and approval.status == "pending"
            status, body = await local.call("POST", f"/v1/approvals/{approval.id}",
                                            json={"decision": "once", "password": "hunter2"})
            assert status == 200 and body["status"] == "once" and "hunter2" not in json.dumps(body)
            assert fnox.uncached(["A"], d, fnox=bin_) == []
        finally:
            await server.stop()
    asyncio.run(main())
