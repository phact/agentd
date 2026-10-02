"""HTTP/2 interception: two h2 connections bridged stream by stream.

Each request stream from the sandbox gets placeholder injection (or a 403 on
that stream alone), then becomes a stream on the upstream connection;
response headers, data and trailers come back scrubbed. Data is acknowledged
to a side only once it has been forwarded, so flow control propagates end to
end (a slow server slows the client instead of filling our memory).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import h2.exceptions

if TYPE_CHECKING:
    from agentd.egress.proxy import EgressProxy

_CHUNK = 64 * 1024
logger = logging.getLogger(__name__)


class _Side:
    def __init__(self, conn: h2.connection.H2Connection, writer: asyncio.StreamWriter):
        self.conn = conn
        self.writer = writer
        # stream id -> deque of [data, end_stream, (other side, other stream, bytes to ack)]
        self.pending: dict[int, deque] = {}

    def flush(self) -> None:
        data = self.conn.data_to_send()
        if data:
            self.writer.write(data)


def _queue(side: _Side, sid: int, data: bytes, end: bool, ack: tuple | None) -> None:
    side.pending.setdefault(sid, deque()).append([data, end, ack])
    _drain(side, sid)


def _drain(side: _Side, sid: int) -> None:
    q = side.pending.get(sid)
    while q:
        item = q[0]
        data, end, ack = item
        try:
            while data:
                window = min(side.conn.local_flow_control_window(sid), side.conn.max_outbound_frame_size)
                if window <= 0:
                    item[0] = data
                    return
                side.conn.send_data(sid, data[:window])
                data = data[window:]
            if end:
                side.conn.end_stream(sid)
        except (h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError, KeyError):
            q.clear()
            break
        q.popleft()
        if ack is not None:
            other, other_sid, n = ack
            if n:
                try:
                    other.conn.acknowledge_received_data(n, other_sid)
                except (h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError, KeyError):
                    pass
    side.pending.pop(sid, None)


async def bridge_h2(proxy: "EgressProxy", host: str, down_r, down_w, up_r, up_w) -> None:
    from agentd.egress.proxy import Refused, Scrubber

    down = _Side(h2.connection.H2Connection(h2.config.H2Configuration(
        client_side=False, header_encoding=None)), down_w)
    up = _Side(h2.connection.H2Connection(h2.config.H2Configuration(
        client_side=True, header_encoding=None)), up_w)
    down.conn.initiate_connection()
    up.conn.initiate_connection()
    down.flush()
    up.flush()
    d2u: dict[int, int] = {}
    u2d: dict[int, int] = {}
    scrubbers: dict[int, Scrubber] = {}   # by upstream stream id
    refused: set[int] = set()             # downstream streams answered with a 403
    ended_up: set[int] = set()            # upstream streams we already ended (never end twice)

    queue: asyncio.Queue = asyncio.Queue()

    async def read(side_name: str, r: asyncio.StreamReader) -> None:
        try:
            while data := await r.read(_CHUNK):
                await queue.put((side_name, data))
        except (ConnectionError, OSError):
            pass
        await queue.put((side_name, None))

    readers = [asyncio.ensure_future(read("down", down_r)), asyncio.ensure_future(read("up", up_r))]
    try:
        while True:
            side_name, data = await queue.get()
            if data is None:
                logger.debug("h2 bridge %s: %s side closed", host, side_name)
                return
            side = down if side_name == "down" else up
            try:
                events = side.conn.receive_data(data)
            except h2.exceptions.ProtocolError as e:
                logger.warning("h2 bridge %s: protocol error from the %s side: %s", host, side_name, e)
                return
            for ev in events:
                if side is down:
                    if isinstance(ev, h2.events.RequestReceived):
                        hdrs = list(ev.headers)
                        pseudo = {k: v for k, v in hdrs if k.startswith(b":")}
                        method = pseudo.get(b":method", b"GET").decode()
                        path = pseudo.get(b":path", b"/").decode("latin-1")
                        regular = [(k, v) for k, v in hdrs if not k.startswith(b":")]
                        try:
                            regular, used = await proxy.inject_or_ask(host, method, path, regular)
                        except Refused as e:
                            approval_id = getattr(e, "approval_id", None)
                            proxy.audit(event="refused", host=host, method=method, path=path.split("?", 1)[0],
                                        secret=e.detail.get("secret"), http="2", approval=approval_id)
                            body = proxy.refusal_body(e, approval_id)
                            down.conn.send_headers(ev.stream_id, [(b":status", b"403"),
                                                                  (b"content-type", b"application/json"),
                                                                  (b"content-length", str(len(body)).encode())])
                            _queue(down, ev.stream_id, body, True, None)
                            refused.add(ev.stream_id)
                            continue
                        proxy.audit(event="request", host=host, method=method, path=path.split("?", 1)[0],
                                    secrets=used, http="2")
                        regular = [(k, v) for k, v in regular if k.lower() != b"accept-encoding"]
                        regular.append((b"accept-encoding", b"identity"))
                        usid = up.conn.get_next_available_stream_id()
                        d2u[ev.stream_id], u2d[usid] = usid, ev.stream_id
                        scrubbers[usid] = Scrubber(proxy.masks)
                        ends = ev.stream_ended is not None
                        up.conn.send_headers(usid, [(k, v) for k, v in hdrs if k.startswith(b":")] + regular,
                                             end_stream=ends)
                        if ends:
                            ended_up.add(usid)
                    elif isinstance(ev, h2.events.DataReceived):
                        if ev.stream_id in refused:
                            down.conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                        elif ev.stream_id in d2u:
                            _queue(up, d2u[ev.stream_id], ev.data, False,
                                   (down, ev.stream_id, ev.flow_controlled_length))
                    elif isinstance(ev, h2.events.StreamEnded):
                        usid = d2u.get(ev.stream_id)
                        if usid is not None and ev.stream_id not in refused and usid not in ended_up:
                            ended_up.add(usid)
                            _queue(up, usid, b"", True, None)
                    elif isinstance(ev, h2.events.StreamReset):
                        usid = d2u.pop(ev.stream_id, None)
                        if usid is not None:
                            u2d.pop(usid, None)
                            try:
                                up.conn.reset_stream(usid)
                            except h2.exceptions.ProtocolError:
                                pass
                    elif isinstance(ev, h2.events.WindowUpdated):
                        for sid in list(down.pending):
                            _drain(down, sid)
                    elif isinstance(ev, h2.events.ConnectionTerminated):
                        return
                else:
                    dsid = u2d.get(getattr(ev, "stream_id", 0) or 0)
                    if isinstance(ev, (h2.events.ResponseReceived, h2.events.InformationalResponseReceived)):
                        if dsid is not None:
                            scrub = scrubbers[ev.stream_id]
                            down.conn.send_headers(dsid, [(k, scrub.text(v)) for k, v in ev.headers],
                                                   end_stream=getattr(ev, "stream_ended", None) is not None)
                    elif isinstance(ev, h2.events.DataReceived):
                        if dsid is None:
                            up.conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                        else:
                            out = scrubbers[ev.stream_id].feed(ev.data)
                            _queue(down, dsid, out, False, (up, ev.stream_id, ev.flow_controlled_length))
                    elif isinstance(ev, h2.events.TrailersReceived):
                        if dsid is not None:
                            scrub = scrubbers[ev.stream_id]
                            rest = scrub.flush()
                            if rest:
                                _queue(down, dsid, rest, False, None)
                            down.conn.send_headers(dsid, [(k, scrub.text(v)) for k, v in ev.headers], end_stream=True)
                            scrubbers[ev.stream_id] = Scrubber({})
                    elif isinstance(ev, h2.events.StreamEnded):
                        if dsid is not None:
                            rest = scrubbers[ev.stream_id].flush()
                            try:
                                _queue(down, dsid, rest, True, None)
                            except h2.exceptions.ProtocolError:
                                pass
                            d2u.pop(dsid, None)
                            u2d.pop(ev.stream_id, None)
                            scrubbers.pop(ev.stream_id, None)
                    elif isinstance(ev, h2.events.StreamReset):
                        if dsid is not None:
                            try:
                                down.conn.reset_stream(dsid, error_code=ev.error_code)
                            except h2.exceptions.ProtocolError:
                                pass
                            d2u.pop(dsid, None)
                            u2d.pop(ev.stream_id, None)
                    elif isinstance(ev, h2.events.WindowUpdated):
                        for sid in list(up.pending):
                            _drain(up, sid)
                    elif isinstance(ev, h2.events.ConnectionTerminated):
                        return
            down.flush()
            up.flush()
            await asyncio.gather(down.writer.drain(), up.writer.drain())
    finally:
        for task in readers:
            task.cancel()
