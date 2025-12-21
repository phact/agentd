# agentd

LLM agent utilities featuring:

1. **Programmatic Tool Calling (PTC)** - Bash-enabled agents with MCP tools exposed as AgentSkills
2. **Patched Responses API + Agent Daemon** - Traditional tool_calls with MCP, plus YAML-configured reactive agents

## Installation

```bash
pip install agentd
# or
uv add agentd
```

---

## Programmatic Tool Calling (PTC)

PTC gives you a **bash-enabled agent** that unifies **MCP tools with the AgentSkills spec**.

Instead of JSON `tool_calls`, the LLM writes code in fenced blocks. MCP tools and `@tool` functions are auto-converted to Python bindings in a discoverable skills directory.

```python
from agentd import patch_openai_with_ptc, display_events, tool
from openai import OpenAI

@tool
def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    import math
    return str(eval(expression, {"__builtins__": {}}, {"sqrt": math.sqrt}))

client = patch_openai_with_ptc(OpenAI(), cwd="./workspace")

stream = client.responses.create(
    model="claude-sonnet-4-20250514",
    input=[{"role": "user", "content": "List files, then calculate sqrt(144)"}],
    stream=True
)

for event in display_events(stream):
    if event.type == "text_delta":
        print(event.text, end="", flush=True)
    elif event.type == "code_execution":
        print(f"\n$ {event.code}\n{event.output}\n")
```

### Key Features

**Bash-enabled agent:** The LLM can run shell commands directly:
~~~markdown
```bash:execute
ls -la
git status
curl https://api.example.com/data
```
~~~

**MCP + AgentSkills unified:** Tools from MCP servers and `@tool` decorators are exposed as Python functions following the [AgentSkills spec](https://github.com/anthropics/agentskills):
~~~markdown
```python:execute
from lib.tools import read_file, fetch_url
result = read_file(path="/tmp/data.txt")
print(result)
```
~~~

**File creation:** The LLM can create new scripts:
~~~markdown
```my_script.py:create
print("Hello from generated script!")
```
~~~

**XML support:** Also parses Claude's XML function call format:
```xml
<invoke name="bash:execute">
  <parameter name="command">ls -la</parameter>
</invoke>
```

### Auto-Generated Skills Directory

PTC generates a skills directory combining MCP tools and local functions:

```
skills/
  lib/
    tools.py              # Python bindings for ALL tools (MCP + @tool)
  filesystem/             # From @modelcontextprotocol/server-filesystem
    SKILL.md              # AgentSkills spec: YAML frontmatter + docs
    scripts/
      read_file_example.py
  local/                  # From @tool decorated functions
    SKILL.md
    scripts/
      calculate_example.py
```

The LLM discovers tools by exploring:
```bash
ls skills/                           # List available skills
cat skills/filesystem/SKILL.md       # Read skill documentation
```

Then imports and uses them:
```python
from lib.tools import read_file, calculate
```

### MCP Bridge

An HTTP bridge runs locally to route tool calls:

```python
# Auto-generated in skills/lib/tools.py
def read_file(path: str) -> dict:
    return _call("read_file", path=path)  # POST to http://localhost:PORT/call/read_file
```

The bridge dispatches to MCP servers or local Python functions as appropriate.

### PTC with MCP Servers

```python
from agents.mcp.server import MCPServerStdio
from agentd import patch_openai_with_ptc

mcp_server = MCPServerStdio(
    params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"]},
    cache_tools_list=True
)

client = patch_openai_with_ptc(OpenAI(), cwd="./workspace")

response = client.responses.create(
    model="claude-sonnet-4-20250514",
    input="Explore the available skills and use one",
    mcp_servers=[mcp_server],
    stream=True
)
```

### Display Events

```python
from agentd import display_events

for event in display_events(stream):
    match event.type:
        case "text_delta":
            print(event.text, end="")
        case "code_execution":
            print(f"Code: {event.code}")
            print(f"Output: {event.output}")
            print(f"Status: {event.status}")  # "completed" or "failed"
        case "turn_end":
            print("\n---")
```

---

## Traditional Tool Calling

For cases where you want standard JSON `tool_calls` instead of code fences.

### Patched Responses API

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

**Multi-provider support:**
```python
model="gpt-4o"                      # OpenAI
model="claude-sonnet-4-20250514"    # Anthropic
model="gemini/gemini-2.0-flash"     # Google
```

### Agent Daemon

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

---

## API Reference

### Patching Functions

```python
from agentd import patch_openai_with_mcp, patch_openai_with_ptc

# PTC: bash + skills
client = patch_openai_with_ptc(OpenAI(), cwd="./workspace")

# Traditional tool_calls
client = patch_openai_with_mcp(OpenAI())
```

### Tool Decorator

```python
from agentd import tool

@tool
def my_function(arg1: str, arg2: int = 10) -> str:
    """Description goes here.

    arg1: Description of arg1
    arg2: Description of arg2
    """
    return f"Result: {arg1}, {arg2}"
```

---

## Examples

See [`examples/`](./examples/):
- `ptc_with_mcp.py` - PTC with MCP servers
- `ptc_with_tools.py` - PTC with @tool decorator

See [`config/`](./config/) for agent daemon configs.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  ┌─────────────────────────┐      ┌─────────────────────────┐   │
│  │          PTC            │      │     Agent Daemon        │   │
│  │  (bash, skills, MCP)    │      │  (YAML, subscriptions)  │   │
│  └───────────┬─────────────┘      └───────────┬─────────────┘   │
│              │                                │                  │
│              ▼                                ▼                  │
│  ┌─────────────────────┐          ┌─────────────────────┐       │
│  │ patch_openai_ptc    │          │ patch_openai_mcp    │       │
│  │ (fence parse/exec)  │          │ (tool_calls loop)   │       │
│  └─────────┬───────────┘          └─────────┬───────────┘       │
│            │                                │                    │
│   ┌────────┴────────┐                       │                    │
│   │   MCP Bridge    │                       │                    │
│   │  (HTTP server)  │                       │                    │
│   └────────┬────────┘                       │                    │
│            │                                │                    │
│            └────────────────┬───────────────┘                   │
│                             ▼                                    │
│                  ┌─────────────────────┐                        │
│                  │    MCP Servers      │                        │
│                  └─────────────────────┘                        │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## License

MIT
