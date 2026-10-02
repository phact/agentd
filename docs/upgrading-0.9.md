# Upgrading from 0.8

0.9 runs everything in a sandbox and removes the old executors:

| 0.8 | 0.9 |
|---|---|
| default: code runs on the host (`SubprocessExecutor`) | default: `KrunExecutor`, else `DockerExecutor`; error if neither is set up |
| `SubprocessExecutor`, `SandboxRuntimeExecutor`, `MicrosandboxExecutor`, `MicrosandboxCLIExecutor`, `create_*_executor`, `SandboxConfig` | removed; use `KrunExecutor` or `DockerExecutor` |
| `DockerExecutor`: new `docker run --rm` per command | `DockerExecutor`: one container per session, `--network none`, persistent shell |
| snapshot / restore | removed |
| `MCPBridge(port=..., host=...)`, `start_bridge(port)`, `MCP_BRIDGE_URL` | Unix socket only: `MCPBridge(socket_path)`, `start_bridge(socket_path)`, `MCP_BRIDGE_SOCKET` |
| `setup_skills_directory(..., bridge_port=...)` | `bridge_socket_path=` required |
| skills dir added to the host `PATH` | only on the sandbox's `PATH` |
| Claude subscription fallback via `claude-agent-sdk` (tools redirected by a hook) | the `claude` CLI with every tool disabled, inside the sandbox |

New dependencies: `aiohttp`, `cryptography`.

