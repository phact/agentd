"""
Tests for the MCP Bridge (HTTP over a Unix socket).
"""
import asyncio
import http.client
import json
import os
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path

from agentd.mcp_bridge import MCPBridge


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def _http(sock_path, method: str, path: str, body=None):
    """(status, json body) for one request to the bridge socket."""
    conn = _UnixHTTPConnection(str(sock_path))
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


@contextmanager
def running_bridge(**tools):
    with tempfile.TemporaryDirectory() as tmp:
        bridge = MCPBridge(Path(tmp) / "bridge.sock")
        for name, fn in tools.items():
            bridge.register_local_tool(name, fn)
        bridge.start_in_thread()
        try:
            yield bridge
        finally:
            bridge.stop_thread()


class TestMCPBridgeBasics:
    """Test basic bridge functionality."""

    def test_bridge_starts_in_thread(self):
        with running_bridge() as bridge:
            assert bridge.socket_path.exists()
            assert oct(bridge.socket_path.stat().st_mode & 0o777) == "0o600", "only agentd itself connects"
            assert _http(bridge.socket_path, "GET", "/health") == (200, {"status": "ok"})

    def test_bridge_lists_tools(self):
        with running_bridge() as bridge:
            assert _http(bridge.socket_path, "GET", "/tools") == (200, {"tools": []})

    def test_bridge_404_unknown_tool(self):
        with running_bridge() as bridge:
            status, data = _http(bridge.socket_path, "POST", "/call/nonexistent", {})
            assert status == 404 and "not found" in data["error"].lower()


class TestMCPBridgeLocalTools:
    """Test local tool registration and calling."""

    def test_register_and_call_local_tool(self):
        def add(a: int, b: int) -> int:
            return a + b

        with running_bridge(add=add) as bridge:
            assert "add" in _http(bridge.socket_path, "GET", "/tools")[1]["tools"]
            assert _http(bridge.socket_path, "POST", "/call/add", {"a": 2, "b": 3}) == (200, 5)

    def test_local_tool_with_string_args(self):
        with running_bridge(greet=lambda name: f"Hello, {name}!") as bridge:
            assert _http(bridge.socket_path, "POST", "/call/greet", {"name": "World"}) == (200, "Hello, World!")

    def test_local_tool_error_handling(self):
        def fail():
            raise ValueError("intentional error")

        with running_bridge(fail=fail) as bridge:
            status, data = _http(bridge.socket_path, "POST", "/call/fail", {})
            assert status == 500 and "intentional error" in data["error"]

    def test_multiple_local_tools(self):
        tools = {"add": lambda a, b: a + b, "mul": lambda a, b: a * b, "upper": lambda s: s.upper()}
        with running_bridge(**tools) as bridge:
            assert set(_http(bridge.socket_path, "GET", "/tools")[1]["tools"]) == {"add", "mul", "upper"}
            for name, args, expected in [
                ("add", {"a": 1, "b": 2}, 3),
                ("mul", {"a": 3, "b": 4}, 12),
                ("upper", {"s": "hello"}, "HELLO"),
            ]:
                assert _http(bridge.socket_path, "POST", f"/call/{name}", args) == (200, expected)


