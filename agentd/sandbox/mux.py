"""Stream multiplexer for the host <-> sandbox connection.

One persistent connection (the host-dialed vsock port) carries many
independent byte streams: command execs opened by the host, and bridge /
model-API connections opened from inside the sandbox. Same idea as yamux,
kept small and stdlib-only because this module runs on both sides (host
agentd and ``sandboxd`` inside the microVM).

Wire format, every frame::

    stream_id: u32 | type: u8 | length: u32 | payload: bytes[length]

Stream ids are odd when the host opened the stream and even when the
sandbox did, so the two sides never collide.

Frame types:
    HELLO   sandbox -> host once the connection is ready (stream 0)
    OPEN    open a stream; payload is JSON metadata (``{"kind": ...}``)
    DATA    stream bytes
    EOF     sender will write no more on this stream (half close)
    CLOSE   stream finished; optional JSON payload (e.g. ``{"exit": 0}``)
    WINDOW  grant the peer ``u32`` more bytes of send credit on the stream

Flow control is credit based: each side may have at most ``WINDOW_SIZE``
unacknowledged bytes in flight per stream, and the receiver returns credit
as the application consumes data. A slow reader therefore stalls only its
own stream, not the whole connection.
"""
from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, Awaitable, Callable

HELLO, OPEN, DATA, EOF, CLOSE, WINDOW = range(1, 7)

_HEADER = struct.Struct(">IBI")
MAX_FRAME = 64 * 1024
WINDOW_SIZE = 256 * 1024


class MuxClosed(ConnectionError):
    """The underlying connection went away."""


class Stream:
    """One logical bidirectional byte stream on a :class:`Mux`."""

    def __init__(self, mux: "Mux", stream_id: int, meta: dict[str, Any]):
        self.mux = mux
        self.id = stream_id
        self.meta = meta
        self.close_meta: dict[str, Any] | None = None
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._buffer = b""
        self._eof = False
        self._send_credit = WINDOW_SIZE
        self._credit_event = asyncio.Event()
        self._credit_event.set()
        self._unacked = 0
        self._write_eof_sent = False
        self._closed = asyncio.Event()

    # -- receiving -----------------------------------------------------

    async def read(self, n: int = -1) -> bytes:
        """Return up to ``n`` bytes (all buffered if ``n < 0``); b"" at EOF."""
        if not self._buffer and not self._eof:
            chunk = await self._chunks.get()
            if chunk is None:
                self._eof = True
            else:
                self._buffer = chunk
        if n < 0 or n >= len(self._buffer):
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:n], self._buffer[n:]
        await self._ack(len(data))
        return data

    async def read_all(self) -> bytes:
        parts = []
        while True:
            chunk = await self.read()
            if not chunk:
                return b"".join(parts)
            parts.append(chunk)

    async def _ack(self, n: int) -> None:
        if not n:
            return
        self._unacked += n
        if self._unacked >= WINDOW_SIZE // 2 and not self._closed.is_set():
            grant, self._unacked = self._unacked, 0
            await self.mux._send(self.id, WINDOW, struct.pack(">I", grant))

    # -- sending -------------------------------------------------------

    async def write(self, data: bytes) -> None:
        if self._closed.is_set():
            raise MuxClosed(f"stream {self.id} closed")
        view = memoryview(data)
        while view:
            while self._send_credit <= 0:
                if self._closed.is_set():
                    raise MuxClosed(f"stream {self.id} closed")
                self._credit_event.clear()
                await self._credit_event.wait()
            n = min(len(view), MAX_FRAME, self._send_credit)
            self._send_credit -= n
            await self.mux._send(self.id, DATA, bytes(view[:n]))
            view = view[n:]

    async def write_eof(self) -> None:
        if not self._write_eof_sent:
            self._write_eof_sent = True
            await self.mux._send(self.id, EOF, b"")

    async def close(self, meta: dict[str, Any] | None = None) -> None:
        if self._closed.is_set():
            return
        payload = json.dumps(meta).encode() if meta else b""
        try:
            await self.mux._send(self.id, CLOSE, payload)
        except MuxClosed:
            pass
        self._finish(meta)

    async def wait_closed(self) -> dict[str, Any] | None:
        """Wait for the peer (or us) to close; returns the CLOSE metadata."""
        await self._closed.wait()
        return self.close_meta

    # -- driven by Mux -------------------------------------------------

    def _feed(self, data: bytes) -> None:
        self._chunks.put_nowait(data)

    def _feed_eof(self) -> None:
        self._chunks.put_nowait(None)

    def _grant(self, n: int) -> None:
        self._send_credit += n
        self._credit_event.set()

    def _finish(self, meta: dict[str, Any] | None) -> None:
        if self._closed.is_set():
            return
        self.close_meta = meta
        self._closed.set()
        self._chunks.put_nowait(None)
        self._credit_event.set()
        self.mux._streams.pop(self.id, None)


