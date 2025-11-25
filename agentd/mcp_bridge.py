# agentd/mcp_bridge.py
"""
HTTP Bridge for MCP tool calls.

Provides a local HTTP server that proxies tool calls to MCP servers,
allowing skill scripts to call MCP tools via simple HTTP requests.
"""

import asyncio
import json
import logging
import threading
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


class MCPBridge:
    """Local HTTP server that proxies MCP tool calls."""

    def __init__(self, port: int = 0):
        """
        Initialize the MCP bridge.

        Args:
            port: Port to listen on (0 = auto-assign)
        """
        self.port = port
        self.servers: dict[str, Any] = {}  # tool_name -> server connection
        self.local_tools: dict[str, callable] = {}  # tool_name -> function
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()

    async def start(self) -> int:
        """
        Start the bridge server.

        Returns:
            The port number the server is listening on.
        """
        app = web.Application()
        app.router.add_post('/call/{tool_name}', self.handle_call)
        app.router.add_get('/tools', self.handle_list_tools)
        app.router.add_get('/health', self.handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, 'localhost', self.port)
        await self._site.start()

        # Get the actual port if auto-assigned
        actual_port = self._site._server.sockets[0].getsockname()[1]
        self.port = actual_port

        logger.info(f"MCP Bridge started on http://localhost:{actual_port}")
        return actual_port

    async def stop(self):
        """Stop the bridge server."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("MCP Bridge stopped")

    def start_in_thread(self) -> int:
        """
        Start the bridge server in a background thread.

        This is useful when you need to make synchronous HTTP calls
        to the bridge from the main thread.

        Returns:
            The port number the server is listening on.
        """
        def run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def setup_and_run():
                port = await self.start()
                self._started.set()
                # Keep running until stopped
                while True:
                    await asyncio.sleep(1)

            self._loop.run_until_complete(setup_and_run())

        self._thread = threading.Thread(target=run_server, daemon=True)
        self._thread.start()

        # Wait for server to start
        self._started.wait(timeout=10)
        return self.port

    def stop_thread(self):
        """Stop the bridge server running in background thread."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def register_server(self, tool_name: str, server):
        """Register an MCP server for a tool."""
        self.servers[tool_name] = server
        logger.debug(f"Registered MCP server for tool: {tool_name}")

    def register_local_tool(self, tool_name: str, func: callable):
        """Register a local Python function as a tool."""
        self.local_tools[tool_name] = func
        logger.debug(f"Registered local tool: {tool_name}")

    async def handle_call(self, request: web.Request) -> web.Response:
        """Handle a tool call request."""
        tool_name = request.match_info['tool_name']

        try:
            args = await request.json()
        except json.JSONDecodeError:
            args = {}

        logger.info(f"Tool call: {tool_name}({args})")

        # Check MCP servers first
        if tool_name in self.servers:
            try:
                server = self.servers[tool_name]
                result = await server.call_tool(tool_name, args)
                content = result.dict().get('content', result.dict())
                return web.json_response(content)
            except Exception as e:
                logger.error(f"MCP tool call failed: {e}")
                return web.json_response(
                    {"error": str(e)},
                    status=500
                )

        # Check local tools
        if tool_name in self.local_tools:
            try:
                func = self.local_tools[tool_name]
                result = func(**args)
                if asyncio.iscoroutine(result):
                    result = await result
                return web.json_response({"result": result})
            except Exception as e:
                logger.error(f"Local tool call failed: {e}")
                return web.json_response(
                    {"error": str(e)},
                    status=500
                )

        # Tool not found
        return web.json_response(
            {"error": f"Tool '{tool_name}' not found"},
            status=404
        )

    async def handle_list_tools(self, request: web.Request) -> web.Response:
        """List all available tools."""
        tools = list(self.servers.keys()) + list(self.local_tools.keys())
        return web.json_response({"tools": tools})

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok"})


# Global bridge instance for convenience
_bridge: MCPBridge | None = None


async def start_bridge(port: int = 0) -> MCPBridge:
    """Start a global MCP bridge instance."""
    global _bridge
    if _bridge is None:
        _bridge = MCPBridge(port=port)
        await _bridge.start()
    return _bridge


async def stop_bridge():
    """Stop the global MCP bridge instance."""
    global _bridge
    if _bridge is not None:
        await _bridge.stop()
        _bridge = None


def get_bridge() -> MCPBridge | None:
    """Get the global MCP bridge instance."""
    return _bridge
