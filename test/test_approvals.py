"""
Egress approvals (agentd.egress.approvals): requests, decisions and grants,
the signed webhook, the proxy holding requests while an approver decides,
and agentd serve's approval endpoints.
"""
import asyncio
import dataclasses
import hashlib
import hmac
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

from agentd.egress import policy as pol
from agentd.egress import proxy as px
from agentd.egress.approvals import Approvals
from agentd.egress.ca import SessionCA
from agentd.egress.policy import Grant, Policy, SecretRule
from agentd.egress.proxy import EgressProxy
from test.test_egress import REAL, Guest, _request, _upstream


class FakeEgress:
    def __init__(self, session, placeholders, config_files=()):
        self.session, self.placeholders, self.config_files = session, placeholders, list(config_files)

    def loaded(self, name):
        return True  # the proxy holds its values: nothing to unlock

    def uncached(self, names):
        return []

    def fill(self, names, password):
        return {}


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
        assert fnox_cfg.read_text() == "", "the fnox config is never edited (its contents key fnox's cache)"
        assert a.saved_rules() == [policy.rules[-1]] and a.saved_allows() == ["npmjs.org:443"]
        fresh_policy = Policy()
        Approvals(allow_file=tmp_path / "allow.toml").asker(FakeEgress("s2", {"GH": "ph", "X": "p"}), fresh_policy)
        assert fresh_policy.rules == [policy.rules[-1]] and fresh_policy.connect("npmjs.org", "", 443) == "pass", \
            "always grants apply to later sessions"
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
            assert b"200" in head.split(b"\r\n")[0], "'once' decided after the hold: the retry goes through"
            writer.close()
            reader, writer = await guest.connect("localhost", port)
            head, body = await _request(reader, writer, "DELETE", "/ok/x", auth)
            assert b"403" in head.split(b"\r\n")[0], "...once"
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
        a.hold = 0.1  # request_access waits up to the hold for an answer
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


@pytest.mark.skipif(shutil.which("fnox") is None, reason="needs fnox")
def test_list_secrets_and_use_one_without_a_rule(tmp_path):
    from agentd.egress import Egress, EgressSession
    from agentd.egress import approvals as ap
    from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY

    (tmp_path / "fnox.toml").write_text(
        '[providers.plain]\ntype = "plain"\n[secrets]\n'
        'GH = { provider = "plain", value = "ghp_realgh0000000000", description = "GitHub, phact/agentd" }\n'
        'DB = { provider = "plain", value = "db-real-password" }\n'
        'HIDDEN = { provider = "plain", value = "hidden-value" }\n'
        '[[proxy.rules]]\nsecret = "GH"\ndomain = "api.github.com"\nmethods = ["GET"]\n')
    a = Approvals(hold=0.1, allow_file=tmp_path / "allow.toml")
    sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="ls-")) / "s.sock"

    async def main():
        s = EgressSession(Egress(approvals=a, audit=None, secrets=("GH", "DB")), tmp_path, sock, session="s1")
        await s.start()
        ap.enable_access_skill(a)
        try:
            listed = await FUNCTION_REGISTRY["list_secrets"]()
            assert listed == [
                {"name": "GH", "description": "GitHub, phact/agentd", "env": "GH",
                 "rules": [{"host": "api.github.com", "header": "authorization", "methods": ["GET"],
                            "paths": ["*"]}]},
                {"name": "DB", "description": "", "env": "DB", "rules": []}]
            env = s.sandbox_env()
            assert {"GH", "DB"} <= set(env) and "HIDDEN" not in env, "only the secrets Egress(secrets=) exposes"
            assert set(s.proxy.secrets) == {"GH"}, "DB isn't read from fnox until it may be sent"
            assert "db-real-password" not in repr(listed) + repr(env) and "ghp_realgh" not in repr(env)

            with pytest.raises(ValueError, match="list_secrets"):
                await FUNCTION_REGISTRY["request_access"](host="db.example.com", reason="x", secret="HIDDEN")
            ph = s.placeholders["DB"].encode()
            auth = [(b"authorization", b"Basic " + ph)]
            r = await FUNCTION_REGISTRY["request_access"](host="db.example.com", reason="run a query",
                                                         secret="DB", method="post", path="/q")
            a.decide(r["id"], "session")
            assert (await FUNCTION_REGISTRY["list_secrets"]())[1]["rules"] == [
                {"host": "db.example.com", "header": "authorization", "methods": ["POST"], "paths": ["/q"]}]
            headers, used = await s.proxy.inject_or_ask("db.example.com", "POST", "/q", auth)
            assert headers == [(b"authorization", b"Basic db-real-password")] and used == ["DB"]
            assert s.proxy.masks[b"db-real-password"] == b"*" * 16, "responses are scrubbed of it now"
            # Elsewhere it still needs an approval.
            with pytest.raises(px.Refused) as e:
                await s.proxy.inject_or_ask("evil.example.com", "POST", "/q", auth)
            assert e.value.approval_status == "pending"

            # Asked ahead and approved "once": the next matching request, and only that one.
            r = await FUNCTION_REGISTRY["request_access"](host="db2.example.com", reason="one query",
                                                         secret="DB", method="POST", path="/q")
            a.decide(r["id"], "once")
            assert s.policy.connect("db2.example.com", "", 443) == "intercept"
            assert {"host": "db2.example.com", "header": "authorization", "methods": ["POST"], "paths": ["/q"],
                    "once": True} in (await FUNCTION_REGISTRY["list_secrets"]())[1]["rules"]
            headers, _ = await s.proxy.inject_or_ask("db2.example.com", "POST", "/q", auth)
            assert headers == [(b"authorization", b"Basic db-real-password")]
            with pytest.raises(px.Refused):
                await s.proxy.inject_or_ask("db2.example.com", "POST", "/q", auth)
            assert s.policy.connect("db2.example.com", "", 443) == "deny"

            # A host asked for ahead, "once": one connection.
            r = await FUNCTION_REGISTRY["request_access"](host="pypi.org", reason="install deps")
            a.decide(r["id"], "once")
            assert s.policy.connect("pypi.org", "", 443) == "pass"
            assert s.policy.connect("pypi.org", "", 443) == "deny"
        finally:
            await s.stop()
            for name in ("list_secrets", "request_access", "access_status"):
                FUNCTION_REGISTRY.pop(name, None)
                SCHEMA_REGISTRY.pop(name, None)
    asyncio.run(main())


