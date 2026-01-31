from agentd.patch import patch_openai_with_mcp
from agentd.ptc import patch_openai_with_ptc, display_events, TextDelta, CodeExecution, TurnEnd
from agentd.tool_decorator import tool
from agentd.microsandbox_executor import (
    MicrosandboxExecutor,
    create_microsandbox_executor,
    SandboxConfig,
)
from agentd.microsandbox_cli_executor import (
    MicrosandboxCLIExecutor,
    create_microsandbox_cli_executor,
)
from agentd.sandbox_runtime_executor import (
    SandboxRuntimeExecutor,
    create_sandbox_runtime_executor,
)

__all__ = [
    'patch_openai_with_mcp',
    'patch_openai_with_ptc',
    'display_events',
    'TextDelta',
    'CodeExecution',
    'TurnEnd',
    'tool',
    # API-based executor (blocked by https://github.com/microsandbox/microsandbox/issues/314)
    'MicrosandboxExecutor',
    'create_microsandbox_executor',
    'SandboxConfig',
    # CLI-based executor (recommended)
    'MicrosandboxCLIExecutor',
    'create_microsandbox_cli_executor',
    # OS-level sandbox (lightweight, no containers)
    'SandboxRuntimeExecutor',
    'create_sandbox_runtime_executor',
]
