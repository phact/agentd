from dataclasses import dataclass, field
from typing import List
from pydantic import AnyUrl


@dataclass
class MCPServerConfig:
    type: str
    command: str
    arguments: List[str]
    env_vars: List[str] = field(default_factory=list)
    tool_filter: List[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    name: str
    model: str
    system_prompt: str
    mcp_servers: List[MCPServerConfig]
    subscriptions: List[AnyUrl] = field(default_factory=list)


@dataclass
class Config:
    agents: List[AgentConfig]
