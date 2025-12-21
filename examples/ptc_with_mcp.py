#!/usr/bin/env python3
"""
PTC Example with MCP servers.

Shows how to use MCP servers with PTC. The MCP tools are exposed
as Python functions the LLM can import and call.
"""
import tempfile
from pathlib import Path

from agentd import patch_openai_with_ptc, display_events
from agents.mcp.server import MCPServerStdio
from openai import OpenAI


SYSTEM_PROMPT = """You are an AI assistant that can run bash commands and Python code.

To run bash:
```bash:execute
<command>
```

To run Python:
```python:execute
from lib.tools import some_function
result = some_function(arg="value")
print(result)
```

To create a file:
```filename.ext:create
<contents>
```

You have a ./skills/ directory with available tools:
- `skills/lib/tools.py` - shared module with all tool functions
- `skills/<name>/SKILL.md` - documentation for each skill (YAML frontmatter + instructions)
- `skills/<name>/scripts/` - example scripts

To discover skills:
1. `ls skills/` to list available skills
2. `head -5 skills/<name>/SKILL.md` to read frontmatter (name, description)
3. Read full SKILL.md only if you need detailed instructions"""


def main():
    with tempfile.TemporaryDirectory() as workspace:
        print(f"Workspace: {workspace}")
        print("=" * 60)

        # Setup MCP "everything" server (reference server with sample tools)
        mcp_server = MCPServerStdio(
            params={
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-everything"],
            },
            cache_tools_list=True,
            client_session_timeout_seconds=60  # Longer timeout for tool calls
        )

        client = patch_openai_with_ptc(OpenAI(), cwd=workspace)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Check out the available skills, then use one of the tools to do something interesting."
            }
        ]

        print("\n\033[1m🤖 Claude:\033[0m\n")

        stream = client.responses.create(
            model="anthropic/claude-haiku-4-5",
            input=messages,
            mcp_servers=[mcp_server],
            stream=True
        )

        for event in display_events(stream):
            if event.type == "text_delta":
                print(event.text, end="", flush=True)
            elif event.type == "code_execution":
                print("\n" + "─" * 50)
                print(f"\033[33m⚡ {event.code}\033[0m")
                if event.output:
                    for line in event.output.split('\n'):
                        print(f"   \033[36m{line}\033[0m")
                if event.status == "failed":
                    print(f"   \033[31m(failed)\033[0m")
                print("─" * 50 + "\n")
            elif event.type == "turn_end":
                print()

        # Clean up MCP connections before exit
        client.cleanup_ptc()


if __name__ == "__main__":
    main()
