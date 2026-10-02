# agentd/mcp_bridge.py
"""
Unix-socket bridge for MCP tool calls.

A small HTTP server on a host Unix socket that proxies tool calls to MCP
servers (and local @tool functions), so skill scripts can call them. Sandboxed
code reaches it through its sandbox's ``bridge`` endpoint, which agentd tunnels
to this socket; nothing listens on the network.
"""

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from aiohttp import web
from agentd.secrets import scrub  # results to sandboxes never carry host secrets

logger = logging.getLogger(__name__)


class MCPBridge:
    """Unix-socket HTTP server that proxies MCP tool calls."""

    def __init__(
        self,
        socket_path: str | Path,
        main_loop: asyncio.AbstractEventLoop | None = None,
    ):
        """
        Initialize the MCP bridge.

        Args:
            socket_path: Path of the Unix socket to listen on.
            main_loop: The event loop where MCP connections were established.
                       Tool calls will be dispatched to this loop.
        """
        self.socket_path = Path(socket_path)
        self.servers: dict[str, Any] = {}  # tool_name -> server connection
        self.local_tools: dict[str, callable] = {}  # tool_name -> function
        self._runner: web.AppRunner | None = None
        self._site: web.UnixSite | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # Bridge's own loop
        self._stop_event: asyncio.Event | None = None  # Signals run_server to exit cleanly
        self._main_loop: asyncio.AbstractEventLoop | None = main_loop  # MCP connection loop
        self._started = threading.Event()

    async def start(self) -> str:
        """Start the bridge server; returns the socket path."""
        app = web.Application()
        app.router.add_post('/call/{tool_name}', self.handle_call)
        app.router.add_get('/tools', self.handle_list_tools)
        app.router.add_get('/health', self.handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        if self.socket_path.exists():
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._site = web.UnixSite(self._runner, str(self.socket_path))
        await self._site.start()
        # Only agentd itself connects (sandboxes reach it via their tunnel).
        os.chmod(self.socket_path, 0o600)

        logger.info(f"MCP Bridge started on unix://{self.socket_path}")
        return str(self.socket_path)

    async def stop(self):
        """Stop the bridge server."""
        if self._runner:
            await self._runner.cleanup()
            if self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass
            logger.info("MCP Bridge stopped")

    def start_in_thread(self) -> str:
        """
        Start the bridge server in a background thread.

        This is useful when you need to make synchronous HTTP calls
        to the bridge from the main thread.

        Returns:
            The socket path.
        """
        def run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def setup_and_run():
                self._stop_event = asyncio.Event()
                await self.start()
                self._started.set()
                # Keep running until stop_thread() sets the event
                await self._stop_event.wait()

            self._loop.run_until_complete(setup_and_run())

        self._thread = threading.Thread(target=run_server, daemon=True)
        self._thread.start()

        # Wait for server to start
        self._started.wait(timeout=10)
        return str(self.socket_path)

    async def start_async(self) -> str:
        """
        Start the bridge server in the current async context.

        This allows the bridge to handle requests while other async
        operations (like subprocess execution) are awaited.

        Returns:
            The socket path.
        """
        result = await self.start()
        self._started.set()
        return result

    def stop_thread(self):
        """Stop the bridge server running in background thread."""
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

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

                # If running in a separate thread with main_loop reference,
                # dispatch the call there (MCP connections must be used from the loop that created them)
                # If running in the main async context (no thread), just await directly
                if self._main_loop is not None and self._thread is not None:
                    future = asyncio.run_coroutine_threadsafe(
                        server.call_tool(tool_name, args),
                        self._main_loop
                    )
                    result = future.result(timeout=60)  # Wait up to 60 seconds
                else:
                    # Running in same async context - await directly
                    result = await server.call_tool(tool_name, args)

                # Unwrap MCP content blocks → return raw text/value
                content = result.dict().get('content', [])
                texts = [c.get('text', '') for c in content
                         if isinstance(c, dict) and c.get('type') == 'text']
                if texts:
                    text = '\n'.join(texts) if len(texts) > 1 else texts[0]
                    try:
                        return web.json_response(scrub(json.loads(text)))
                    except (json.JSONDecodeError, TypeError):
                        return web.json_response(scrub(text))
                return web.json_response(scrub(content))
            except Exception as e:
                logger.error(f"MCP tool call failed: {e}")
                return web.json_response(
                    {"error": scrub(str(e))},
                    status=500
                )

        # Check local tools
        if tool_name in self.local_tools:
            try:
                func = self.local_tools[tool_name]
                result = func(**args)
                if asyncio.iscoroutine(result):
                    result = await result
                return web.json_response(scrub(result))
            except Exception as e:
                logger.error(f"Local tool call failed: {e}")
                return web.json_response(
                    {"error": scrub(str(e))},
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


async def start_bridge(socket_path: str | Path) -> MCPBridge:
    """Start a global MCP bridge instance."""
    global _bridge
    if _bridge is None:
        _bridge = MCPBridge(socket_path)
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
