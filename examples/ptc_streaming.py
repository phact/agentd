#!/usr/bin/env python3
"""
PTC Streaming Example with Claude Haiku 4.5

Demonstrates programmatic tool calling using code fences
instead of native tool calling API.
"""
import tempfile
from pathlib import Path

from agentd import patch_openai_with_ptc, display_events
from openai import OpenAI


SYSTEM_PROMPT = """You are an AI assistant with the ability to execute bash commands and create files.

To run a bash command, use this EXACT format:
```bash:execute
<your command here>
```

To create a file, use:
```filename.py:create
<file contents>
```

The command output will be shown to you. Always use these formats - regular code blocks will NOT execute.

You have a ./skills/ directory with tools available. Run `ls` or explore to see what's there."""


def main():
    # Create a workspace directory
    with tempfile.TemporaryDirectory() as workspace:
        # Create some test files
        (Path(workspace) / "data.txt").write_text(
            "Alice,25,Engineer\nBob,30,Designer\nCarol,28,Manager"
        )
        (Path(workspace) / "numbers.json").write_text(
            '{"values": [10, 20, 30, 40, 50]}'
        )

        print(f"Workspace: {workspace}")
        print("=" * 60)

        # Patch client with PTC
        client = patch_openai_with_ptc(OpenAI(), cwd=workspace)

        # Start conversation
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""Explore the workspace at {workspace} and tell me:
1. What files are there?
2. What's in each file?
3. Calculate the sum of the numbers in numbers.json"""
            }
        ]

        print("\n\033[1m🤖 Claude Haiku 4.5:\033[0m\n")

        # Use streaming with display_events() for clean output
        stream = client.responses.create(
            model="anthropic/claude-haiku-4-5",
            input=messages,
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