class TestMCPBridgeAsync:
    """Test async bridge functionality."""

    def test_bridge_starts_async(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                bridge = MCPBridge(Path(tmp) / "a.sock")
                assert await bridge.start_async() == str(bridge.socket_path)
                assert bridge._site is not None
                await bridge.stop()
                assert not bridge.socket_path.exists()

        asyncio.run(run())

    def test_async_local_tool_registration(self):
        async def async_add(a: int, b: int) -> int:
            await asyncio.sleep(0.01)
            return a + b

        with running_bridge(async_add=async_add) as bridge:
            assert bridge.local_tools["async_add"] is async_add
            assert _http(bridge.socket_path, "POST", "/call/async_add", {"a": 4, "b": 5}) == (200, 9)


class TestMCPBridgeFromGeneratedCode:
    """Call the bridge through the real generated lib/tools.py."""

    def test_generated_tools_module_calls_bridge_over_socket(self, monkeypatch):
        from agentd.ptc import generate_tools_module

        schema = {"description": "Read a file", "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "optional_arg": {"type": "string"}}, "required": ["path"]}}
        with running_bridge(read_file=lambda path: f"contents of {path}") as bridge:
            namespace: dict = {}
            monkeypatch.setenv("MCP_BRIDGE_SOCKET", str(bridge.socket_path))
            exec(generate_tools_module({"read_file": schema}), namespace)
            assert namespace["read_file"](path="/etc/hosts") == "contents of /etc/hosts"
            # None values are filtered out before calling.
            assert namespace["_call"]("read_file", path="/tmp/test", optional_arg=None) == "contents of /tmp/test"

    def test_generated_tools_module_without_bridge_reports_error(self, monkeypatch):
        from agentd.ptc import generate_tools_module

        monkeypatch.delenv("MCP_BRIDGE_SOCKET", raising=False)
        namespace: dict = {}
        exec(generate_tools_module({}), namespace)
        assert "not available" in namespace["_call"]("anything")["error"]


class TestMCPBridgeUnixSocket:
    """Test Unix socket mode of the bridge."""

    def test_bridge_starts_with_socket(self):
        """Test bridge starts with Unix socket."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "test.sock"
            bridge = MCPBridge(socket_path=socket_path)

            async def run():
                result = await bridge.start_async()
                assert result == str(socket_path), "Should return socket path"
                assert socket_path.exists(), "Socket file should exist"
                await bridge.stop()
                assert not socket_path.exists(), "Socket should be cleaned up"

            asyncio.run(run())

    def test_socket_health_check(self):
        """Test health endpoint via Unix socket."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "health.sock"
            bridge = MCPBridge(socket_path=socket_path)
            bridge.start_in_thread()

            # Connect via Unix socket and send HTTP request
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(socket_path))

            request = b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            sock.sendall(request)

            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                except socket.timeout:
                    break
            sock.close()

            # Parse response
            assert b"200 OK" in response, "Should return 200"
            body = response.split(b"\r\n\r\n", 1)[1]
            data = json.loads(body)
            assert data["status"] == "ok"

            bridge.stop_thread()

    def test_socket_tool_call(self):
        """Test calling a tool via Unix socket."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "tools.sock"
            bridge = MCPBridge(socket_path=socket_path)
            bridge.register_local_tool("multiply", lambda a, b: a * b)
            bridge.start_in_thread()

            # Call tool via socket
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(socket_path))

            data = json.dumps({"a": 7, "b": 6}).encode()
            request = (
                f"POST /call/multiply HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode() + data
            sock.sendall(request)

            response = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                except socket.timeout:
                    break
            sock.close()

            body = response.split(b"\r\n\r\n", 1)[1]
            result = json.loads(body)
            assert result == 42, "7 * 6 should be 42"

            bridge.stop_thread()

    def test_socket_replaces_existing(self):
        """Test that starting bridge removes existing socket file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "replace.sock"

            # Create a dummy file
            socket_path.write_text("dummy")
            assert socket_path.exists()

            bridge = MCPBridge(socket_path=socket_path)

            async def run():
                await bridge.start_async()
                # Should have replaced the file with actual socket
                assert socket_path.exists()
                # Verify it's a socket by connecting
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(socket_path))
                sock.close()
                await bridge.stop()

            asyncio.run(run())

    def test_socket_path_property(self):
        """Test socket_path is stored correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "prop.sock"
            bridge = MCPBridge(socket_path=socket_path)

            assert bridge.socket_path == socket_path

    def test_socket_thread_mode_returns_path(self):
        """Test start_in_thread returns socket path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "thread.sock"
            bridge = MCPBridge(socket_path=socket_path)

            result = bridge.start_in_thread()
            assert result == str(socket_path), "Should return socket path string"

            bridge.stop_thread()
