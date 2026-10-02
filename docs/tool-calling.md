# Traditional tool calling and the agent daemon

For cases where you want standard JSON `tool_calls` instead of code fences. Tools are MCP calls made from the host; no agent-written code runs.

## Patched Responses API

A lightweight agentic loop that patches the OpenAI client to transparently handle MCP tool calls. Works with any provider via LiteLLM.

```python
from agents.mcp.server import MCPServerStdio
from agentd import patch_openai_with_mcp
from openai import OpenAI

fs_server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/"],
    },
    cache_tools_list=True
)

client = patch_openai_with_mcp(OpenAI())

response = client.chat.completions.create(
    model="gemini/gemini-2.0-flash",  # Any provider via LiteLLM
    messages=[{"role": "user", "content": "List files in /tmp/"}],
    mcp_servers=[fs_server],
)

print(response.choices[0].message.content)
```

**What it does:**
- Patches `chat.completions.create` and `responses.create`
- Auto-connects to MCP servers and extracts tool schemas
- Intercepts tool calls, executes via MCP, feeds results back
- Loops until no more tool calls (max 20 iterations)
- Supports streaming

## Agent Daemon

YAML-configured agents with MCP resource subscriptions. Agents react to resource changes automatically.

```bash
uvx agentd config.yaml
```

**Configuration:**

```yaml
agents:
  - name: news_agent
    model: gpt-4o-mini
    system_prompt: |
      You monitor a URL for changes. When new content arrives,
      save it to ./output/data.txt using the edit_file tool.
    mcp_servers:
      - type: stdio
        command: uv
        arguments: ["run", "mcp_subscribe", "--poll-interval", "5", "--", "uvx", "mcp-server-fetch"]
      - type: stdio
        command: npx
        arguments: ["-y", "@modelcontextprotocol/server-filesystem", "./output/"]
    subscriptions:
      - "tool://fetch/?url=https://example.com/api/data"
```

**How subscriptions work:**
1. Agent connects to MCP servers
2. Subscribes to resource URIs (e.g., `tool://fetch/?url=...`)
3. When resource changes, MCP server sends notification
4. Agent calls the tool, gets result, sends to LLM
5. LLM responds (can call more tools)

Built on [mcp-subscribe](https://github.com/phact/mcp-subscribe).

Each agent also has an interactive REPL:
```
news_agent> What files have you saved?
Assistant: I've saved 3 files to ./output/...
```

