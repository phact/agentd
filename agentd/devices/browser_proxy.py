"""A local proxy for agentd's Chrome that checks every connection's host.

Request interception (CDP ``Fetch``) sees HTTP from the targets agentd attached
to, but not WebSockets, and a target it never attached to sees none. Pointing
Chrome at this proxy (``--proxy-server``, loopback included) puts one more
check under everything: ``CONNECT host:port`` (HTTPS, WSS) and absolute-URI
requests (HTTP, WS) are refused with a 403 unless ``allowed(host)`` says yes.
Bytes are relayed untouched, so sites see Chrome's own TLS.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_CHUNK = 64 * 1024


class PolicyProxy:
    def __init__(self, allowed: Callable[[str], bool]):
        self.allowed = allowed
        self.port: int | None = None
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass
            self._server = None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            line, _, rest = head.partition(b"\r\n")
            method, target, version = line.decode("latin-1").split(" ", 2)
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                host = host.strip("[]")
                if not self.allowed(host):
                    return await _refuse(writer, host)
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 15)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                url = urlsplit(target)
                host = url.hostname or ""
                if not host or not self.allowed(host):
                    return await _refuse(writer, host or target)
                up_r, up_w = await asyncio.wait_for(
                    asyncio.open_connection(host, url.port or (443 if url.scheme == "https" else 80)), 15)
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                headers = [h for h in rest.split(b"\r\n") if h]
                upgrade = any(h.lower().startswith(b"upgrade:") for h in headers)
                # One request per connection (unless it upgrades), so a reused proxy
                # connection can't carry a request for another host past the check.
                kept = [h for h in headers if not h.lower().startswith((b"proxy-connection:", b"connection:"))
                        or (upgrade and h.lower().startswith(b"connection:"))]
                if not upgrade:
                    kept.append(b"Connection: close")
                up_w.write(f"{method} {path} {version}\r\n".encode("latin-1") + b"\r\n".join(kept) + b"\r\n\r\n")
                await up_w.drain()
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError,
                OSError, ValueError):
            pass
        except Exception:
            logger.exception("browser proxy failed")
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def _refuse(writer: asyncio.StreamWriter, host: str) -> None:
    body = f"agentd: {host} isn't allowed from this browser".encode()
    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: "
                 + str(len(body)).encode() + b"\r\n\r\n" + body)
    await writer.drain()


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(_CHUNK):
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass
