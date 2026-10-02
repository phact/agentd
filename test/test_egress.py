"""
Egress (agentd.egress): policy and fnox rules, placeholders, the scrubber,
the per-session CA, and the proxy end to end on the host (a stand-in for
agentd-net, a local HTTPS upstream): injection, refusal, scrubbing,
keep-alive, large and chunked bodies, pass-through and denial. With
AGENTD_LIVE=1, real sandboxes (native libkrun and Colima) reaching real
services through it.
"""
import asyncio
import json
import os
import shutil
import ssl
import tempfile
from pathlib import Path

import pytest

from agentd.egress import policy as pol
from agentd.egress import proxy as px
from agentd.egress.ca import SessionCA
from agentd.egress.policy import Allow, Policy, SecretRule
from agentd.egress.proxy import EgressProxy, Refused, Scrubber

REAL = "tok_REALSECRETxyz1234567890abcdef"


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #

def test_rules_and_allows():
    r = SecretRule("GH", "api.github.com", methods=("GET",), paths=("/repos/me/**",))
    assert r.matches("get", "/repos/me/app/pulls?state=open") and not r.matches("POST", "/repos/me/app")
    assert not r.matches("GET", "/repos/other/app")
    assert SecretRule("X", "h", paths=("/v1/*/items",)).matches("GET", "/v1/a/items")
    assert not SecretRule("X", "h", paths=("/v1/*/items",)).matches("GET", "/v1/a/b/items"), "* stays in a segment"
    a = Allow.parse("pypi.org")
    assert a.ports == (443, 80) and a.matches("pypi.org", "10.212.0.1", 443) and not a.matches("pypi.org", "", 22)
    assert Allow.parse("github.com:22").matches("github.com", "", 22)
    assert Allow.parse("*.example.com").matches("a.example.com", "", 443)
    assert Allow.parse("10.0.2.58:8001").matches(None, "10.0.2.58", 8001)
    p = Policy(rules=[r], allows=[a])
    assert p.connect("api.github.com", "", 443) == "intercept"
    assert p.connect("pypi.org", "", 443) == "pass"
    assert p.connect("evil.com", "", 443) == "deny" and p.connect(None, "1.2.3.4", 443) == "deny"


def test_fnox_rules_and_grants(tmp_path):
    cfg = tmp_path / "fnox.toml"
    cfg.write_text('[[proxy.rules]]\nsecret = "GH"\ndomain = "API.github.com"\nmethods = ["get"]\npaths = ["/**"]\n'
                   '[[proxy.rules]]\nsecret = "NPM"\ndomain = "registry.npmjs.org"\nheader = "Authorization"\n')
    rules = pol.load_rules([cfg])
    assert rules[0] == SecretRule("GH", "api.github.com", "authorization", ("GET",), ())
    assert rules[1].header == "authorization" and rules[1].methods == ()
    pol.add_rule_to_fnox(cfg, SecretRule("GH", "api.github.com", methods=("POST",), paths=("/repos/me/app/pulls",)))
    assert pol.load_rules([cfg])[-1] == SecretRule("GH", "api.github.com", "authorization", ("POST",),
                                                    ("/repos/me/app/pulls",))


def test_placeholders():
    ph = pol.make_placeholder("ghp_" + "a" * 36)
    assert ph.startswith("ghp_") and len(ph) == 40 and ph != "ghp_" + "a" * 36
    assert len(pol.make_placeholder("short")) >= 8


@pytest.mark.skipif(shutil.which("fnox") is None, reason="needs fnox")
def test_fnox_discovery_and_get(tmp_path):
    (tmp_path / "fnox.toml").write_text('[providers.plain]\ntype = "plain"\n[secrets]\n'
                                        'T = { provider = "plain", value = "v4lue", env = false }\n'
                                        '[[proxy.rules]]\nsecret = "T"\ndomain = "example.com"\n')
    files = pol.fnox_config_files(tmp_path)
    assert (tmp_path / "fnox.toml").resolve() in [f.resolve() for f in files]
    assert pol.fnox_get("T", tmp_path) == "v4lue"
    with pytest.raises(RuntimeError):
        pol.fnox_get("MISSING", tmp_path)


