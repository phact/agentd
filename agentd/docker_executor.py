"""Docker-based Executor.

Runs each bash/python invocation in a fresh `docker run --rm` container.
The agent's cwd is bind-mounted at /workspace; working directory is set to
/workspace so commands see the same files they would in SubprocessExecutor.

This is more isolated than SubprocessExecutor (no shared shell state, no
host filesystem access outside the mounted cwd) and more available than
SandboxRuntimeExecutor (no `srt` install) or Microsandbox (no microVM
runtime) for users who already have Docker Desktop / docker installed.

Trade-offs vs other executors:
  - Cold start: ~200-800ms per command (container spin-up). Fine for an
    agent doing a handful of greps; bad for tight loops.
  - No persistent shell. Each call starts a fresh container, so `cd`, env
    vars set in one call don't survive to the next.
  - Network: defaults to docker's bridge. Pass network="none" to fully
    isolate, or network="host" to share the host namespace.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path


class DockerExecutor:
    """Executor that runs commands in a fresh Docker container per call.

    Parameters
    ----------
    image
        Docker image to run commands in. Must have `bash` and `python` on PATH.
        Defaults to ``python:3.11-slim`` which has both.
    timeout
        Seconds to wait before killing the container.
    network
        ``docker run --network`` value. ``bridge`` (default), ``host``, or
        ``none``.
    mount_workspace
        If True (default), bind-mount the call's ``cwd`` at /workspace. Set
        False to skip the mount (useful when you want a fully ephemeral
        container).
    extra_run_args
        Additional flags appended to every ``docker run``. Use this for
        ``--read-only``, ``-e``, custom mounts, ``--cap-drop=ALL``, etc.
    docker_binary
        Path to docker CLI. Defaults to ``docker`` resolved via PATH.
    """

    def __init__(
        self,
        image: str = "python:3.11-slim",
        timeout: int = 60,
        network: str = "bridge",
        mount_workspace: bool = True,
        extra_run_args: list[str] | None = None,
        docker_binary: str = "docker",
    ):
        self.image = image
        self.timeout = timeout
        self.network = network
        self.mount_workspace = mount_workspace
        self.extra_run_args = list(extra_run_args or [])
        self.docker_binary = docker_binary

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_run_command(self, cwd: Path, container_cmd: list[str]) -> list[str]:
        argv = [self.docker_binary, "run", "--rm", "-i"]
        if self.network:
            argv += ["--network", self.network]
        if self.mount_workspace:
            argv += ["-v", f"{cwd}:/workspace", "-w", "/workspace"]
            # Put /workspace/skills on PATH so the `skills` CLI shipped inside
            # the workspace is callable as a bare command. The base PATH below
            # matches python:3.11-slim's default; if the user picks a different
            # image they can override via extra_run_args.
            argv += [
                "-e",
                "PATH=/workspace/skills:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            ]
        # agentd's PTC layer sets MCP_BRIDGE_URL on the host (typically
        # `http://localhost:<port>`). Inside the container `localhost` is
        # the container itself; rewrite it to `host.docker.internal` so the
        # in-container skill wrappers can call back to the host bridge.
        mcp_url = os.environ.get("MCP_BRIDGE_URL", "http://host.docker.internal:8765")
        for host_alias in ("localhost", "127.0.0.1", "0.0.0.0"):
            mcp_url = mcp_url.replace(f"//{host_alias}:", "//host.docker.internal:")
        argv += [
            "-e", f"MCP_BRIDGE_URL={mcp_url}",
            "--add-host", "host.docker.internal:host-gateway",  # in case the runtime needs the hint (Colima, plain Linux Docker)
        ]
        argv += self.extra_run_args
        argv.append(self.image)
        argv += container_cmd
        return argv

    @staticmethod
    def _format_output(stdout: bytes | str, stderr: bytes | str) -> str:
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        out = stdout.rstrip("\n")
        if stderr:
            err = stderr.rstrip("\n")
            return f"{out}\n{err}" if out else err
        return out

    # ------------------------------------------------------------------ #
    # Executor protocol
    # ------------------------------------------------------------------ #

    def execute_bash(self, command: str, cwd: Path) -> tuple[str, int]:
        cwd = Path(cwd).resolve()
        argv = self._build_run_command(cwd, ["bash", "-c", command])
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.timeout}s", 1
        except FileNotFoundError:
            return (
                f"docker binary not found at '{self.docker_binary}'. "
                "Install Docker Desktop or set docker_binary= explicitly.",
                1,
            )
        except Exception as e:
            return f"Error executing command: {e}", 1
        return self._format_output(result.stdout, result.stderr), result.returncode

    def execute_python(
        self,
        code: str,
        cwd: Path,
        pythonpath: Path | None = None,
    ) -> tuple[str, int]:
        cwd = Path(cwd).resolve()
        # Write the code to a temp file inside cwd so the bind-mount can see it.
        try:
            cwd.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", dir=cwd, delete=False
            ) as f:
                f.write(code)
                host_path = Path(f.name)
        except Exception as e:
            return f"Error preparing python code: {e}", 1
        container_path = f"/workspace/{host_path.name}"
        container_cmd = ["python", container_path]
        extra_env: list[str] = []
        if pythonpath:
            extra_env = ["-e", f"PYTHONPATH={pythonpath}"]
        # Splice extra env directly into argv so we don't mutate self.extra_run_args.
        try:
            argv = self._build_run_command(cwd, container_cmd)
            if extra_env:
                # extra env goes immediately before the image name; rebuild for clarity.
                insert_idx = argv.index(self.image)
                argv = argv[:insert_idx] + extra_env + argv[insert_idx:]
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
            return self._format_output(result.stdout, result.stderr), result.returncode
        except subprocess.TimeoutExpired:
            return f"Python execution timed out after {self.timeout}s", 1
        except Exception as e:
            return f"Error executing Python: {e}", 1
        finally:
            host_path.unlink(missing_ok=True)

    def create_file(self, filename: str, content: str, cwd: Path) -> str:
        # Write directly on the host; the bind-mount makes it visible to the
        # next container invocation.
        try:
            cwd = Path(cwd).resolve()
            filepath = cwd / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)
            return f"Created file: {filename}"
        except Exception as e:
            return f"Error creating file {filename}: {e}"

    def close(self) -> None:
        # Containers are --rm, nothing to tear down.
        return

    # ------------------------------------------------------------------ #
    # Async variants
    # ------------------------------------------------------------------ #

    async def execute_bash_async(self, command: str, cwd: Path) -> tuple[str, int]:
        cwd = Path(cwd).resolve()
        argv = self._build_run_command(cwd, ["bash", "-c", command])
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return (
                f"docker binary not found at '{self.docker_binary}'. "
                "Install Docker Desktop or set docker_binary= explicitly.",
                1,
            )
        except Exception as e:
            return f"Error executing command: {e}", 1
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Command timed out after {self.timeout}s", 1
        return self._format_output(stdout, stderr), proc.returncode or 0

    async def execute_python_async(
        self,
        code: str,
        cwd: Path,
        pythonpath: Path | None = None,
    ) -> tuple[str, int]:
        cwd = Path(cwd).resolve()
        try:
            cwd.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", dir=cwd, delete=False
            ) as f:
                f.write(code)
                host_path = Path(f.name)
        except Exception as e:
            return f"Error preparing python code: {e}", 1
        container_path = f"/workspace/{host_path.name}"
        argv = self._build_run_command(cwd, ["python", container_path])
        if pythonpath:
            insert_idx = argv.index(self.image)
            argv = argv[:insert_idx] + ["-e", f"PYTHONPATH={pythonpath}"] + argv[insert_idx:]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Python execution timed out after {self.timeout}s", 1
            return self._format_output(stdout, stderr), proc.returncode or 0
        except Exception as e:
            return f"Error executing Python: {e}", 1
        finally:
            host_path.unlink(missing_ok=True)


def create_docker_executor(
    image: str = "python:3.11-slim",
    timeout: int = 60,
    **kwargs,
) -> DockerExecutor:
    """Factory mirroring create_subprocess_executor / create_sandbox_runtime_executor."""
    return DockerExecutor(image=image, timeout=timeout, **kwargs)
