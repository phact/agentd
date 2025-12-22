#!/usr/bin/env python3
"""
PTC with Microsandbox Example

Demonstrates programmatic tool calling with code execution
running in isolated microVMs via microsandbox.

Requirements:
    - microsandbox installed: curl -sSL https://get.microsandbox.dev | sh
    - KVM enabled (Linux) or Apple Silicon (macOS)
"""

from agentd import (
    patch_openai_with_ptc,
    display_events,
    create_microsandbox_cli_executor,
)
from openai import OpenAI


SYSTEM_PROMPT = """You are an AI assistant with the ability to execute code securely in a sandbox.

To run a bash command:
```bash:execute
<your command here>
```

To run Python code:
```python:execute
<python code>
```

To create a file:
```filename.py:create
<file contents>
```

Your code runs in an isolated microVM. The workspace is at /workspace."""


def main():
    # Create sandboxed executor
    executor = create_microsandbox_cli_executor(
        conversation_id="demo",
        image="python",
        memory=1024,
        timeout=60,
    )

    print(f"Workspace: {executor.snapshot_manager.workspace_dir}")
    print("=" * 60)

    # Create some test files
    executor.create_file("data.csv", "name,age\nAlice,30\nBob,25\nCarol,35", None)
    executor.create_file("config.json", '{"debug": true, "version": "1.0"}', None)

    # Patch OpenAI client with PTC and our executor
    client = patch_openai_with_ptc(
        OpenAI(),
        cwd=str(executor.snapshot_manager.workspace_dir),
        executor=executor,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": """Please do the following:
1. List the files in the workspace
2. Show the contents of data.csv
3. Write a Python script that reads data.csv and prints the average age
4. Run the script""",
        },
    ]

    print("\n🤖 Running with sandboxed execution...\n")

    # Use Responses API with streaming for display_events support
    stream = client.responses.create(
        model="anthropic/claude-haiku-4-5",
        input=messages,
        stream=True,
    )

    for event in display_events(stream):
        if event.type == "text_delta":
            print(event.text, end="", flush=True)

        elif event.type == "code_execution":
            print("\n" + "─" * 50)
            # First line of code is the fence type (bash/python)
            lines = event.code.split("\n")
            fence_type = lines[0] if lines else "code"
            code = "\n".join(lines[1:]) if len(lines) > 1 else event.code
            print(f"⚡ [{fence_type}] {code[:60]}{'...' if len(code) > 60 else ''}")
            if event.output:
                for line in event.output.split("\n")[:10]:
                    print(f"   {line}")
                if event.output.count("\n") > 10:
                    print("   ...")
            if event.status == "failed":
                print("   ❌ (failed)")
            print("─" * 50 + "\n")

        elif event.type == "turn_end":
            print()

    # Take a snapshot
    snapshot = executor.snapshot("after_demo")
    print(f"\n📸 Snapshot created: {snapshot.id}")
    print(f"   Path: {snapshot.path}")

    # Show available snapshots
    print(f"\n📚 Available snapshots: {[s.id for s in executor.list_snapshots()]}")

    executor.close()
    print("\n✅ Done!")


if __name__ == "__main__":
    main()
