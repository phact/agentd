#!/usr/bin/env python3
"""
PTC Example with MCP servers.

Shows how to use MCP servers with PTC. The MCP tools are exposed
via the HTTP bridge and can be called from skill scripts.
"""
import tempfile
from pathlib import Path

from agentd import patch_openai_with_ptc, display_events
from agents.mcp.server import MCPServerStdio
from openai import OpenAI


SYSTEM_PROMPT = """You are an AI assistant with filesystem tools available.

To run a bash command:
```bash:execute
<command>
```

To create a file:
```filename.ext:create
<contents>
```

You have a ./skills/ directory with tools including filesystem operations.
Explore with `ls skills/` and `cat skills/SKILL.md` to see what's available.

To use MCP tools:
```python:execute
from _lib.tools import read_file, list_directory
print(read_file(path="/some/path"))
```

Always explore the skills directory first to discover available tools."""


def main():
    with tempfile.TemporaryDirectory() as workspace:
        # Create some test files
        (Path(workspace) / "notes.txt").write_text(
            "Meeting notes:\n- Discuss Q4 roadmap\n- Review budget\n- Team updates"
        )
        (Path(workspace) / "config.json").write_text(
            '{"debug": true, "port": 8080, "name": "myapp"}'
        )
        subdir = Path(workspace) / "src"
        subdir.mkdir()
        (subdir / "main.py").write_text('print("Hello, World!")')

        print(f"Workspace: {workspace}")
        print("=" * 60)

        # Setup MCP filesystem server
        fs_server = MCPServerStdio(
            params={
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", workspace],
            },
            cache_tools_list=True
        )

        client = patch_openai_with_ptc(OpenAI(), cwd=workspace)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Explore {workspace} using the available tools. List all files recursively and show me the contents of config.json."
            }
        ]

        print("\n\033[1m🤖 Claude:\033[0m\n")

        stream = client.responses.create(
            model="anthropic/claude-haiku-4-5",
            input=messages,
            mcp_servers=[fs_server],
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


if __name__ == "__main__":
    main()
