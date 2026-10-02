"""
Egress approvals (agentd.egress.approvals): requests, decisions and grants,
the signed webhook, the proxy holding requests while an approver decides,
and agentd serve's approval endpoints.
"""
import asyncio
import hashlib
import hmac
import json
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

from agentd.egress import policy as pol
from agentd.egress import proxy as px
from agentd.egress.approvals import Approvals
from agentd.egress.ca import SessionCA
from agentd.egress.policy import Policy, SecretRule
from agentd.egress.proxy import EgressProxy
from test.test_egress import REAL, Guest, _request, _upstream


class FakeEgress:
    def __init__(self, session, placeholders, config_files=()):
        self.session, self.placeholders, self.config_files = session, placeholders, list(config_files)


def test_requests_decisions_and_grants(tmp_path):
    async def main():
        fnox_cfg = tmp_path / "fnox.toml"
        fnox_cfg.write_text("")
        a = Approvals(allow_file=tmp_path / "allow.toml")
        policy = Policy()
        a.asker(FakeEgress("s1", {"GH": "ph"}, [fnox_cfg]), policy)
        r1 = a.request("connect", "s1", {"host": "pypi.org", "ip": "", "port": 443})
        assert a.request("connect", "s1", {"host": "pypi.org", "ip": "", "port": 443}).id == r1.id, "deduped"
        a.decide(r1.id, "session")
        assert policy.connect("pypi.org", "", 443) == "pass" and not (tmp_path / "allow.toml").exists()
        r2 = a.request("connect", "s1", {"host": "npmjs.org", "ip": "", "port": 443})
        a.decide(r2.id, "always")
        assert a.saved_allows() == ["npmjs.org:443"]
        r3 = a.request("secret", "s1", {"secret": "GH", "host": "api.github.com", "method": "POST",
                                        "path": "/repos/me/app/pulls?x=1", "header": "authorization"})
        a.decide(r3.id, "always", by="peer-9")
        assert policy.rules[-1] == SecretRule("GH", "api.github.com", "authorization", ("POST",),
                                              ("/repos/me/app/pulls",))
        assert pol.load_rules([fnox_cfg]) == [policy.rules[-1]], "always grants go into the fnox config"
        assert a.items[r3.id].decided_by == "peer-9"
        with pytest.raises(ValueError):
            a.decide(r3.id, "maybe")
        assert a.decide(r3.id, "deny").status == "always", "decided approvals don't change"
        # a new session picks up the saved allowances
        p2 = Policy()
        a.asker(FakeEgress("s2", {}), p2)
        assert p2.connect("npmjs.org", "", 443) == "pass"
    asyncio.run(main())


def test_webhook_is_signed(tmp_path):
    async def main():
        got = []

        async def hook(request):
            got.append((await request.read(), request.headers.get("x-agentd-signature")))
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_post("/hook", hook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            a = Approvals(f"http://127.0.0.1:{port}/hook", secret="s3cret", answer_url="https://box.example",
                          allow_file=tmp_path / "allow.toml")
            approval = a.request("connect", "s1", {"host": "x.com", "ip": "", "port": 443}, reason="docs")
            for _ in range(50):
                if got:
                    break
                await asyncio.sleep(0.05)
            body, sig = got[0]
            assert sig == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
            payload = json.loads(body)
            assert payload["approval"]["id"] == approval.id and payload["approval"]["reason"] == "docs"
            assert payload["answer"]["url"] == f"https://box.example/v1/approvals/{approval.id}"
        finally:
            await runner.cleanup()
    asyncio.run(main())


def test_proxy_holds_for_approvals(monkeypatch, tmp_path):
    async def main():
        seen = []
        runner, port, test_ca = await _upstream(seen)
        monkeypatch.setattr(px, "_UPSTREAM_CA", test_ca.pem.decode())
        sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="ap-")) / "s.sock"
        ph = pol.make_placeholder(REAL)
        ca = SessionCA()
        policy = Policy(rules=[SecretRule("TOKEN", "localhost", methods=("GET",), paths=("/ok/**",), port=port)])
        a = Approvals(hold=1.0, allow_file=tmp_path / "allow.toml")
        egress = FakeEgress("s1", {"TOKEN": ph})
        proxy = EgressProxy(sock, policy, secrets={"TOKEN": REAL}, placeholders={"TOKEN": ph}, ca=ca,
                            ask=a.asker(egress, policy), session="s1")
        await proxy.start()

        async def approve_soon(kind, decision, delay=0.2, path=None):
            for _ in range(100):
                pending = [x for x in a.list(pending_only=True)
                           if x.kind == kind and path in (None, x.details.get("path"))]
                if pending:
                    await asyncio.sleep(delay)
                    a.decide(pending[0].id, decision)
                    return pending[0]
                await asyncio.sleep(0.02)

        try:
            guest = Guest(sock, ca.pem)
            auth = f"Authorization: Bearer {ph}\r\n".encode()
            # A secret outside its rule, approved for the session while held: injected.
            reader, writer = await guest.connect("localhost", port)
            approver = asyncio.ensure_future(approve_soon("secret", "session"))
            head, body = await _request(reader, writer, "POST", "/ok/new", auth)
            await approver
            assert b"200" in head.split(b"\r\n")[0] and seen[-1]["auth"] == f"Bearer {REAL}"
            # ...and the next one needs no approval.
            n = len(a.items)
            head, body = await _request(reader, writer, "POST", "/ok/new", auth)
            assert b"200" in head.split(b"\r\n")[0] and len(a.items) == n
            writer.close()

            # Not decided within the hold: 403 with the pending approval; retried after approval: OK.
            reader, writer = await guest.connect("localhost", port)
            head, body = await _request(reader, writer, "DELETE", "/ok/x", auth)
            err = json.loads(body)["error"]
            assert b"403" in head.split(b"\r\n")[0] and err["approval"]["status"] == "pending"
            a.decide(err["approval"]["id"], "once")
            writer.close()
            reader, writer = await guest.connect("localhost", port)
            head, body = await _request(reader, writer, "DELETE", "/ok/x", auth)
            assert b"403" in head.split(b"\r\n")[0], "'once' was spent by the held request's approval"
            writer.close()

            # Denied while held: the 403 says so (not pending, no retry).
            reader, writer = await guest.connect("localhost", port)
            approver = asyncio.ensure_future(approve_soon("secret", "deny", path="/ok/y"))
            head, body = await _request(reader, writer, "PUT", "/ok/y", auth)
            await approver
            err = json.loads(body)["error"]
            assert b"403" in head.split(b"\r\n")[0] and err["approval"]["status"] == "deny", err
            assert "retry_after" not in err["approval"] and "denied" in err["message"]
            writer.close()

            # An unlisted host, approved once while held: the connection goes through.
            other = Guest(sock, test_ca.pem)
            approver = asyncio.ensure_future(approve_soon("connect", "once"))
            policy.rules.clear()  # localhost is now just a host with no rules
            res = await other.connect("localhost", port)
            await approver
            assert res is not None
            head, body = await _request(*res, "GET", "/hello", b"")
            assert b"200" in head.split(b"\r\n")[0] and seen[-1]["path"] == "/hello"
            res[1].close()

            # Denied: closed (an explanatory 403 is only for ports 443 and 80).
            approver = asyncio.ensure_future(approve_soon("connect", "deny"))
            assert await guest.connect("localhost", port) is None
            await approver
        finally:
            await proxy.stop()
            await runner.cleanup()
    asyncio.run(main())


