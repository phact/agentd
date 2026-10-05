"""
The browser's policy proxy (agentd.devices.browser_proxy): CONNECT and
absolute-URI requests are relayed only to allowed hosts; one request per
connection, so a reused connection can't reach a second host; WebSocket
upgrades stay open.
"""
import asyncio

from aiohttp import web

from agentd.devices.browser_proxy import PolicyProxy


async def _upstream():
    async def hello(request):
        return web.Response(text=f"hello from {request.host}")

    async def ws(request):
        w = web.WebSocketResponse()
        await w.prepare(request)
        async for msg in w:
            await w.send_str("echo " + msg.data)
        return w
    app = web.Application()
    app.router.add_get("/", hello)
    app.router.add_get("/ws", ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def _raw(port, data: bytes, read_until_close=True) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    out = await asyncio.wait_for(r.read(65536) if not read_until_close else r.read(), 5)
    w.close()
    return out


def test_policy_proxy():
    async def main():
        runner, up = await _upstream()
        asked = []
        proxy = PolicyProxy(lambda host: asked.append(host) or host == "127.0.0.1")
        port = await proxy.start()
        try:
            out = await _raw(port, f"GET http://127.0.0.1:{up}/ HTTP/1.1\r\nHost: 127.0.0.1:{up}\r\n"
                                   "Proxy-Connection: keep-alive\r\n\r\n".encode())
            assert b"200 OK" in out and b"hello from 127.0.0.1" in out, out
            out = await _raw(port, b"GET http://evil.example/ HTTP/1.1\r\nHost: evil.example\r\n\r\n")
            assert out.startswith(b"HTTP/1.1 403") and b"evil.example isn't allowed" in out
            out = await _raw(port, b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n")
            assert out.startswith(b"HTTP/1.1 403")
            # CONNECT to an allowed host: a tunnel (here to a plain HTTP server, to see the bytes pass).
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"CONNECT 127.0.0.1:{up} HTTP/1.1\r\nHost: 127.0.0.1:{up}\r\n\r\n".encode())
            assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            w.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{up}\r\nConnection: close\r\n\r\n".encode())
            assert b"hello" in await asyncio.wait_for(r.read(), 5)
            w.close()
            # A second request on the same connection never reaches another host: the first is sent
            # with "Connection: close", and nothing after it is routed anywhere new.
            asked.clear()
            out = await _raw(port, f"GET http://127.0.0.1:{up}/ HTTP/1.1\r\nHost: 127.0.0.1:{up}\r\n\r\n"
                                   "GET http://evil.example/ HTTP/1.1\r\nHost: evil.example\r\n\r\n".encode())
            assert asked == ["127.0.0.1"] and out.startswith(b"HTTP/1."), "evil.example was never asked about or reached"
            # WebSocket through the proxy (absolute URI with Upgrade): stays open both ways.
            import aiohttp

            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"http://127.0.0.1:{up}/ws", proxy=f"http://127.0.0.1:{port}") as ws:
                    await ws.send_str("hi")
                    assert (await ws.receive(timeout=5)).data == "echo hi"
        finally:
            await proxy.stop()
            await runner.cleanup()
    asyncio.run(main())
