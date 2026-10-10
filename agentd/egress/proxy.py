"""The egress proxy: every connection out of a sandbox, one decision each.

agentd-net (the sandbox's network card) hands each guest TCP connection to
this proxy over a Unix socket, starting with a JSON line naming the
destination (``host`` when the guest resolved a name, ``dst`` / ``port``).

  * **intercept**: a host with fnox secret rules, on 443. TLS is terminated
    with the session CA (the sandbox trusts it). In each request, a
    placeholder in a rule's header is swapped for the real secret when the
    rule's method and path match; a placeholder anywhere it isn't allowed gets
    a 403 explaining which rule is missing. Responses are scrubbed of real
    values (streamed; same-length replacement). The upstream connection is
    opened first, so the sandbox is offered exactly the protocol the real
    server speaks (HTTP/2 or HTTP/1.1).
  * **pass**: allowed host/port: bytes spliced to the real destination,
    untouched (the server sees the client's own TLS).
  * **deny**: closed. With approvals, first held while an approver decides
    (agentd.egress.approvals).

Every decision goes to the audit log (never header values or bodies). What
an approval let through is also reported to the approvals (``grant.used``),
once per connection and grant.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Any, Callable

import h11

from agentd.egress.ca import SessionCA
from agentd.egress.policy import Grant, Policy, SecretRule

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_UPSTREAM_CA: str | None = None
RETRY_AFTER = 30  # seconds, in a pending approval's 403
ACCEPT = b"\x01"  # to agentd-net: let the guest's bytes flow (closing instead refuses)


def _approval(detail: dict, approval_id: str | None, status: str | None) -> str:
    """Add the approval behind a 403 to its detail; returns the note for its message."""
    if not approval_id:
        return ""
    if status == "deny":
        detail["approval"] = {"id": approval_id, "status": "deny"}
        return " (the human denied this)"
    detail["approval"] = {"id": approval_id, "status": "pending", "retry_after": RETRY_AFTER}
    return " (an approval is pending; retry later)"


def upstream_context(alpn: tuple[str, ...]) -> ssl.SSLContext:
    if _UPSTREAM_CA is not None:  # tests: trust a local test server
        ctx = ssl.create_default_context(cadata=_UPSTREAM_CA)
        ctx.set_alpn_protocols(list(alpn))
        return ctx
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(list(alpn))
    return ctx


class Scrubber:
    """Replaces real secret values in a byte stream, across chunk boundaries."""

    def __init__(self, replacements: dict[bytes, bytes]):
        self.replacements = {k: v for k, v in replacements.items() if k}
        self.keep = max((len(k) for k in self.replacements), default=1) - 1
        self.buf = b""

    def feed(self, data: bytes) -> bytes:
        if not self.replacements:
            return data
        self.buf += data
        for secret, mask in self.replacements.items():
            self.buf = self.buf.replace(secret, mask)
        if len(self.buf) <= self.keep:
            return b""
        out, self.buf = self.buf[: len(self.buf) - self.keep], self.buf[len(self.buf) - self.keep:]
        return out

    def flush(self) -> bytes:
        out, self.buf = self.buf, b""
        for secret, mask in self.replacements.items():
            out = out.replace(secret, mask)
        return out

    def text(self, value: bytes) -> bytes:
        for secret, mask in self.replacements.items():
            value = value.replace(secret, mask)
        return value


class Conn:
    """One sandbox connection: where it goes, and the grants already reported for it."""

    def __init__(self, host: str | None, ip: str, port: int):
        self.host, self.ip, self.port = host, ip, port
        self.via: tuple[Grant, dict] | None = None  # the grant that let it through, reported with its first request
        self.reported: set[tuple] = set()


class Refused(Exception):
    def __init__(self, message: str, detail: dict[str, Any]):
        super().__init__(message)
        self.detail = detail


class EgressProxy:
    def __init__(
        self,
        socket_path: Path,
        policy: Policy,
        *,
        secrets: dict[str, str],
        placeholders: dict[str, str],
        ca: SessionCA,
        audit_path: Path | None = None,
        session: str = "",
        ask: Callable[..., Any] | None = None,
        load: Callable[[str], Any] | None = None,
        report: Callable[[dict], None] | None = None,
    ):
        self.socket_path = Path(socket_path)
        self.policy = policy
        self.secrets = secrets            # name -> real value (host memory only)
        self.placeholders = placeholders  # name -> placeholder (what the sandbox holds)
        self.ca = ca
        self.audit_path = audit_path
        self.session = session
        self.ask = ask                    # approvals hook: await ask(kind, **details) -> (approved, id, status)
        self.load = load                  # await load(name): read a secret not read yet (add_secret)
        self.report = report              # report(event): a grant was used (Approvals.grant_used)
        self.masks: dict[bytes, bytes] = {}
        for name, value in secrets.items():
            self._mask(name, value)
        self._loop: asyncio.AbstractEventLoop | None = None
        if load is not None:
            from agentd import fnox

            fnox.on_clear(self)  # a vault changed: read again (or unlock) on next use
        self._server: asyncio.base_events.Server | None = None

    def _mask(self, name: str, value: str) -> None:
        ph = self.placeholders.get(name, "")
        self.masks[value.encode()] = ph.encode() if len(ph) == len(value) else b"*" * len(value)

    def forget_secrets(self) -> None:
        """Drop the values read so far (fnox.clear(): the vault changed). Masks stay, so
        responses are still scrubbed of old values."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self.secrets.clear)
                return
        self.secrets.clear()

    def add_secret(self, name: str, value: str) -> None:
        """A secret read after the session started (responses are scrubbed of it from now on)."""
        self.secrets[name] = value
        self._mask(name, value)

    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_unix_server(self._on_stream, path=str(self.socket_path))
        self.socket_path.chmod(0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    def audit(self, **entry: Any) -> None:
        if self.audit_path is None:
            return
        entry = {"ts": round(time.time(), 3), "session": self.session, **entry}
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.warning("egress audit log unwritable: %s", self.audit_path)

    def grant_used(self, conn: Conn, kind: str, grant: Grant, rule: dict, *, method: str | None = None,
                   path: str | None = None, secret: str | None = None) -> None:
        """Report a grant used on ``conn`` (once per connection and grant)."""
        key = (grant.approval, json.dumps(rule, sort_keys=True))
        if key in conn.reported:
            return
        conn.reported.add(key)
        event: dict[str, Any] = {
            "kind": kind, "grant": {"approval": grant.approval, "decision": grant.decision, "rule": rule},
            "host": conn.host, "ip": conn.ip, "port": conn.port}
        if method is not None:
            event.update(method=method, path=(path or "").split("?", 1)[0])
        if secret is not None:
            event["secret"] = secret
        self.audit(event="grant_used", **event)
        if self.report is not None:
            try:
                self.report({"session": self.session, "ts": round(time.time(), 3), **event})
            except Exception:
                logger.exception("grant report failed")

    def requested(self, conn: Conn, method: str, path: str, grants: list[tuple[Grant, dict, str]]) -> None:
        """A request on ``conn`` went upstream, filling secrets under ``grants``."""
        for grant, rule, secret in grants:
            self.grant_used(conn, "secret", grant, rule, method=method, path=path, secret=secret)
        if conn.via is not None and not conn.reported:
            self.grant_used(conn, "connect", *conn.via, method=method, path=path)

    # ------------------------------------------------------------------ #

    async def _on_stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = json.loads(await asyncio.wait_for(reader.readline(), 10))
            host, ip, port = header.get("host"), header.get("dst", ""), int(header.get("port", 0))
            conn = Conn(host, ip, port)
            decision, entry = self.policy.match(host, ip, port)
            approval_id, approval_status = None, None
            if decision == "deny" and self.ask is not None:
                approved, approval_id, approval_status = await self.ask("connect", host=host, ip=ip, port=port)
                if approved:
                    # A session/always grant is in the policy now
                    decision, entry = self.policy.match(host, ip, port)
                    if decision == "deny":  # approved once
                        decision = "pass"
                        conn.via = (Grant("once", approval_id), {"host": host or ip, "ports": [port]})
            if entry is not None and entry.grant is not None:
                conn.via = (entry.grant, entry.summary())
            self.audit(event="connect", host=host, ip=ip, port=port, decision=decision, approval=approval_id)
            if decision == "pass":
                if conn.via is not None:  # nothing more to see: report it now
                    self.grant_used(conn, "connect", *conn.via)
                await self._pass(reader, writer, host or ip, port)
            elif decision == "intercept":
                await self._intercept(reader, writer, conn)
            else:
                await self._explain(reader, writer, host, ip, port, approval_id, approval_status)
        except (asyncio.TimeoutError, ValueError, ConnectionError, OSError) as e:
            logger.debug("egress stream ended: %s", e)
        except Exception:
            logger.exception("egress stream failed")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _explain(self, reader, writer, host: str | None, ip: str, port: int, approval_id: str | None,
                       approval_status: str | None = None) -> None:
        """A refused HTTP(S) connection gets a 403 saying why (and about the
        pending approval) instead of a bare reset; other protocols are reset."""
        if not host or port not in (443, 80):
            return
        try:
            writer.write(ACCEPT)
            if port == 443:
                reader, writer = await _start_tls_server(reader, writer, self.ca.server_context(host, ("http/1.1",)))
            conn = h11.Connection(h11.SERVER)
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    data = await asyncio.wait_for(reader.read(_CHUNK), 30)
                    conn.receive_data(data)
                    continue
                break
            if not isinstance(event, h11.Request):
                return
            detail = {"host": host, "port": port}
            note = _approval(detail, approval_id, approval_status)
            body = json.dumps({"error": {"type": "agentd_egress", "message":
                               f"agentd egress: {host}:{port} isn't allowed from this sandbox" + note,
                               **detail}}).encode()
            writer.write(conn.send(h11.Response(status_code=403, headers=[
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"connection", b"close")])) + conn.send(h11.Data(data=body)) + conn.send(h11.EndOfMessage()))
            await writer.drain()
        except (ssl.SSLError, ConnectionError, OSError, asyncio.TimeoutError, h11.ProtocolError):
            pass

    async def _pass(self, reader, writer, host: str, port: int) -> None:
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        except (OSError, asyncio.TimeoutError):
            return  # closing without the accept byte resets the guest's connection
        writer.write(ACCEPT)
        await _splice(reader, writer, up_reader, up_writer)

    async def _intercept(self, reader, writer, conn: Conn) -> None:
        host, port = conn.host, conn.port
        # Learn what the real server speaks first, then let the sandbox choose
        # among those; the upstream connection used matches the sandbox's choice.
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=upstream_context(("h2", "http/1.1")), server_hostname=host), 15)
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
            self.audit(event="upstream_failed", host=host, port=port, error=str(e)[:200] or type(e).__name__)
            return
        ssl_obj = up_writer.get_extra_info("ssl_object")
        upstream_h2 = (ssl_obj.selected_alpn_protocol() if ssl_obj else None) == "h2"
        ctx = self.ca.server_context(host, ("h2", "http/1.1") if upstream_h2 else ("http/1.1",))
        try:
            # Accept and switch to TLS in the same step: the guest's first
            # bytes (its ClientHello) only start flowing after the accept byte.
            writer.write(ACCEPT)
            reader, writer = await _start_tls_server(reader, writer, ctx)
        except (ssl.SSLError, ConnectionError, OSError) as e:
            self.audit(event="tls_failed", host=host, error=str(e)[:200] or type(e).__name__)
            up_writer.close()
            return
        down_ssl = writer.get_extra_info("ssl_object")
        proto = (down_ssl.selected_alpn_protocol() if down_ssl else None) or "http/1.1"
        try:
            if proto == "h2":
                from agentd.egress.http2 import bridge_h2

                await bridge_h2(self, host, reader, writer, up_reader, up_writer, conn)
                return
            if upstream_h2:  # the sandbox wants HTTP/1.1: reconnect upstream to match
                up_writer.close()
                try:
                    up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(
                        host, port, ssl=upstream_context(("http/1.1",)), server_hostname=host), 15)
                except (OSError, asyncio.TimeoutError, ssl.SSLError) as e:
                    self.audit(event="upstream_failed", host=host, port=port, error=str(e)[:200] or type(e).__name__)
                    return
            await self._http1(conn, reader, writer, up_reader, up_writer)
        finally:
            up_writer.close()

    # ------------------------------------------------------------------ #
    # Requests
    # ------------------------------------------------------------------ #

    def inject(self, host: str, method: str, path: str, headers: list[tuple[bytes, bytes]],
               once: frozenset[str] = frozenset(), *, grants: list | None = None,
               once_grant: tuple[Grant, dict] | None = None) -> tuple[list[tuple[bytes, bytes]], list[str]]:
        """Swap placeholders for secrets where a rule allows it (or ``once``
        allows it for this request); Refused otherwise. ``grants`` collects
        (grant, rule, secret) for each secret filled because of an approval
        (``once_grant``: the one behind ``once``)."""
        rules = self.policy.rules_for(host)
        used: list[str] = []
        out = []
        for name, value in headers:
            lname = name.decode("latin-1").lower()
            for secret, ph in self.placeholders.items():
                bph = ph.encode()
                if not bph or bph not in value:
                    continue
                rule = next((r for r in rules if r.secret == secret and r.header == lname
                             and r.matches(method, path)), None)
                if (rule is not None or secret in once) and secret not in self.secrets:
                    raise Refused(f"agentd egress: {secret} couldn't be read from fnox on the host",
                                  {"secret": secret, "host": host, "method": method, "path": path, "header": lname,
                                   "unavailable": True})
                if rule is None and secret not in once:
                    allowed = [r.describe() for r in rules if r.secret == secret] or ["none"]
                    raise Refused(f"agentd egress: {secret} may not be sent in {lname} with {method} {host}{path}",
                                  {"secret": secret, "host": host, "method": method, "path": path, "header": lname,
                                   "rules": allowed})
                value = value.replace(bph, self.secrets[secret].encode())
                used.append(secret)
                if grants is not None:
                    if rule is not None and rule.grant is not None:
                        grants.append((rule.grant, rule.summary(), secret))
                    elif rule is None and once_grant is not None:
                        grants.append((*once_grant, secret))
                if rule is not None:
                    self.policy.use(rule)
            out.append((name, value))
        return out, used

    def refusal_body(self, e: Refused) -> bytes:
        detail = dict(e.detail)
        message = str(e) + _approval(detail, getattr(e, "approval_id", None), getattr(e, "approval_status", None))
        return json.dumps({"error": {"type": "agentd_egress", "message": message, **detail}}).encode()

    async def _load_needed(self, host: str, method: str, path: str, headers: list[tuple[bytes, bytes]],
                           once: frozenset[str] = frozenset()) -> None:
        """Read from fnox the secrets this request may send that weren't read yet. One locked in
        fnox is asked about (an ``unlock`` approval: the human's master password unlocks it)."""
        from agentd.fnox import SecretMissing

        if self.load is None:
            return
        rules = self.policy.rules_for(host)
        for name, value in headers:
            lname = name.decode("latin-1").lower()
            for secret, ph in list(self.placeholders.items()):
                if secret in self.secrets or not ph or ph.encode() not in value:
                    continue
                if not (secret in once or any(r.secret == secret and r.header == lname and r.matches(method, path)
                                              for r in rules)):
                    continue
                try:
                    await self.load(secret)
                    continue
                except SecretMissing:
                    pass
                detail = {"secret": secret, "host": host, "method": method, "path": path, "header": lname,
                          "locked": True}
                message = f"agentd egress: {secret} is locked in fnox"
                if self.ask is None:
                    raise Refused(message + " (and no approver is configured to unlock it)", detail)
                approved, approval_id, status = await self.ask("unlock", secrets=[secret], host=host)
                if approved:
                    try:
                        await self.load(secret)
                        continue
                    except SecretMissing:
                        pass
                e = Refused(message, detail)
                e.approval_id, e.approval_status = approval_id, status
                raise e

    async def inject_or_ask(self, host: str, method: str, path: str, headers: list[tuple[bytes, bytes]],
                            grants: list | None = None):
        """inject(), and when refused, hold for an approval: (headers, used) or Refused (with .approval_id
        and .approval_status). ``grants``: as for inject()."""
        await self._load_needed(host, method, path, headers)
        try:
            return self.inject(host, method, path, headers, grants=grants)
        except Refused as e:
            if self.ask is None or e.detail.get("unavailable"):
                raise
            keys = ("secret", "host", "method", "path", "header")
            approved, approval_id, status = await self.ask("secret", **{k: e.detail.get(k) for k in keys})
            if approved:
                once = frozenset({e.detail["secret"]})
                await self._load_needed(host, method, path, headers, once)
                rule = {"secret": e.detail["secret"], "host": host, "header": e.detail.get("header"),
                        "methods": [method], "paths": [path.split("?", 1)[0]]}
                return self.inject(host, method, path, headers, once=once, grants=grants,
                                   once_grant=(Grant(status, approval_id), rule))
            e.approval_id, e.approval_status = approval_id, status
            raise

    async def _http1(self, conn: Conn, reader, writer, up_reader, up_writer) -> None:
        host = conn.host
        down = h11.Connection(h11.SERVER)
        up = h11.Connection(h11.CLIENT)

        async def next_event(conn: h11.Connection, r: asyncio.StreamReader):
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    conn.receive_data(await r.read(_CHUNK))
                    continue
                return event

        while True:
            event = await next_event(down, reader)
            if not isinstance(event, h11.Request):
                return  # ConnectionClosed or a protocol error
            method = event.method.decode()
            path = event.target.decode("latin-1")
            grants: list = []
            try:
                headers, used = await self.inject_or_ask(host, method, path, list(event.headers), grants)
            except Refused as e:
                approval_id = getattr(e, "approval_id", None)
                self.audit(event="refused", host=host, method=method, path=path.split("?", 1)[0],
                           secret=e.detail.get("secret"), approval=approval_id)
                body = self.refusal_body(e)
                writer.write(down.send(h11.Response(status_code=403, headers=[
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close")])) + down.send(h11.Data(data=body)) + down.send(h11.EndOfMessage()))
                await writer.drain()
                return
            self.audit(event="request", host=host, method=method, path=path.split("?", 1)[0], secrets=used)
            self.requested(conn, method, path, grants)
            # Responses must stay scrubbable: no compression from the server.
            headers = [(n, v) for n, v in headers if n.lower() not in (b"accept-encoding", b"expect")]
            headers.append((b"accept-encoding", b"identity"))
            if down.they_are_waiting_for_100_continue:
                writer.write(down.send(h11.InformationalResponse(status_code=100, headers=[])))
                await writer.drain()
            up_writer.write(up.send(h11.Request(method=event.method, target=event.target, headers=headers)))
            # request body
            while True:
                ev = await next_event(down, reader)
                if isinstance(ev, h11.Data):
                    up_writer.write(up.send(h11.Data(data=ev.data)))
                    await up_writer.drain()
                elif isinstance(ev, h11.EndOfMessage):
                    up_writer.write(up.send(h11.EndOfMessage()))
                    await up_writer.drain()
                    break
                else:
                    return
            # response
            scrub = Scrubber(self.masks)
            while True:
                ev = await next_event(up, up_reader)
                if isinstance(ev, h11.InformationalResponse):
                    writer.write(down.send(ev))
                elif isinstance(ev, h11.Response):
                    writer.write(down.send(h11.Response(
                        status_code=ev.status_code, reason=ev.reason, http_version=ev.http_version,
                        headers=[(n, scrub.text(v)) for n, v in ev.headers])))
                elif isinstance(ev, h11.Data):
                    out = scrub.feed(ev.data)
                    if out:
                        writer.write(down.send(h11.Data(data=out)))
                elif isinstance(ev, h11.EndOfMessage):
                    rest = scrub.flush()
                    if rest:
                        writer.write(down.send(h11.Data(data=rest)))
                    writer.write(down.send(h11.EndOfMessage(headers=[(n, scrub.text(v)) for n, v in ev.headers])))
                    await writer.drain()
                    break
                else:
                    return
                await writer.drain()
            if down.our_state is h11.MUST_CLOSE or up.our_state is h11.MUST_CLOSE \
                    or up.their_state is h11.MUST_CLOSE or down.their_state is h11.MUST_CLOSE:
                return
            try:
                down.start_next_cycle()
                up.start_next_cycle()
            except h11.LocalProtocolError:
                return


async def _start_tls_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                            ctx: ssl.SSLContext) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """TLS on an accepted stream, as the server (Python 3.10-compatible)."""
    loop = asyncio.get_running_loop()
    protocol = writer.transport.get_protocol()
    transport = await asyncio.wait_for(
        loop.start_tls(writer.transport, protocol, ctx, server_side=True), 15)
    new_writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    if hasattr(protocol, "_replace_writer"):
        protocol._replace_writer(new_writer)
    protocol._over_ssl = True  # the stream is now TLS (set at connection time otherwise)
    # Keep the plain writer alive: Python 3.12+ closes a StreamWriter's transport
    # when it's garbage-collected, which would cut the TLS connection under us.
    new_writer._agentd_plain_writer = writer
    return reader, new_writer


async def _splice(r1, w1, r2, w2) -> None:
    async def pipe(r, w):
        try:
            while data := await r.read(_CHUNK):
                w.write(data)
                await w.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                if w.can_write_eof():
                    w.write_eof()
            except (OSError, RuntimeError):
                pass

    await asyncio.gather(pipe(r1, w2), pipe(r2, w1))
    for w in (w1, w2):
        try:
            w.close()
        except Exception:
            pass
