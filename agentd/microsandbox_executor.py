# agentd/microsandbox_executor.py
"""
Microsandbox-based executor for secure code execution in microVMs.

Uses the microsandbox JSON-RPC API directly (bypassing SDK limitations)
to get volume mounting support for persistence and snapshots.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SandboxConfig:
    """Configuration for a microsandbox instance."""
    image: str = "microsandbox/python"
    memory: int = 1024
    cpus: int = 1
    workdir: str = "/workspace"


@dataclass
class Snapshot:
    """Represents a point-in-time snapshot of sandbox state."""
    id: str
    turn: int
    path: Path
    timestamp: float = field(default_factory=lambda: __import__('time').time())


class MicrosandboxClient:
    """Low-level JSON-RPC client for microsandbox server."""

    def __init__(self, server_url: str = "http://127.0.0.1:5555", api_key: str | None = None):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key or os.environ.get("MSB_API_KEY")
        self._request_id = 0

    def _next_id(self) -> str:
        self._request_id += 1
        return str(self._request_id)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make a JSON-RPC call to the microsandbox server."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params
        }

        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{self.server_url}/api/v1/rpc",
                headers=self._headers(),
                json=payload
            )
            resp.raise_for_status()
            result = resp.json()

            if "error" in result:
                raise RuntimeError(f"RPC error: {result['error']}")

            return result.get("result", {})

    async def start_sandbox(
        self,
        name: str,
        namespace: str = "default",
        config: SandboxConfig | None = None,
        volumes: list[str] | None = None,
        envs: list[str] | None = None
    ) -> dict[str, Any]:
        """Start a new sandbox with optional volume mounts."""
        config = config or SandboxConfig()

        config_dict = {
            "image": config.image,
            "memory": config.memory,
            "cpus": config.cpus,
            "workdir": config.workdir,
        }

        if volumes:
            config_dict["volumes"] = volumes
        if envs:
            config_dict["envs"] = envs

        return await self._rpc("sandbox.start", {
            "sandbox": name,
            "namespace": namespace,
            "config": config_dict
        })

    async def stop_sandbox(self, name: str, namespace: str = "default") -> dict[str, Any]:
        """Stop a running sandbox."""
        return await self._rpc("sandbox.stop", {
            "sandbox": name,
            "namespace": namespace
        })

    async def exec_command(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        namespace: str = "default",
        timeout: int = 60
    ) -> dict[str, Any]:
        """Execute a shell command in a running sandbox."""
        params = {
            "sandbox": name,
            "namespace": namespace,
            "command": command,
            "timeout": timeout
        }
        if args:
            params["args"] = args
        return await self._rpc("sandbox.command.run", params)

    async def run_code(
        self,
        name: str,
        code: str,
        language: str = "python",
        namespace: str = "default",
        timeout: int = 60
    ) -> dict[str, Any]:
        """Run code in a sandbox REPL."""
        return await self._rpc("sandbox.repl.run", {
            "sandbox": name,
            "namespace": namespace,
            "code": code,
            "language": language,
            "timeout": timeout
        })


class SnapshotManager:
    """Manages filesystem snapshots for time travel."""

    def __init__(self, base_dir: Path, workspace_dir: Path | None = None):
        self.base_dir = base_dir
        self._workspace_dir = workspace_dir  # Custom workspace path
        self.snapshots_dir = base_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots: list[Snapshot] = []
        self._current_turn = 0

    @property
    def workspace_dir(self) -> Path:
        """The live workspace directory mounted into sandbox."""
        if self._workspace_dir:
            return self._workspace_dir
        return self.base_dir / "workspace"

    def create_snapshot(self, label: str | None = None) -> Snapshot:
        """Create a snapshot of current workspace state."""
        self._current_turn += 1
        snapshot_id = f"turn_{self._current_turn:04d}"
        if label:
            snapshot_id += f"_{label}"

        snapshot_path = self.snapshots_dir / snapshot_id

        # Copy workspace to snapshot
        if self.workspace_dir.exists():
            shutil.copytree(self.workspace_dir, snapshot_path, dirs_exist_ok=True)
        else:
            snapshot_path.mkdir(parents=True)

        snapshot = Snapshot(
            id=snapshot_id,
            turn=self._current_turn,
            path=snapshot_path
        )
        self._snapshots.append(snapshot)

        logger.info(f"Created snapshot: {snapshot_id}")
        return snapshot

    def restore_snapshot(self, snapshot_id: str) -> bool:
        """Restore workspace to a previous snapshot."""
        snapshot = next((s for s in self._snapshots if s.id == snapshot_id), None)
        if not snapshot:
            # Try to find by turn number
            try:
                turn = int(snapshot_id)
                snapshot = next((s for s in self._snapshots if s.turn == turn), None)
            except ValueError:
                pass

        if not snapshot or not snapshot.path.exists():
            logger.error(f"Snapshot not found: {snapshot_id}")
            return False

        # Clear current workspace and restore from snapshot
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir)

        shutil.copytree(snapshot.path, self.workspace_dir)
        logger.info(f"Restored to snapshot: {snapshot.id}")
        return True

    def list_snapshots(self) -> list[Snapshot]:
        """List all available snapshots."""
        return list(self._snapshots)

    def get_latest_snapshot(self) -> Snapshot | None:
        """Get the most recent snapshot."""
        return self._snapshots[-1] if self._snapshots else None


class MicrosandboxExecutor:
    """
    Executor that runs code in microsandbox microVMs.

    Implements the Executor protocol from ptc.py for drop-in replacement
    of SubprocessExecutor.

    Features:
    - Hardware-isolated execution via microVM
    - Persistent workspace via volume mounting
    - Snapshot-based time travel
    - Conversation-scoped sandbox lifecycle
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:5555",
        api_key: str | None = None,
        config: SandboxConfig | None = None,
        timeout: int = 60,
        conversation_id: str | None = None,
        base_dir: Path | None = None,
        workspace_dir: Path | None = None,
        additional_volumes: list[tuple[str, str]] | None = None,
        envs: dict[str, str] | None = None,
        auto_snapshot: bool = True
    ):
        self.client = MicrosandboxClient(server_url, api_key)
        self.config = config or SandboxConfig()
        self.timeout = timeout
        self.auto_snapshot = auto_snapshot
        self.additional_volumes = additional_volumes or []
        self.envs = envs or {}

        # Conversation-scoped identity
        self.conversation_id = conversation_id or uuid.uuid4().hex[:12]
        self.sandbox_name = f"agentd_{self.conversation_id}"
        self.namespace = "agentd"

        # Setup workspace and snapshots
        self.base_dir = base_dir or Path(tempfile.mkdtemp(prefix="agentd_msb_"))
        self.snapshot_manager = SnapshotManager(self.base_dir, workspace_dir=workspace_dir)
        self.snapshot_manager.workspace_dir.mkdir(parents=True, exist_ok=True)

        # State
        self._started = False
        self._execution_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop for sync methods."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
            return self._loop

    def _run_async(self, coro):
        """Run async code from sync context."""
        try:
            loop = asyncio.get_running_loop()
            # Already in async context - can't use run_until_complete
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        except RuntimeError:
            # No running loop - safe to use run
            return asyncio.run(coro)

    async def start(self) -> None:
        """Start the sandbox for this conversation."""
        if self._started:
            return

        workspace = str(self.snapshot_manager.workspace_dir.absolute())
        volumes = [f"{workspace}:{self.config.workdir}"]

        # Add additional volumes
        for host_path, container_path in self.additional_volumes:
            volumes.append(f"{host_path}:{container_path}")

        # Build env list from self.envs
        envs = [f"{k}={v}" for k, v in self.envs.items()]

        logger.info(f"Starting sandbox {self.sandbox_name} with workspace {workspace}")

        await self.client.start_sandbox(
            name=self.sandbox_name,
            namespace=self.namespace,
            config=self.config,
            volumes=volumes,
            envs=envs
        )

        self._started = True
        logger.info(f"Sandbox {self.sandbox_name} started")

    async def stop(self) -> None:
        """Stop the sandbox."""
        if not self._started:
            return

        try:
            await self.client.stop_sandbox(self.sandbox_name, self.namespace)
            logger.info(f"Sandbox {self.sandbox_name} stopped")
        except Exception as e:
            logger.warning(f"Error stopping sandbox: {e}")
        finally:
            self._started = False

    async def execute_bash_async(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command in sandbox."""
        await self.start()

        try:
            result = await self.client.exec_command(
                name=self.sandbox_name,
                command="bash",
                args=["-c", command],
                namespace=self.namespace,
                timeout=self.timeout
            )

            output = result.get("stdout", "") or ""
            stderr = result.get("stderr", "")
            if stderr:
                output = f"{output}\n{stderr}" if output else stderr

            exit_code = result.get("exit_code", 0)

            self._execution_count += 1

            return output.strip(), exit_code

        except Exception as e:
            logger.error(f"Bash execution failed: {e}")
            return f"Error: {e}", 1

    async def execute_python_async(
        self,
        code: str,
        cwd: Path,
        pythonpath: Path | None = None
    ) -> tuple[str, int]:
        """Run Python code in sandbox."""
        await self.start()

        try:
            result = await self.client.run_code(
                name=self.sandbox_name,
                code=code,
                language="python",
                namespace=self.namespace,
                timeout=self.timeout
            )

            output = result.get("stdout", "") or ""
            stderr = result.get("stderr", "")
            if stderr:
                output = f"{output}\n{stderr}" if output else stderr

            exit_code = result.get("exit_code", 0)
            success = result.get("success", exit_code == 0)

            self._execution_count += 1

            return output.strip(), 0 if success else 1

        except Exception as e:
            logger.error(f"Python execution failed: {e}")
            return f"Error: {e}", 1

    def execute_bash(self, command: str, cwd: Path) -> tuple[str, int]:
        """Sync wrapper for bash execution."""
        return self._run_async(self.execute_bash_async(command, cwd))

    def execute_python(
        self,
        code: str,
        cwd: Path,
        pythonpath: Path | None = None
    ) -> tuple[str, int]:
        """Sync wrapper for Python execution."""
        return self._run_async(self.execute_python_async(code, cwd, pythonpath))

    def create_file(self, filename: str, content: str, cwd: Path) -> str:
        """Create a file in the workspace."""
        try:
            # Write to the mounted workspace directory
            filepath = self.snapshot_manager.workspace_dir / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)
            return f"Created file: {filename}"
        except Exception as e:
            return f"Error creating file {filename}: {e}"

    # =========================================================================
    # Snapshot / Time Travel API
    # =========================================================================

    def snapshot(self, label: str | None = None) -> Snapshot:
        """Create a snapshot of current state."""
        return self.snapshot_manager.create_snapshot(label)

    def restore(self, snapshot_id: str) -> bool:
        """Restore to a previous snapshot (time travel)."""
        return self.snapshot_manager.restore_snapshot(snapshot_id)

    def list_snapshots(self) -> list[Snapshot]:
        """List all snapshots."""
        return self.snapshot_manager.list_snapshots()

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def close(self) -> None:
        """Clean up sandbox."""
        if self._started:
            self._run_async(self.stop())

        if self._loop and not self._loop.is_closed():
            self._loop.close()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    def __enter__(self):
        self._run_async(self.start())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# =============================================================================
# Factory / Convenience
# =============================================================================

def create_microsandbox_executor(
    conversation_id: str | None = None,
    base_dir: Path | None = None,
    **kwargs
) -> MicrosandboxExecutor:
    """
    Create a MicrosandboxExecutor for a conversation.

    Usage with PTC:
        from agentd.microsandbox_executor import create_microsandbox_executor

        executor = create_microsandbox_executor(conversation_id="conv_123")
        patch_openai_with_ptc(client, executor=executor)

        # After conversation
        executor.snapshot("final")
        executor.close()
    """
    return MicrosandboxExecutor(
        conversation_id=conversation_id,
        base_dir=base_dir,
        **kwargs
    )