def test_grants_used_are_reported(monkeypatch, tmp_path):
    """What an approval let through is reported once per connection and grant;
    what the config allows isn't."""
    async def main():
        seen = []
        runner, port, test_ca = await _upstream(seen)
        monkeypatch.setattr(px, "_UPSTREAM_CA", test_ca.pem.decode())
        sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="gu-")) / "s.sock"
        ph, other_ph = pol.make_placeholder(REAL), pol.make_placeholder("other-real-value")
        ca = SessionCA()
        policy = Policy(rules=[SecretRule("CFG", "localhost", methods=("GET",), port=port)])
        reports = []
        a = Approvals(hold=1.0, allow_file=tmp_path / "allow.toml", webhook="http://hook.invalid/",
                      on_grant_used=reports.append)
        posted = []

        async def post(body):
            posted.append(json.loads(body))
        monkeypatch.setattr(a, "_post", post)
        egress = FakeEgress("s1", {"TOKEN": ph, "CFG": other_ph})
        proxy = EgressProxy(sock, policy, secrets={"TOKEN": REAL, "CFG": "other-real-value"},
                            placeholders={"TOKEN": ph, "CFG": other_ph}, ca=ca, ask=a.asker(egress, policy),
                            session="s1", report=a.grant_used)
        await proxy.start()

        async def decide_soon(kind, decision):
            for _ in range(100):
                pending = [x for x in a.list(pending_only=True) if x.kind == kind]
                if pending:
                    a.decide(pending[0].id, decision)
                    return pending[0]
                await asyncio.sleep(0.02)

        def auth(p):
            return f"Authorization: Bearer {p}\r\n".encode()
        try:
            guest = Guest(sock, ca.pem)
            # The config's rule: nothing to report.
            reader, writer = await guest.connect("localhost", port)
            head, _ = await _request(reader, writer, "GET", "/cfg", auth(other_ph))
            assert b"200" in head.split(b"\r\n")[0] and reports == []

            # Approved for the session while held: reported once on this connection...
            decider = asyncio.ensure_future(decide_soon("secret", "session"))
            head, _ = await _request(reader, writer, "POST", "/ok/new?q=secret", auth(ph))
            approval = await decider
            assert b"200" in head.split(b"\r\n")[0]
            r = reports[-1]
            assert r["kind"] == "secret" and r["session"] == "s1" and r["secret"] == "TOKEN"
            assert r["grant"] == {"approval": approval.id, "decision": "session", "rule": {
                "secret": "TOKEN", "host": "localhost", "header": "authorization", "methods": ["POST"],
                "paths": ["/ok/new"]}}
            assert (r["host"], r["port"], r["method"], r["path"]) == ("localhost", port, "POST", "/ok/new")
            assert "ts" in r and posted[-1] == {"type": "grant.used", **r}
            await _request(reader, writer, "POST", "/ok/new", auth(ph))
            assert len(reports) == 1, "one report per connection"
            writer.close()
            # ...and again on the next connection.
            reader, writer = await guest.connect("localhost", port)
            await _request(reader, writer, "POST", "/ok/new", auth(ph))
            assert len(reports) == 2 and reports[-1]["grant"]["approval"] == approval.id
            writer.close()

            # Approved once while held: the "once" grant is the approval.
            reader, writer = await guest.connect("localhost", port)
            decider = asyncio.ensure_future(decide_soon("secret", "once"))
            head, _ = await _request(reader, writer, "DELETE", "/ok/x", auth(ph))
            once = await decider
            assert b"200" in head.split(b"\r\n")[0]
            assert reports[-1]["grant"]["approval"] == once.id and reports[-1]["grant"]["decision"] == "once"
            assert reports[-1]["method"] == "DELETE"
            writer.close()

            # A connection let through by a connect approval: reported when it's made (nothing to see inside).
            policy.rules.clear()
            plain = Guest(sock, test_ca.pem)
            decider = asyncio.ensure_future(decide_soon("connect", "once"))
            res = await plain.connect("localhost", port)
            conn_once = await decider
            assert res is not None and reports[-1]["kind"] == "connect"
            assert reports[-1]["grant"] == {"approval": conn_once.id, "decision": "once",
                                            "rule": {"host": "localhost", "ports": [port]}}
            assert "method" not in reports[-1]
            res[1].close()
            decider = asyncio.ensure_future(decide_soon("connect", "always"))
            res = await plain.connect("localhost", port)
            always = await decider
            assert reports[-1]["grant"]["approval"] == always.id and reports[-1]["grant"]["decision"] == "always"
            res[1].close()
        finally:
            await proxy.stop()

        # A later run: the saved "always" rules are reported by rule (no approval id).
        policy2 = Policy()
        reports.clear()
        a2 = Approvals(hold=1.0, allow_file=tmp_path / "allow.toml", on_grant_used=reports.append)
        a2.decide(a2.request("secret", "s2", {"secret": "TOKEN", "host": "localhost", "method": "GET",
                                              "path": "/saved", "header": "authorization"}).id, "always")
        a3 = Approvals(hold=1.0, allow_file=tmp_path / "allow.toml", on_grant_used=reports.append)
        proxy = EgressProxy(sock, policy2, secrets={"TOKEN": REAL}, placeholders={"TOKEN": ph}, ca=ca,
                            ask=a3.asker(FakeEgress("s3", {"TOKEN": ph}), policy2), session="s3",
                            report=a3.grant_used)
        await proxy.start()
        try:
            plain = Guest(sock, test_ca.pem)
            res = await plain.connect("localhost", port)
            assert reports[-1]["grant"] == {"approval": None, "decision": "always",
                                            "rule": {"host": "localhost", "ports": [port]}}
            res[1].close()
            policy2.allows.clear()
            # Saved rules are for port 443: move it to the test server's.
            assert policy2.rules[0].grant == Grant("always")
            policy2.rules[0] = dataclasses.replace(policy2.rules[0], port=port)
            # Reached only through a granted secret rule: a request without the secret reports the rule.
            reader, writer = await Guest(sock, ca.pem).connect("localhost", port)
            head, _ = await _request(reader, writer, "GET", "/plain", b"")
            assert b"200" in head.split(b"\r\n")[0]
            assert reports[-1]["kind"] == "connect" and reports[-1]["path"] == "/plain"
            assert reports[-1]["grant"]["rule"]["secret"] == "TOKEN" and reports[-1]["grant"]["approval"] is None
            head, _ = await _request(reader, writer, "GET", "/saved", auth(ph))
            assert reports[-1]["kind"] == "connect", "the same grant: already reported on this connection"
            writer.close()
            reader, writer = await Guest(sock, ca.pem).connect("localhost", port)
            await _request(reader, writer, "GET", "/saved", auth(ph))
            assert reports[-1]["kind"] == "secret" and reports[-1]["secret"] == "TOKEN"
            writer.close()
        finally:
            await proxy.stop()
            await runner.cleanup()
    asyncio.run(main())