def test_secret_names_from_fnox_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FNOX_PROFILE", raising=False)
    cfg = tmp_path / "fnox.toml"
    cfg.write_text('[providers.plain]\ntype = "plain"\n[secrets]\n'
                   'GH = { provider = "plain", value = "ghp_x", description = "GitHub PAT" }\n'
                   'DB = { provider = "plain", value = "pw" }\n'
                   '[profiles.work.secrets]\nWORK = { provider = "plain", value = "w", description = "work" }\n')
    assert pol.load_secret_names([cfg]) == {"GH": "GitHub PAT", "DB": ""}
    assert pol.load_secret_names([cfg], "work") == {"GH": "GitHub PAT", "DB": "", "WORK": "work"}
    ph = pol.opaque_placeholder()
    assert ph.startswith("agentd_ph_") and len(ph) == 34 and ph != pol.opaque_placeholder()


def test_scrubber_across_chunks():
    s = Scrubber({b"SECRET": b"######"})
    out = b"".join(s.feed(c) for c in [b"xxSEC", b"RE", b"Tyy SE", b"CRET"]) + s.flush()
    assert out == b"xx######yy ######"
    assert Scrubber({}).feed(b"abc") == b"abc"


# --------------------------------------------------------------------------- #
# The proxy on the host, against a local HTTPS upstream
# --------------------------------------------------------------------------- #

class Guest:
    """What agentd-net does: header, wait for the accept byte, then bytes."""

    def __init__(self, sock: Path, ca_pem: bytes):
        self.sock, self.ca_pem = sock, ca_pem

    async def connect(self, host, port, tls=True, alpn=("http/1.1",)):
        reader, writer = await asyncio.open_unix_connection(str(self.sock))
        writer.write(json.dumps({"v": 1, "src": "10.211.0.2:1", "dst": "10.212.0.1", "port": port,
                                 "host": host}).encode() + b"\n")
        accept = await reader.read(1)
        if accept != b"\x01":
            writer.close()
            return None
        if not tls:
            return reader, writer
        ctx = ssl.create_default_context(cadata=self.ca_pem.decode())
        ctx.set_alpn_protocols(list(alpn))
        protocol = writer.transport.get_protocol()
        transport = await asyncio.get_running_loop().start_tls(writer.transport, protocol, ctx, server_hostname=host)
        tls_writer = asyncio.StreamWriter(transport, protocol, reader, asyncio.get_running_loop())
        tls_writer._plain = writer  # see agentd.egress.proxy._start_tls_server
        if hasattr(protocol, "_replace_writer"):
            protocol._replace_writer(tls_writer)
        return reader, tls_writer


async def _upstream(seen: list):
    """A local HTTPS server (cert for "localhost" from a test CA) that echoes headers."""
    from aiohttp import web

    test_ca = SessionCA("test upstream CA")

    async def echo(request):
        body = await request.read()
        seen.append({"auth": request.headers.get("authorization"), "path": request.path, "len": len(body),
                     "accept_encoding": request.headers.get("accept-encoding")})
        if request.path == "/chunked":
            resp = web.StreamResponse()
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            for part in (b"start ", REAL.encode()[:7], REAL.encode()[7:], b" end"):
                await resp.write(part)
            await resp.write_eof()
            return resp
        return web.json_response({"you_sent": request.headers.get("authorization"), "len": len(body)},
                                 headers={"x-echo": request.headers.get("authorization", "")})

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", echo)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=test_ca.server_context("localhost", ("http/1.1",)))
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port, test_ca


async def _request(reader, writer, method, path, headers=b"", body=b""):
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n".encode() + headers
                 + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    length = None
    chunked = b"transfer-encoding: chunked" in head.lower()
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    if chunked:
        data = b""
        while True:
            size = int((await reader.readuntil(b"\r\n")).strip(), 16)
            chunk = await reader.readexactly(size + 2)
            if size == 0:
                break
            data += chunk[:-2]
        return head, data
    return head, await reader.readexactly(length or 0)


