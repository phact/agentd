# agentd/microsandbox_cli_executor.py
"""
Microsandbox CLI-based executor for secure code execution in microVMs.

Uses `msb exe` subprocess calls instead of the server API (which has known
portal connection issues - see https://github.com/microsandbox/microsandbox/issues/314).
"""

import asyncio
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Represents a point-in-time snapshot of sandbox state."""
    id: str
    turn: int
    path: Path
    timestamp: float = field(default_factory=lambda: __import__('time').time())


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
            try:
                turn = int(snapshot_id)
                snapshot = next((s for s in self._snapshots if s.turn == turn), None)
            except ValueError:
                pass

        if not snapshot or not snapshot.path.exists():
            logger.error(f"Snapshot not found: {snapshot_id}")
            return False

        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir)

        shutil.copytree(snapshot.path, self.workspace_dir)
        logger.info(f"Restored to snapshot: {snapshot.id}")
        return True

    def list_snapshots(self) -> list[Snapshot]:
        """List all available snapshots."""
        return list(self._snapshots)


class MicrosandboxCLIExecutor:
    """
    Executor that runs code in microsandbox microVMs via CLI.

    Uses `msb exe` subprocess calls which work reliably (unlike the server API).

    Implements the Executor protocol from ptc.py for drop-in replacement
    of SubprocessExecutor.
    """

    def __init__(
        self,
        image: str = "python",
        memory: int = 1024,
        cpus: int = 1,
        timeout: int = 60,
        conversation_id: str | None = None,
        base_dir: Path | None = None,
        workspace_dir: Path | None = None,
        additional_volumes: list[tuple[str, str]] | None = None,
        envs: dict[str, str] | None = None,
        msb_path: str | None = None,
        network_scope: str = "any"
    ):
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.timeout = timeout
        self.network_scope = network_scope  # any, public, none
        self.additional_volumes = additional_volumes or []
        self.envs = envs or {}

        # Find msb binary
        self.msb_path = msb_path or self._find_msb()

        # Conversation-scoped identity
        self.conversation_id = conversation_id or uuid.uuid4().hex[:12]

        # Setup workspace and snapshots
        if base_dir:
            self.base_dir = base_dir
        else:
            import tempfile
            self.base_dir = Path(tempfile.mkdtemp(prefix="agentd_msb_"))

        self.snapshot_manager = SnapshotManager(self.base_dir, workspace_dir=workspace_dir)
        self.snapshot_manager.workspace_dir.mkdir(parents=True, exist_ok=True)

        self._execution_count = 0

    def _find_msb(self) -> str:
        """Find the msb binary."""
        # Check common locations
        locations = [
            os.path.expanduser("~/.local/bin/msb"),
            "/usr/local/bin/msb",
            "/usr/bin/msb",
        ]

        for loc in locations:
            if os.path.isfile(loc) and os.access(loc, os.X_OK):
                return loc

        # Try PATH
        result = subprocess.run(
            ["which", "msb"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()

        raise RuntimeError(
            "msb not found. Install with: curl -sSL https://get.microsandbox.dev | sh"
        )

    def _build_command(
        self,
        exec_cmd: str | None = None,
        args: list[str] | None = None,
        volumes: list[tuple[str, str]] | None = None,
        envs: list[str] | None = None,
        workdir: str | None = None
    ) -> list[str]:
        """Build the msb exe command."""
        cmd = [
            self.msb_path, "exe", self.image,
            "--memory", str(self.memory),
            "--cpus", str(self.cpus),
            "--scope", self.network_scope,
        ]

        # Add volumes
        if volumes:
            for host_path, container_path in volumes:
                cmd.extend(["-v", f"{host_path}:{container_path}"])

        # Add environment variables
        if envs:
            for env in envs:
                cmd.extend(["--env", env])

        # Add workdir
        if workdir:
            cmd.extend(["--workdir", workdir])

        # Add exec command
        if exec_cmd:
            cmd.extend(["-e", exec_cmd])

        # Add separator and args
        if args:
            cmd.append("--")
            cmd.extend(args)

        return cmd

    def _run_command(self, cmd: list[str]) -> tuple[str, int]:
        """Run a command and return (output, exit_code)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )

            output = result.stdout
            if result.stderr:
                # Filter out debug/info logs from stderr
                stderr_lines = []
                for line in result.stderr.split('\n'):
                    # Skip microsandbox log lines
                    if not any(x in line for x in ['INFO', 'DEBUG', 'WARN', 'microsandbox']):
                        stderr_lines.append(line)
                stderr = '\n'.join(stderr_lines).strip()
                if stderr:
                    output = f"{output}\n{stderr}" if output else stderr

            return output.strip(), result.returncode

        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.timeout}s", 1
        except Exception as e:
            return f"Error: {e}", 1

    async def _run_command_async(self, cmd: list[str]) -> tuple[str, int]:
        """Run a command asynchronously."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Command timed out after {self.timeout}s", 1

            output = stdout.decode() if stdout else ""
            if stderr:
                stderr_text = stderr.decode()
                # Filter out debug/info logs
                stderr_lines = []
                for line in stderr_text.split('\n'):
                    if not any(x in line for x in ['INFO', 'DEBUG', 'WARN', 'microsandbox']):
                        stderr_lines.append(line)
                stderr_filtered = '\n'.join(stderr_lines).strip()
                if stderr_filtered:
                    output = f"{output}\n{stderr_filtered}" if output else stderr_filtered

            return output.strip(), proc.returncode or 0

        except Exception as e:
            return f"Error: {e}", 1

    def _get_volumes(self) -> list[tuple[str, str]]:
        """Get volume mappings for the sandbox."""
        workspace = str(self.snapshot_manager.workspace_dir.absolute())
        volumes = [(workspace, "/workspace")]
        volumes.extend(self.additional_volumes)
        return volumes

    def _get_envs(self) -> list[str]:
        """Get environment variables for the sandbox."""
        return [f"{k}={v}" for k, v in self.envs.items()]

    def execute_bash(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command in sandbox."""
        cmd = self._build_command(
            exec_cmd="bash",
            args=["-c", command],
            volumes=self._get_volumes(),
            envs=self._get_envs(),
            workdir="/workspace"
        )

        output, exit_code = self._run_command(cmd)
        self._execution_count += 1
        return output, exit_code

    async def execute_bash_async(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command in sandbox asynchronously."""
        cmd = self._build_command(
            exec_cmd="bash",
            args=["-c", command],
            volumes=self._get_volumes(),
            envs=self._get_envs(),
            workdir="/workspace"
        )

        output, exit_code = await self._run_command_async(cmd)
        self._execution_count += 1
        return output, exit_code

    def execute_python(
        self,
        code: str,
        cwd: Path,
        pythonpath: Path | None = None
    ) -> tuple[str, int]:
        """Run Python code in sandbox."""
        # Write code to temp file to avoid CLI arg issues with newlines
        temp_name = f"_temp_{uuid.uuid4().hex[:8]}.py"
        temp_path = self.snapshot_manager.workspace_dir / temp_name
        temp_path.write_text(code)

        try:
            cmd = self._build_command(
                args=[f"/workspace/{temp_name}"],
                volumes=self._get_volumes(),
                envs=self._get_envs(),
                workdir="/workspace"
            )

            output, exit_code = self._run_command(cmd)
            self._execution_count += 1
            return output, exit_code
        finally:
            temp_path.unlink(missing_ok=True)

    async def execute_python_async(
        self,
        code: str,
        cwd: Path,
        pythonpath: Path | None = None
    ) -> tuple[str, int]:
        """Run Python code in sandbox asynchronously."""
        # Write code to temp file to avoid CLI arg issues with newlines
        temp_name = f"_temp_{uuid.uuid4().hex[:8]}.py"
        temp_path = self.snapshot_manager.workspace_dir / temp_name
        temp_path.write_text(code)

        try:
            cmd = self._build_command(
                args=[f"/workspace/{temp_name}"],
                volumes=self._get_volumes(),
                envs=self._get_envs(),
                workdir="/workspace"
            )

            output, exit_code = await self._run_command_async(cmd)
            self._execution_count += 1
            return output, exit_code
        finally:
            temp_path.unlink(missing_ok=True)

    def create_file(self, filename: str, content: str, cwd: Path) -> str:
        """Create a file in the workspace."""
        try:
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
        """Clean up (no-op for CLI executor, sandboxes are ephemeral)."""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def create_microsandbox_cli_executor(
    conversation_id: str | None = None,
    base_dir: Path | None = None,
    **kwargs
) -> MicrosandboxCLIExecutor:
    """
    Create a MicrosandboxCLIExecutor for a conversation.

    Usage with PTC:
        from agentd.microsandbox_cli_executor import create_microsandbox_cli_executor

        executor = create_microsandbox_cli_executor(conversation_id="conv_123")
        patch_openai_with_ptc(client, executor=executor)

        # After important turns
        executor.snapshot("after_setup")

        # Time travel
        executor.restore("turn_0001")

        executor.close()
    """
    return MicrosandboxCLIExecutor(
        conversation_id=conversation_id,
        base_dir=base_dir,
        **kwargs
    )
