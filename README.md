# agentd

Run agents behind the OpenAI client API, sandboxed.

- **PTC**: the model writes bash/python fences instead of JSON tool calls; MCP servers and `@tool` functions show up as skills.
- **Harnesses**: run Claude Code, Codex, OpenCode or omp through the same API and switch between them mid-conversation.
- **Sandboxes**: all agent code runs in a libkrun microVM or a Docker container, with no network and no credentials.
- **Egress**: optional network access through a host proxy that injects fnox secrets, so the sandbox never holds them.
- **agentd serve**: sessions, background turns and schedules as an API, reachable from other boxes over p2claw.

## Install

```bash
uv add agentd
```

Then set up a sandbox (from a checkout of this repo):

```bash
# macOS (Apple Silicon)
brew tap slp/krun
brew trust --formula slp/krun/libkrun slp/krun/libkrunfw slp/krun/virglrenderer-krun
brew install slp/krun/libkrun
agentd/sandbox/build.sh
python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents

# Linux (KVM)
agentd-sandbox linux setup

# macOS via Colima (M3+, macOS 15+)
agentd-sandbox colima setup

# or Docker
python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents
```

`agentd-sandbox status` shows what's ready. Details, custom images and mounts: [docs/sandboxes.md](docs/sandboxes.md).

## PTC

```python
from agentd import patch_openai_with_ptc, display_events, tool
from openai import OpenAI

@tool
def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    return str(eval(expression, {"__builtins__": {}}))

client = patch_openai_with_ptc(OpenAI(), cwd="./workspace")
stream = client.responses.create(model="claude-sonnet-5", input="List files, then calculate 12*12", stream=True)

for event in display_events(stream):
    if event.type == "text_delta":
        print(event.text, end="")
    elif event.type == "code_execution":
        print(f"\n$ {event.code}\n{event.output}")
```

More: [docs/ptc.md](docs/ptc.md).

## Harnesses

```python
client = patch_openai_with_ptc(OpenAI(), cwd="./workspace", harness="claude-code")
r = client.responses.create(model="claude-sonnet-5", input="Add a test for utils.py and run it")
r = client.responses.create(input="Review that test", previous_response_id=r.id, harness="codex")
```

`harness=` is `"ptc"` (default), `"claude-code"`, `"codex"`, `"opencode"` or `"omp"`. Each runs the real CLI inside the sandbox; credentials stay on the host behind a proxy. `agentd.available(client)` says which harnesses are ready and what models they have. Resume, transcripts, custom model servers: [docs/harnesses.md](docs/harnesses.md).

## Network and secrets

```python
from agentd import KrunExecutor
from agentd.egress import Egress
from agentd.egress.approvals import Approvals

executor = KrunExecutor(egress=Egress(
    allow=["pypi.org", "files.pythonhosted.org"],
    approvals=Approvals(webhook="https://approver.example/hook"),
))
```

Sandboxes get placeholders; the proxy swaps in real values from fnox only for the hosts, methods and paths its rules allow. Anything else is refused or sent to your approval webhook. libkrun only. [docs/egress-and-secrets.md](docs/egress-and-secrets.md).

## agentd serve

```bash
agentd serve
```

Serves sessions over Unix sockets: turns, background runs, schedules, transcripts. Other boxes reach it over p2claw, and `agentd.remote.Fleet` drives them. [docs/agentd-serve.md](docs/agentd-serve.md).

## Plain tool calling

`patch_openai_with_mcp(OpenAI())` runs MCP tool calls in a regular `tool_calls` loop with any LiteLLM provider, and `uvx agentd config.yaml` runs YAML-configured agents that react to MCP resource changes. [docs/tool-calling.md](docs/tool-calling.md).

Coming from 0.8: [docs/upgrading-0.9.md](docs/upgrading-0.9.md).

## License

MIT
