# Programmatic Tool Calling (PTC)

PTC gives you a **bash-enabled agent** that unifies **MCP tools with the AgentSkills spec**.

Instead of JSON `tool_calls`, the LLM writes code in fenced blocks, which agentd runs in the sandbox. MCP tools and `@tool` functions are auto-converted to Python bindings in a discoverable skills directory.

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
    model="claude-sonnet-5",
    input=[{"role": "user", "content": "List files, then calculate sqrt(144)"}],
    stream=True
)

for event in display_events(stream):
    if event.type == "text_delta":
        print(event.text, end="", flush=True)
    elif event.type == "code_execution":
        print(f"\n$ {event.code}\n{event.output}\n")
```

## Key Features

**Bash-enabled agent:** The LLM can run shell commands. They run in one persistent shell in the sandbox, so `cd`, `export`, `pushd`/`popd` and shell functions carry over between blocks:
~~~markdown
```bash:execute
ls -la
git status
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

## Auto-Generated Skills Directory

PTC generates a skills directory combining MCP tools and local functions:

```
skills/
  skills                # CLI: `skills list`, `skills read <skill>`, `skills exec`
  lib/
    tools.py            # Python bindings for ALL tools (MCP + @tool)
  filesystem/           # From @modelcontextprotocol/server-filesystem
    SKILL.md            # AgentSkills spec: YAML frontmatter + docs
    scripts/
      read_file_example.py
  local/                # From @tool decorated functions
    SKILL.md
    scripts/
      calculate_example.py
```

The `skills` CLI is on the sandbox's `PATH`; every harness discovers tools the same way.

## MCP Bridge

MCP servers and `@tool` functions run on the host, behind an HTTP bridge on a host Unix socket. Inside the sandbox, `/run/agentd/bridge.sock` is tunneled to it, so the generated bindings reach host tools without any network:

```python
# Auto-generated in skills/lib/tools.py
def read_file(path: str) -> dict:
    return _call("read_file", path=path)  # POST /call/read_file over $MCP_BRIDGE_SOCKET
```

## PTC with MCP Servers

```python
from agents.mcp.server import MCPServerStdio
from agentd import patch_openai_with_ptc

mcp_server = MCPServerStdio(
    params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"]},
    cache_tools_list=True
)

client = patch_openai_with_ptc(OpenAI(), cwd="./workspace")

response = client.responses.create(
    model="claude-sonnet-5",
    input="Explore the available skills and use one",
    mcp_servers=[mcp_server],
    stream=True
)
```

## Display Events

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

## Tool Decorator

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

