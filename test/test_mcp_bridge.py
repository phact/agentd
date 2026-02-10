"""
Tests for the MCP Bridge HTTP server.
"""
import asyncio
import json
import pytest
import socket
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

from agentd.mcp_bridge import MCPBridge


class TestMCPBridgeBasics:
    """Test basic bridge functionality."""

    def test_bridge_starts_in_thread(self):
        """Test bridge starts in background thread and returns port."""
        bridge = MCPBridge(port=0)
        port = bridge.start_in_thread()

        assert port > 0, "Should return a valid port"
        assert bridge.port == port, "Bridge should store the port"

        # Health check should work
        resp = urllib.request.urlopen(f"http://localhost:{port}/health")
        data = json.loads(resp.read())
        assert data["status"] == "ok"

        bridge.stop_thread()

    def test_bridge_lists_tools(self):
        """Test /tools endpoint lists registered tools."""
        bridge = MCPBridge(port=0)
        port = bridge.start_in_thread()

        # Initially empty
        resp = urllib.request.urlopen(f"http://localhost:{port}/tools")
        data = json.loads(resp.read())
        assert data["tools"] == []

        bridge.stop_thread()

    def test_bridge_404_unknown_tool(self):
        """Test calling unknown tool returns 404."""
        bridge = MCPBridge(port=0)
        port = bridge.start_in_thread()

        req = urllib.request.Request(
            f"http://localhost:{port}/call/nonexistent",
            data=b"{}",
            headers={"Content-Type": "application/json"}
        )

        try:
            urllib.request.urlopen(req)
            assert False, "Should have raised HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 404
            data = json.loads(e.read())
            assert "not found" in data["error"].lower()

        bridge.stop_thread()


class TestMCPBridgeLocalTools:
    """Test local tool registration and calling."""

    def test_register_and_call_local_tool(self):
        """Test registering and calling a local Python function."""
        bridge = MCPBridge(port=0)

        def add(a: int, b: int) -> int:
            return a + b

        bridge.register_local_tool("add", add)
        port = bridge.start_in_thread()

        # Check tool is listed
        resp = urllib.request.urlopen(f"http://localhost:{port}/tools")
        data = json.loads(resp.read())
        assert "add" in data["tools"]

        # Call the tool
        req = urllib.request.Request(
            f"http://localhost:{port}/call/add",
            data=json.dumps({"a": 2, "b": 3}).encode(),
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        assert data == 5

        bridge.stop_thread()

    def test_local_tool_with_string_args(self):
        """Test local tool with string arguments."""
        bridge = MCPBridge(port=0)

        def greet(name: str) -> str:
            return f"Hello, {name}!"

        bridge.register_local_tool("greet", greet)
        port = bridge.start_in_thread()

        req = urllib.request.Request(
            f"http://localhost:{port}/call/greet",
            data=json.dumps({"name": "World"}).encode(),
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        assert data == "Hello, World!"

        bridge.stop_thread()

    def test_local_tool_error_handling(self):
        """Test local tool that raises an exception."""
        bridge = MCPBridge(port=0)

        def fail():
            raise ValueError("intentional error")

        bridge.register_local_tool("fail", fail)
        port = bridge.start_in_thread()

        req = urllib.request.Request(
            f"http://localhost:{port}/call/fail",
            data=b"{}",
            headers={"Content-Type": "application/json"}
        )

        try:
            urllib.request.urlopen(req)
            assert False, "Should have raised HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            data = json.loads(e.read())
            assert "intentional error" in data["error"]

        bridge.stop_thread()

    def test_multiple_local_tools(self):
        """Test registering multiple local tools."""
        bridge = MCPBridge(port=0)

        bridge.register_local_tool("add", lambda a, b: a + b)
        bridge.register_local_tool("mul", lambda a, b: a * b)
        bridge.register_local_tool("upper", lambda s: s.upper())

        port = bridge.start_in_thread()

        # Check all tools listed
        resp = urllib.request.urlopen(f"http://localhost:{port}/tools")
        data = json.loads(resp.read())
        assert set(data["tools"]) == {"add", "mul", "upper"}

        # Call each
        for name, args, expected in [
            ("add", {"a": 1, "b": 2}, 3),
            ("mul", {"a": 3, "b": 4}, 12),
            ("upper", {"s": "hello"}, "HELLO"),
        ]:
            req = urllib.request.Request(
                f"http://localhost:{port}/call/{name}",
                data=json.dumps(args).encode(),
                headers={"Content-Type": "application/json"}
            )
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            assert data == expected, f"{name} should return {expected}"

        bridge.stop_thread()


class TestMCPBridgeAsync:
    """Test async bridge functionality."""

    def test_bridge_starts_async(self):
        """Test bridge starts in async context."""
        async def run():
            bridge = MCPBridge(port=0)
            port = await bridge.start_async()

            assert port > 0
            assert bridge.port == port

            # Verify server is listening by checking tools endpoint
            # Use aiohttp for async HTTP (but we don't have it in test deps)
            # Just verify the port was assigned
            assert bridge._site is not None

            await bridge.stop()

        asyncio.run(run())

    def test_async_local_tool_registration(self):
        """Test registering async local tool."""
        bridge = MCPBridge(port=0)

        async def async_add(a: int, b: int) -> int:
            await asyncio.sleep(0.01)
            return a + b

        bridge.register_local_tool("async_add", async_add)

        assert "async_add" in bridge.local_tools
        assert bridge.local_tools["async_add"] is async_add


class TestMCPBridgeFromGeneratedCode:
    """Test calling bridge the way generated tools.py does."""

    def test_call_pattern_matches_generated_code(self):
        """Test the _call pattern used in generated lib/tools.py."""
        bridge = MCPBridge(port=0)

        def read_file(path: str) -> str:
            return f"contents of {path}"

        bridge.register_local_tool("read_file", read_file)
        port = bridge.start_in_thread()

        # This mimics how generated tools.py calls the bridge
        def _call(name: str, **kwargs):
            filtered = {k: v for k, v in kwargs.items() if v is not None}
            req = urllib.request.Request(
                f"http://localhost:{port}/call/{name}",
                data=json.dumps(filtered).encode(),
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return {"error": f"HTTP {e.code}: {e.reason}"}

        result = _call("read_file", path="/etc/hosts")
        assert result == "contents of /etc/hosts"

        # Test with None values filtered
        result = _call("read_file", path="/tmp/test", optional_arg=None)
        assert result == "contents of /tmp/test"

        bridge.stop_thread()


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
            assert bridge.port == 0  # Port ignored in socket mode

    def test_socket_thread_mode_returns_path(self):
        """Test start_in_thread returns socket path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "thread.sock"
            bridge = MCPBridge(socket_path=socket_path)

            result = bridge.start_in_thread()
            assert result == str(socket_path), "Should return socket path string"

            bridge.stop_thread()