def test_serve_approval_endpoints(monkeypatch):
    from test.test_serve import Client, FakePool, _short_tmp
    from agentd import availability as av
    from agentd.serve import Server, ServeConfig

    async def fake_available(target, harness_options=None, refresh=False):
        return {}
    monkeypatch.setattr(av, "available_async", fake_available)
    root = _short_tmp()

    async def main():
        cfg = ServeConfig(dir=root / "s", workspace_roots=[root / "ws"], approvers=["peer-ok"],
                          egress={"approvals": {"hold": 1}})
        server = Server(cfg, pool=FakePool(cfg))
        await server.start()
        try:
            a = server.approvals
            a.asker(FakeEgress("s1", {}), Policy())
            approval = a.request("connect", "s1", {"host": "x.com", "ip": "", "port": 443})
            peer, approver, local = (Client(cfg.peers_socket, "peer-no"), Client(cfg.peers_socket, "peer-ok"),
                                     Client(cfg.serve_socket))
            _, listing = await peer.call("GET", "/v1/approvals?pending=1")
            assert [x["id"] for x in listing["data"]] == [approval.id]
            status, _ = await peer.call("POST", f"/v1/approvals/{approval.id}", json={"decision": "session"})
            assert status == 403, "only approvers decide"
            status, body = await approver.call("POST", f"/v1/approvals/{approval.id}", json={"decision": "session"})
            assert status == 200 and body["status"] == "session" and body["decided_by"] == "peer-ok"
            status, body = await local.call("GET", f"/v1/approvals/{approval.id}")
            assert body["status"] == "session"
            status, _ = await local.call("POST", "/v1/approvals/apr_nope", json={"decision": "deny"})
            assert status == 404
        finally:
            await server.stop()
    asyncio.run(main())


def test_request_access_skill(tmp_path):
    from agentd.egress import approvals as ap
    from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY

    async def main():
        a = Approvals(allow_file=tmp_path / "allow.toml")
        policy = Policy()
        a.asker(FakeEgress("s1", {"GH": "ph"}), policy)
        ap.enable_access_skill(a)
        try:
            schema = SCHEMA_REGISTRY["request_access"]["function"]
            assert schema["parameters"]["required"] == ["host", "reason"]
            first = schema["description"].splitlines()[0]
            assert "A request nobody decides within 60 minutes expires (status expired)" in first
            assert "held up to 25 seconds" in first and "timeout of at least 40 seconds" in first
            assert '"agentd_egress"' in first and "retry_after" in first
            assert "host: the host name" in schema["description"]
            r = await FUNCTION_REGISTRY["request_access"](host="API.github.com", reason="open a PR", secret="GH",
                                                         method="post", path="/repos/me/app/pulls")
            assert r["status"] == "pending"
            item = a.items[r["id"]]
            assert item.kind == "secret" and item.details["method"] == "POST" and item.reason == "open a PR"
            a.decide(r["id"], "session")
            assert (await FUNCTION_REGISTRY["access_status"](approval_id=r["id"]))["status"] == "session"
            assert policy.rules[-1].matches("POST", "/repos/me/app/pulls")
            r2 = await FUNCTION_REGISTRY["request_access"](host="pypi.org", reason="install deps")
            a.decide(r2["id"], "once")
            assert a.items[r2["id"]].kind == "connect"
        finally:
            for name in ("request_access", "access_status"):
                FUNCTION_REGISTRY.pop(name, None)
                SCHEMA_REGISTRY.pop(name, None)
    asyncio.run(main())