OnOpen = Callable[[Stream], Awaitable[None]]


class Mux:
    """Multiplexes :class:`Stream` objects over one reader/writer pair."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        is_host: bool,
        on_open: OnOpen | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._on_open = on_open
        self._next_id = 1 if is_host else 2
        self._streams: dict[int, Stream] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()
        self.hello: dict[str, Any] | None = None
        self._hello_event = asyncio.Event()

    async def open_stream(self, meta: dict[str, Any]) -> Stream:
        stream_id = self._next_id
        self._next_id += 2
        stream = Stream(self, stream_id, meta)
        self._streams[stream_id] = stream
        await self._send(stream_id, OPEN, json.dumps(meta).encode())
        return stream

    async def send_hello(self, info: dict[str, Any]) -> None:
        await self._send(0, HELLO, json.dumps(info).encode())

    async def wait_hello(self) -> dict[str, Any]:
        waiter = asyncio.ensure_future(self._hello_event.wait())
        closer = asyncio.ensure_future(self.closed.wait())
        await asyncio.wait({waiter, closer}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        closer.cancel()
        if self.hello is None:
            raise MuxClosed("connection closed before HELLO")
        return self.hello

    async def _send(self, stream_id: int, ftype: int, payload: bytes) -> None:
        if self.closed.is_set():
            raise MuxClosed("mux closed")
        async with self._write_lock:
            try:
                self._writer.write(_HEADER.pack(stream_id, ftype, len(payload)) + payload)
                await self._writer.drain()
            except (ConnectionError, OSError) as e:
                self._shutdown()
                raise MuxClosed(str(e)) from e

    async def run(self) -> None:
        """Read frames until the connection closes."""
        try:
            while True:
                header = await self._reader.readexactly(_HEADER.size)
                stream_id, ftype, length = _HEADER.unpack(header)
                payload = await self._reader.readexactly(length) if length else b""
                self._dispatch(stream_id, ftype, payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._shutdown()

    def _dispatch(self, stream_id: int, ftype: int, payload: bytes) -> None:
        if ftype == HELLO:
            self.hello = json.loads(payload) if payload else {}
            self._hello_event.set()
            return
        if ftype == OPEN:
            stream = Stream(self, stream_id, json.loads(payload) if payload else {})
            self._streams[stream_id] = stream
            if self._on_open is None:
                self._spawn(stream.close({"error": "peer does not accept streams"}))
            else:
                self._spawn(self._run_handler(stream))
            return
        stream = self._streams.get(stream_id)
        if stream is None:
            return  # late frame for a stream we already closed
        if ftype == DATA:
            stream._feed(payload)
        elif ftype == EOF:
            stream._feed_eof()
        elif ftype == WINDOW:
            stream._grant(struct.unpack(">I", payload)[0])
        elif ftype == CLOSE:
            stream._finish(json.loads(payload) if payload else None)

    async def _run_handler(self, stream: Stream) -> None:
        try:
            await self._on_open(stream)
        except MuxClosed:
            pass
        except Exception as e:  # report handler failures to the peer
            await stream.close({"error": f"{type(e).__name__}: {e}"})

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _shutdown(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        for stream in list(self._streams.values()):
            stream._finish({"error": "connection closed"})
        try:
            self._writer.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        self._shutdown()
        for task in list(self._tasks):
            task.cancel()


async def pipe_stream_to_writer(stream: Stream, writer: asyncio.StreamWriter) -> None:
    """Copy a mux stream into a socket until EOF, then half-close the socket."""
    try:
        while True:
            chunk = await stream.read(MAX_FRAME)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def pipe_reader_to_stream(reader: asyncio.StreamReader, stream: Stream) -> None:
    """Copy a socket into a mux stream until EOF, then send EOF on the stream."""
    try:
        while True:
            chunk = await reader.read(MAX_FRAME)
            if not chunk:
                break
            await stream.write(chunk)
        await stream.write_eof()
    except (ConnectionError, OSError, MuxClosed):
        pass


async def splice(stream: Stream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Bidirectionally connect a mux stream and a socket, then close both."""
    try:
        await asyncio.gather(
            pipe_reader_to_stream(reader, stream),
            pipe_stream_to_writer(stream, writer),
        )
    finally:
        try:
            writer.close()
        except Exception:
            pass
        await stream.close()
