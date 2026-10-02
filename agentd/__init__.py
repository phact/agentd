from agentd.conversation_logger import ConversationLog
from agentd.patch import patch_openai_with_mcp
from agentd.ptc import patch_openai_with_ptc, display_events, display_events_async, TextDelta, CodeExecution, TurnEnd
from agentd.tool_decorator import tool
from agentd.model_proxy import ModelUpstream
from agentd.availability import available, available_async, HarnessStatus, ModelInfo
from agentd.sandbox.executor import (
    SandboxExecutor,
    KrunExecutor,
    DockerExecutor,
    default_executor,
)

__all__ = [
    'ModelUpstream',
    'available',
    'available_async',
    'HarnessStatus',
    'ModelInfo',
    'patch_openai_with_mcp',
    'patch_openai_with_ptc',
    'display_events',
    'display_events_async',
    'TextDelta',
    'CodeExecution',
    'TurnEnd',
    'tool',
    'ConversationLog',
    # Sandboxes: code and harnesses run here, never on the host.
    'SandboxExecutor',
    'KrunExecutor',     # libkrun microVM (preferred)
    'DockerExecutor',   # Docker container, --network none
    'default_executor',
]