def test_proxy_injects_refuses_scrubs_and_streams(monkeypatch, tmp_path):
    async def main():
        seen = []
        runner, port, test_ca = await _upstream(seen)
        monkeypatch.setattr(px, "_UPSTREAM_CA", test_ca.pem.decode())
        sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="eg-")) / "streams.sock"
        ph = pol.make_placeholder(REAL)
        rules = [SecretRule("TOKEN", "localhost", methods=("GET", "PUT"), paths=("/ok/**",), port=port)]
        ca = SessionCA()
        proxy = EgressProxy(sock, Policy(rules=rules, allows=[Allow.parse(f"localhost:{port + 1}")]),
                            secrets={"TOKEN": REAL}, placeholders={"TOKEN": ph}, ca=ca,
                            audit_path=tmp_path / "audit.jsonl", session="t")
        await proxy.start()
        try:
            guest = Guest(sock, ca.pem)
            reader, writer = await guest.connect("localhost", port)
            auth = f"Authorization: Bearer {ph}\r\n".encode()
            head, body = await _request(reader, writer, "GET", "/ok/a", auth)
            assert b"200" in head.split(b"\r\n")[0]
            assert seen[-1]["auth"] == f"Bearer {REAL}", "the real value reached the server"
            assert seen[-1]["accept_encoding"] == "identity"
            assert REAL.encode() not in head + body and ph.encode() in body, "echoes are scrubbed back"
            # keep-alive: a second request on the same connection, with a 5 MB body
            big = os.urandom(5 * 1024 * 1024)
            head, body = await _request(reader, writer, "PUT", "/ok/big", auth, big)
            assert seen[-1]["len"] == len(big) and json.loads(body)["len"] == len(big)
            # wrong path: refused, connection closed
            head, body = await _request(reader, writer, "GET", "/nope", auth)
            assert b"403" in head.split(b"\r\n")[0]
            err = json.loads(body)["error"]
            assert err["secret"] == "TOKEN" and err["path"] == "/nope" and "TOKEN -> localhost" in err["rules"][0]
            writer.close()

            # no placeholder: passes through untouched
            reader, writer = await guest.connect("localhost", port)
            head, body = await _request(reader, writer, "GET", "/anything", b"Authorization: Bearer mine\r\n")
            assert seen[-1]["auth"] == "Bearer mine"
            writer.close()

            # denied: HTTPS gets a 403 saying why; other ports are closed before any byte flows
            reader, writer = await guest.connect("evil.example", 443)
            head, body = await _request(reader, writer, "GET", "/", b"")
            assert b"403" in head.split(b"\r\n")[0] and "isn't allowed" in json.loads(body)["error"]["message"]
            writer.close()
            assert await guest.connect("evil.example", 5432, tls=False) is None
            audit = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
            assert {"event": "refused", "secret": "TOKEN"}.items() <= next(a for a in audit if a["event"] == "refused").items()
            assert any(a["event"] == "connect" and a["decision"] == "deny" for a in audit)
            assert all(REAL not in json.dumps(a) for a in audit), "the audit log never holds secrets"
        finally:
            await proxy.stop()
            await runner.cleanup()

    asyncio.run(main())


def test_proxy_scrubs_chunked_split_secrets(monkeypatch, tmp_path):
    async def main():
        seen = []
        runner, port, test_ca = await _upstream(seen)
        monkeypatch.setattr(px, "_UPSTREAM_CA", test_ca.pem.decode())
        sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="eg-")) / "streams.sock"
        ph = pol.make_placeholder(REAL)
        ca = SessionCA()
        proxy = EgressProxy(sock, Policy(rules=[SecretRule("TOKEN", "localhost", port=port)]),
                            secrets={"TOKEN": REAL}, placeholders={"TOKEN": ph}, ca=ca)
        await proxy.start()
        try:
            reader, writer = await Guest(sock, ca.pem).connect("localhost", port)
            head, body = await _request(reader, writer, "GET", "/chunked", f"Authorization: {ph}\r\n".encode())
            assert body == b"start " + ph.encode() + b" end"
            writer.close()
        finally:
            await proxy.stop()
            await runner.cleanup()

    asyncio.run(main())