async def _h2_upstream(test_ca):
    """A local HTTP/2-only HTTPS server: 200 with the request's path for every request."""
    import h2.config
    import h2.connection
    import h2.events

    async def serve(reader, writer):
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        while data := await reader.read(65536):
            for ev in conn.receive_data(data):
                if isinstance(ev, h2.events.RequestReceived):
                    path = dict(ev.headers)[b":path"]
                    conn.send_headers(ev.stream_id, [(b":status", b"200"),
                                                     (b"content-length", str(len(path)).encode())])
                    conn.send_data(ev.stream_id, path, end_stream=True)
            writer.write(conn.data_to_send())
            await writer.drain()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=test_ca.server_context("localhost", ("h2",)))
    return server, server.sockets[0].getsockname()[1]


async def _h2_get(reader, writer, paths, headers=()):
    """GET each path on one HTTP/2 connection; the statuses."""
    import h2.config
    import h2.connection
    import h2.events

    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    statuses = {}
    for path in paths:
        sid = conn.get_next_available_stream_id()
        conn.send_headers(sid, [(b":method", b"GET"), (b":path", path.encode()), (b":scheme", b"https"),
                                (b":authority", b"localhost"), *headers], end_stream=True)
        writer.write(conn.data_to_send())
        await writer.drain()
        while sid not in statuses or statuses[sid][1] is False:
            data = await asyncio.wait_for(reader.read(65536), 5)
            assert data, "connection closed"
            for ev in conn.receive_data(data):
                if isinstance(ev, h2.events.ResponseReceived):
                    statuses[ev.stream_id] = (dict(ev.headers)[b":status"], False)
                elif isinstance(ev, h2.events.StreamEnded):
                    statuses[ev.stream_id] = (statuses[ev.stream_id][0], True)
            writer.write(conn.data_to_send())
    return [s for s, _ in statuses.values()]


