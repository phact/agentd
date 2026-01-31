# agentd/sandbox_runtime_executor.py
"""
Sandbox Runtime executor for OS-level sandboxing using Anthropic's sandbox-runtime.

Uses `srt` CLI to wrap commands with filesystem and network restrictions
via OS-level primitives (sandbox-exec on macOS, bubblewrap on Linux).

See: https://github.com/anthropic-experimental/sandbox-runtime
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
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
        self._workspace_dir = workspace_dir
        self.snapshots_dir = base_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots: list[Snapshot] = []
        self._current_turn = 0

    @property
    def workspace_dir(self) -> Path:
        """The live workspace directory."""
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


class SandboxRuntimeExecutor:
    """
    Executor that runs code with OS-level sandboxing via Anthropic's sandbox-runtime.

    Uses `srt` CLI to wrap commands with filesystem and network restrictions.
    Lighter weight than microVMs but provides meaningful security boundaries.

    Implements the Executor protocol from ptc.py for drop-in replacement.
    """

    def __init__(
        self,
        timeout: int = 60,
        conversation_id: str | None = None,
        base_dir: Path | None = None,
        workspace_dir: Path | None = None,
        # Network config (allow-only pattern)
        allowed_domains: list[str] | None = None,
        denied_domains: list[str] | None = None,
        allow_local_binding: bool = False,
        allow_all_unix_sockets: bool = True,  # Required for MCP tools via Unix socket
        # Filesystem config
        deny_read: list[str] | None = None,
        allow_write: list[str] | None = None,
        deny_write: list[str] | None = None,
        # Optional path to srt binary
        srt_path: str | None = None,
    ):
        self.timeout = timeout
        self.conversation_id = conversation_id or uuid.uuid4().hex[:12]

        # Network configuration
        self.allowed_domains = allowed_domains or []
        self.denied_domains = denied_domains or []
        self.allow_local_binding = allow_local_binding
        self.allow_all_unix_sockets = allow_all_unix_sockets

        # Filesystem configuration
        self.deny_read = deny_read or ["~/.ssh", "~/.aws", "~/.gnupg"]
        self.allow_write = allow_write  # None means use workspace only
        self.deny_write = deny_write or []

        # Find srt binary
        self.srt_path = srt_path or self._find_srt()

        # Setup workspace and snapshots
        if base_dir:
            self.base_dir = base_dir
        else:
            self.base_dir = Path(tempfile.mkdtemp(prefix="agentd_srt_"))

        self.snapshot_manager = SnapshotManager(self.base_dir, workspace_dir=workspace_dir)
        self.snapshot_manager.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Create settings file
        self._settings_path = self.base_dir / "srt-settings.json"
        self._write_settings()

        self._execution_count = 0

    def _find_srt(self) -> str:
        """Find the srt binary."""
        # Check common locations for npm global installs
        locations = [
            os.path.expanduser("~/.npm-global/bin/srt"),
            "/usr/local/bin/srt",
            "/usr/bin/srt",
        ]

        for loc in locations:
            if os.path.isfile(loc) and os.access(loc, os.X_OK):
                return loc

        # Try PATH
        result = subprocess.run(
            ["which", "srt"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()

        raise RuntimeError(
            "srt not found. Install with: npm install -g @anthropic-ai/sandbox-runtime"
        )

    def _write_settings(self) -> None:
        """Write the sandbox-runtime settings file."""
        # Build filesystem allow_write list
        allow_write = self.allow_write
        if allow_write is None:
            # Default: only allow writes to workspace
            allow_write = [str(self.snapshot_manager.workspace_dir)]

        settings = {
            "network": {
                "allowedDomains": self.allowed_domains,
                "deniedDomains": self.denied_domains,
                "allowLocalBinding": self.allow_local_binding,
                "allowAllUnixSockets": self.allow_all_unix_sockets,
            },
            "filesystem": {
                "denyRead": self.deny_read,
                "allowWrite": allow_write,
                "denyWrite": self.deny_write,
            }
        }

        self._settings_path.write_text(json.dumps(settings, indent=2))
        logger.debug(f"Wrote sandbox-runtime settings to {self._settings_path}")

    @property
    def workspace_dir(self) -> Path:
        """The workspace directory for this executor."""
        return self.snapshot_manager.workspace_dir

    @property
    def bridge_socket_path(self) -> Path:
        """Path for MCP bridge Unix socket.

        Returns a socket path in the workspace directory, which is
        accessible from inside the sandbox via bind mount.
        """
        return self.snapshot_manager.workspace_dir / ".mcp-bridge.sock"

    def _build_command(self, cmd: str) -> list[str]:
        """Build the srt-wrapped command."""
        return [
            self.srt_path,
            "--settings", str(self._settings_path),
            cmd
        ]

    def _run_command(self, cmd: list[str], cwd: Path | None = None) -> tuple[str, int]:
        """Run a command and return (output, exit_code)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=cwd or self.snapshot_manager.workspace_dir,
                shell=True if len(cmd) == 1 else False
            )

            # Check for bubblewrap permission errors (common on Linux)
            if result.returncode != 0 and "bwrap" in result.stderr:
                if "Permission denied" in result.stderr or "Operation not permitted" in result.stderr:
                    return (
                        "Sandbox error: bubblewrap lacks required permissions. "
                        "This may require enabling unprivileged user namespaces or "
                        "adjusting AppArmor/seccomp policies. "
                        f"Original error: {result.stderr.strip()}"
                    ), result.returncode

            output = result.stdout
            if result.stderr:
                # Filter out srt info messages
                stderr_lines = []
                for line in result.stderr.split('\n'):
                    if not any(x in line.lower() for x in ['sandbox', 'srt', 'violation']):
                        stderr_lines.append(line)
                stderr = '\n'.join(stderr_lines).strip()
                if stderr:
                    output = f"{output}\n{stderr}" if output else stderr

            return output.strip(), result.returncode

        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.timeout}s", 1
        except Exception as e:
            return f"Error: {e}", 1

    async def _run_command_async(self, cmd: list[str], cwd: Path | None = None) -> tuple[str, int]:
        """Run a command asynchronously."""
        try:
            # Join command for shell execution
            shell_cmd = ' '.join(cmd) if len(cmd) > 1 else cmd[0]

            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or self.snapshot_manager.workspace_dir
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
            stderr_text = stderr.decode() if stderr else ""

            # Check for bubblewrap permission errors
            if proc.returncode != 0 and "bwrap" in stderr_text:
                if "Permission denied" in stderr_text or "Operation not permitted" in stderr_text:
                    return (
                        "Sandbox error: bubblewrap lacks required permissions. "
                        "This may require enabling unprivileged user namespaces or "
                        "adjusting AppArmor/seccomp policies. "
                        f"Original error: {stderr_text.strip()}"
                    ), proc.returncode or 1

            if stderr_text:
                stderr_lines = []
                for line in stderr_text.split('\n'):
                    if not any(x in line.lower() for x in ['sandbox', 'srt', 'violation']):
                        stderr_lines.append(line)
                stderr_filtered = '\n'.join(stderr_lines).strip()
                if stderr_filtered:
                    output = f"{output}\n{stderr_filtered}" if output else stderr_filtered

            return output.strip(), proc.returncode or 0

        except Exception as e:
            return f"Error: {e}", 1

    def execute_bash(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command with sandbox restrictions."""
        workspace = self.snapshot_manager.workspace_dir
        # Write command to temp file for complex commands
        if '\n' in command or len(command) > 500:
            temp_name = f"_temp_{uuid.uuid4().hex[:8]}.sh"
            temp_path = workspace / temp_name
            # Prepend cd to workspace
            temp_path.write_text(f"cd {workspace}\n{command}")
            try:
                full_cmd = self._build_command(f"bash {temp_path}")
                output, exit_code = self._run_command(full_cmd, cwd)
                self._execution_count += 1
                return output, exit_code
            finally:
                temp_path.unlink(missing_ok=True)
        else:
            # Simple command - cd to workspace first
            wrapped_cmd = f"cd {workspace} && {command}"
            full_cmd = self._build_command(f"bash -c {repr(wrapped_cmd)}")
            output, exit_code = self._run_command(full_cmd, cwd)
            self._execution_count += 1
            return output, exit_code

    async def execute_bash_async(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command asynchronously with sandbox restrictions."""
        workspace = self.snapshot_manager.workspace_dir
        if '\n' in command or len(command) > 500:
            temp_name = f"_temp_{uuid.uuid4().hex[:8]}.sh"
            temp_path = workspace / temp_name
            temp_path.write_text(f"cd {workspace}\n{command}")
            try:
                full_cmd = self._build_command(f"bash {temp_path}")
                output, exit_code = await self._run_command_async(full_cmd, cwd)
                self._execution_count += 1
                return output, exit_code
            finally:
                temp_path.unlink(missing_ok=True)
        else:
            wrapped_cmd = f"cd {workspace} && {command}"
            full_cmd = self._build_command(f"bash -c {repr(wrapped_cmd)}")
            output, exit_code = await self._run_command_async(full_cmd, cwd)
            self._execution_count += 1
            return output, exit_code

    def execute_python(
        self,
        code: str,
        cwd: Path,
        pythonpath: Path | None = None
    ) -> tuple[str, int]:
        """Run Python code with sandbox restrictions."""
        workspace = self.snapshot_manager.workspace_dir
        # Write code to temp file
        temp_name = f"_temp_{uuid.uuid4().hex[:8]}.py"
        temp_path = workspace / temp_name
        temp_path.write_text(code)

        try:
            # Build python command with cd to workspace
            if pythonpath:
                python_cmd = f"cd {workspace} && PYTHONPATH={pythonpath}:$PYTHONPATH python {temp_path}"
            else:
                python_cmd = f"cd {workspace} && python {temp_path}"

            full_cmd = self._build_command(python_cmd)
            output, exit_code = self._run_command(full_cmd, cwd)
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
        """Run Python code asynchronously with sandbox restrictions."""
        workspace = self.snapshot_manager.workspace_dir
        temp_name = f"_temp_{uuid.uuid4().hex[:8]}.py"
        temp_path = workspace / temp_name
        temp_path.write_text(code)

        try:
            if pythonpath:
                python_cmd = f"cd {workspace} && PYTHONPATH={pythonpath}:$PYTHONPATH python {temp_path}"
            else:
                python_cmd = f"cd {workspace} && python {temp_path}"

            full_cmd = self._build_command(python_cmd)
            output, exit_code = await self._run_command_async(full_cmd, cwd)
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

    def verify(self) -> tuple[bool, str]:
        """
        Verify that the sandbox is functional.

        Returns:
            Tuple of (success: bool, message: str)

        Use this to check if srt/bubblewrap works on the current system
        before attempting to run untrusted code.
        """
        output, exit_code = self.execute_bash("echo sandbox_ok", Path("."))
        if exit_code == 0 and "sandbox_ok" in output:
            return True, "Sandbox is functional"
        return False, output

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
        """Clean up settings file."""
        if self._settings_path.exists():
            self._settings_path.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def create_sandbox_runtime_executor(
    conversation_id: str | None = None,
    base_dir: Path | None = None,
    **kwargs
) -> SandboxRuntimeExecutor:
    """
    Create a SandboxRuntimeExecutor for a conversation.

    Args:
        conversation_id: Unique identifier for the conversation
        base_dir: Base directory for workspace and snapshots
        **kwargs: Additional arguments passed to SandboxRuntimeExecutor

    Common kwargs:
        allowed_domains: List of domains allowed for network access
        denied_domains: List of domains to block
        allow_local_binding: Allow binding to local ports
        deny_read: Paths to block from reading (default: ~/.ssh, ~/.aws, ~/.gnupg)
        allow_write: Paths allowed for writing (default: workspace only)
        deny_write: Paths to block from writing within allowed zones

    Usage with PTC:
        from agentd import patch_openai_with_ptc, create_sandbox_runtime_executor

        executor = create_sandbox_runtime_executor(
            conversation_id="conv_123",
            allowed_domains=["github.com", "pypi.org"],
        )
        client = patch_openai_with_ptc(OpenAI(), executor=executor)

        # After important turns
        executor.snapshot("after_setup")

        # Time travel
        executor.restore("turn_0001")

        executor.close()
    """
    return SandboxRuntimeExecutor(
        conversation_id=conversation_id,
        base_dir=base_dir,
        **kwargs
    )