def test_ca_contexts():
    ca = SessionCA()
    assert b"BEGIN CERTIFICATE" in ca.pem
    ctx = ca.server_context("api.github.com", ("h2", "http/1.1"))
    assert ca.server_context("api.github.com", ("h2", "http/1.1")) is ctx, "cached per host"


# --------------------------------------------------------------------------- #
# Live: real sandboxes, real services
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not os.environ.get("AGENTD_LIVE") or shutil.which("fnox") is None,
                    reason="set AGENTD_LIVE=1 (needs fnox and internet)")
@pytest.mark.parametrize("backend", ["krun", "krun-colima"])
def test_live_sandbox_egress(backend, tmp_path):
    from agentd.egress import Egress
    from agentd.sandbox.base import DEFAULT_HOME
    from agentd.sandbox.executor import KrunExecutor, colima_available, krun_available

    if not {"krun": krun_available, "krun-colima": colima_available}[backend]():
        pytest.skip(f"{backend} is not set up")
    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=DEFAULT_HOME / "tmp", prefix="egress-"))
    try:
        # httpbin's basic-auth only says 200 to the right credentials: proof of injection.
        (ws / "fnox.toml").write_text(
            '[providers.plain]\ntype = "plain"\n[secrets]\n'
            f'TOKEN = {{ provider = "plain", value = "{REAL}", env = false }}\n'
            'BASIC = { provider = "plain", value = "dXNlcjpwYXNzd2Q=", env = false }\n'
            '[[proxy.rules]]\nsecret = "TOKEN"\ndomain = "httpbin.org"\nmethods = ["GET"]\npaths = ["/anything/**"]\n'
            '[[proxy.rules]]\nsecret = "BASIC"\ndomain = "httpbin.org"\npaths = ["/basic-auth/**"]\n')
        egress = Egress(allow=("pypi.org", "github.com:22"), audit=tmp_path / "audit.jsonl")
        with KrunExecutor(egress=egress, colima=True if backend == "krun-colima" else None) as ex:
            def sh(cmd):
                out, code = ex.execute_bash(cmd, ws)
                assert REAL not in out and "dXNlcjpwYXNzd2Q=" not in out, "a real secret reached the sandbox"
                return out, code

            assert sh("echo $TOKEN")[0] != REAL
            out, _ = sh('curl -sS --http2 https://httpbin.org/basic-auth/user/passwd -H "Authorization: Basic $BASIC"')
            assert json.loads(out) == {"authenticated": True, "user": "user"}, out
            out, _ = sh('curl -sS --http1.1 https://httpbin.org/basic-auth/user/passwd -H "Authorization: Basic $BASIC"')
            assert json.loads(out)["authenticated"] is True
            out, _ = sh('curl -sS https://httpbin.org/anything/x -H "Authorization: Bearer $TOKEN"')
            assert json.loads(out)["headers"]["Authorization"].startswith("Bearer tok_"), "echo scrubbed to the placeholder"
            out, _ = sh('curl -sS -X POST https://httpbin.org/anything/x -H "Authorization: Bearer $TOKEN"')
            assert json.loads(out)["error"]["secret"] == "TOKEN"
            out, _ = sh("curl -sS -m 10 https://example.com/")
            assert "isn't allowed" in json.loads(out)["error"]["message"], "unlisted hosts get a 403 saying why"
            out, _ = sh("curl -sS -m 20 -o /dev/null -w '%{http_code}' https://pypi.org/simple/six/")
            assert out.strip() == "200"
            out, _ = sh("python3 -c \"import socket; print(socket.create_connection(('github.com', 22), 10).recv(8))\"")
            assert "SSH-2.0" in out
    finally:
        shutil.rmtree(ws, ignore_errors=True)