def test_grants_used_over_http2(monkeypatch, tmp_path):
    async def main():
        test_ca = SessionCA("test upstream CA")
        server, port = await _h2_upstream(test_ca)
        monkeypatch.setattr(px, "_UPSTREAM_CA", test_ca.pem.decode())
        sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="g2-")) / "s.sock"
        ph = pol.make_placeholder(REAL)
        ca = SessionCA()
        reports = []
        a = Approvals(hold=1.0, allow_file=tmp_path / "allow.toml", on_grant_used=reports.append)
        policy = Policy()
        a.asker(FakeEgress("s1", {"TOKEN": ph}), policy)
        approval = a.request("secret", "s1", {"secret": "TOKEN", "host": "localhost", "method": "GET",
                                              "path": "/a", "header": "authorization"})
        a.decide(approval.id, "session")
        policy.rules[0] = dataclasses.replace(policy.rules[0], port=port)
        proxy = EgressProxy(sock, policy, secrets={"TOKEN": REAL}, placeholders={"TOKEN": ph}, ca=ca,
                            ask=None, session="s1", report=a.grant_used)
        await proxy.start()
        try:
            reader, writer = await Guest(sock, ca.pem).connect("localhost", port, alpn=("h2",))
            assert writer.get_extra_info("ssl_object").selected_alpn_protocol() == "h2"
            statuses = await _h2_get(reader, writer, ["/a?x=1", "/a"],
                                     [(b"authorization", f"Bearer {ph}".encode())])
            assert statuses == [b"200", b"200"]
            assert len(reports) == 1, reports
            assert reports[0]["kind"] == "secret" and reports[0]["path"] == "/a" and reports[0]["method"] == "GET"
            assert reports[0]["grant"]["approval"] == approval.id
            writer.close()
        finally:
            await proxy.stop()
            server.close()
    asyncio.run(main())
