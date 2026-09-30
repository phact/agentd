# agentd

LLM agent utilities featuring:

1. **Programmatic Tool Calling (PTC)** - Bash-enabled agents with MCP tools exposed as AgentSkills
2. **Agent harnesses** - Run Claude Code or Codex through the same OpenAI-style API, and switch between them mid-conversation
3. **Sandboxes** - All agent-written code and every harness run in a libkrun microVM or Docker container with **no network and no credentials**
4. **Patched Responses API + Agent Daemon** - Traditional tool_calls with MCP, plus YAML-configured reactive agents

## Installation

```bash
pip install agentd
# or
uv add agentd
```

Then set up a sandbox backend (once per machine, from a checkout of this repo):

**libkrun (recommended: a real microVM per session; macOS on Apple Silicon, or Linux with KVM)**
```bash
# macOS: libkrun from its maintainer's Homebrew tap
brew tap slp/krun
brew trust --formula slp/krun/libkrun slp/krun/libkrunfw slp/krun/virglrenderer-krun
brew install slp/krun/libkrun

agentd/sandbox/build.sh                                               # build + sign the launcher
python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents   # base image
```

**Docker (a container per session)**
```bash
python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents   # also tags agentd-sandbox-agents
```

The `agents` image contains Python, git, ripgrep, Claude Code and Codex, with an `agent` user at your uid. On macOS with Docker Desktop or Colima, workspaces must be inside a directory shared with the Docker VM (`$HOME` by default); agentd checks this and tells you if not.

By default agentd uses libkrun if it is set up, otherwise Docker. Code never runs directly on the host.

---

## Programmatic Tool Calling (PTC)

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

### Key Features

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

### Auto-Generated Skills Directory

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

### MCP Bridge

MCP servers and `@tool` functions run on the host, behind an HTTP bridge on a host Unix socket. Inside the sandbox, `/run/agentd/bridge.sock` is tunneled to it, so the generated bindings reach host tools without any network:

```python
# Auto-generated in skills/lib/tools.py
def read_file(path: str) -> dict:
    return _call("read_file", path=path)  # POST /call/read_file over $MCP_BRIDGE_SOCKET
```

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
    model="claude-sonnet-5",
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

## Agent Harnesses

`harness=` picks who runs the agent loop. `"ptc"` (default) is agentd's own loop above; `"claude-code"` and `"codex"` run the real CLI agent, with its own tools, **entirely inside the sandbox**.

```python
from agentd import patch_openai_with_ptc
from openai import OpenAI

client = patch_openai_with_ptc(OpenAI(), cwd="./workspace", harness="claude-code")

msgs = [{"role": "user", "content": "Add a test for utils.py and run it"}]
r = client.chat.completions.create(model="claude-sonnet-5", messages=msgs)

# Same conversation, now Codex (per-call override)
msgs += [{"role": "assistant", "content": r.choices[0].message.content},
         {"role": "user", "content": "Review that test"}]
r = client.chat.completions.create(model=None, messages=msgs, harness="codex")
```

- **Conversations:** the `messages` you send are the history. If they match the previous exchange on the same harness, agentd resumes that harness's native session (with full tool context); otherwise, e.g. after switching harness, it starts a new session seeded with the history. Every harness shares the client's sandbox, so files and processes survive a switch.
- **Responses API:** `client.responses.create(harness=..., input=..., instructions=...)` works too, including `previous_response_id` and `stream=True` (standard `response.*` events). The output is the assistant message; the harness's own tool activity is logged, not returned. Give harnesses tools via `mcp_servers=` (they appear as skills), not `tools=`.
- **Models:** pass the harness's model (`claude-*` for Claude Code, OpenAI models for Codex), or `None` for its default. A model from the other vendor falls back to the default, so switching keeps working.
- **Transcripts:** each harness's sessions are stored in `~/.agentd/transcripts/<harness>/<workspace>/`, mounted into the sandbox where the CLI expects them (the sandbox sees only this workspace's sessions). After every turn they're copied to the CLI's usual place, `~/.claude/projects/<workspace>/` and `~/.codex/sessions/`, so `claude --resume` / `codex resume` on the host find them. `KrunExecutor(transcripts_dir=..., sync_transcripts=False)` changes the store root or turns the copy off. agentd also writes its own JSONL log of every turn (`AGENTD_LOG_DIR`, default `./logs`).
- **Resume by id:** every response carries `agentd.session_id`, the native session id. Pass it back as `session_id=` to resume that session, even from a new process or a fresh sandbox (if you continued it on the host, the newer copy is used). `previous_response_id` works across restarts too.
- **Lifecycle:** one sandbox per client (executor), started on first use and stopped by `executor.close()`; conversations and harnesses share it. Sessions outlive it: their transcripts are on the host.

### Credentials

Real credentials never enter the sandbox. Inside it, each harness talks to a sandbox-side endpoint and holds only a placeholder; agentd's host-side model proxy replaces it with the real credential and forwards to the provider:

| Harness | Credential (on the host) |
|---|---|
| Claude Code, PTC with `claude-*` | `ANTHROPIC_API_KEY`, else your Claude Code login (macOS Keychain / `~/.claude/.credentials.json`) |
| Codex | `OPENAI_API_KEY`, else your Codex ChatGPT login (`~/.codex/auth.json`) |

Subscription tokens are only read, never refreshed by agentd (refreshing would log out your own CLI); run the CLI on the host to refresh them. Codex's ChatGPT mode only talks to `https://chatgpt.com`, so inside the sandbox agentd serves that hostname over TLS with a certificate from a local agentd CA (`~/.agentd/ca`) that only sandboxes trust.

---

## Sandboxes

| | `KrunExecutor` | `DockerExecutor` |
|---|---|---|
| Boundary | libkrun microVM (own kernel) | Docker container (shared kernel) |
| Network | none (no NIC, no TSI) | `--network none` |
| Channel to host | one host-dialed vsock connection | the host-held `docker run -i` stdio |
| Base image | shared read-only + per-session in-memory overlay | image + container layer |
| Startup | ~0.3s | ~0.2s |

Both behave the same:

- **One sandbox per client**, started on first use. The workspace (`cwd`) appears at the same path inside, as do the skills dir and transcript folders. Nothing else from the host is visible unless you add it with `mounts=`.
- **No network.** The only way out is a few sandbox-side endpoints tunneled over the host's one connection: the MCP bridge and the model proxies. Each has a fixed host target.
- **No host environment.** Code runs as the `agent` user with an explicit environment; your keys and env vars don't leak in.
- Files written outside the workspace (e.g. `/tmp`) disappear when the session ends.

```python
from agentd import patch_openai_with_ptc, KrunExecutor, DockerExecutor

client = patch_openai_with_ptc(OpenAI(), cwd="./ws", executor=KrunExecutor())
client = patch_openai_with_ptc(OpenAI(), cwd="./ws", executor=DockerExecutor(timeout=120))
```

**Read-only mounts:** give the sandbox extra host directories it can read but never change:

```python
KrunExecutor(mounts=["~/datasets", "/opt/models"])        # each at the same path inside
DockerExecutor(mounts={"~/notes": "/home/agent/notes"})   # or {host_path: sandbox_path}
```

Read-only is enforced outside the sandbox (libkrun's read-only virtiofs device; Docker `:ro` bind mounts), so even root inside can't write. Sources must be existing directories, and system paths (`/usr`, `/etc`, ...) can't be mounted over. With Docker on macOS, they must be inside a directory shared with the Docker VM.

---

## Traditional Tool Calling

For cases where you want standard JSON `tool_calls` instead of code fences. Tools are MCP calls made from the host; no agent-written code runs.

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

```python
from agentd import (
    patch_openai_with_ptc, patch_openai_with_mcp,
    KrunExecutor, DockerExecutor, default_executor, tool,
)

# PTC / harnesses (sandboxed). harness: "ptc" | "claude-code" | "codex"
client = patch_openai_with_ptc(
    OpenAI(),
    cwd="./workspace",          # the sandbox workspace
    executor=None,              # default_executor(): libkrun if set up, else Docker
    skills_dir=None,            # default: cwd/skills
    harness="ptc",              # overridable per call: create(..., harness="codex")
)

KrunExecutor(rootfs=DEFAULT_ROOTFS,        # ~/.agentd/rootfs/agents ($AGENTD_ROOTFS)
             timeout=60, cpus=2, mem_mib=2048,
             mounts=None,                  # read-only: ["~/data"] or {"~/data": "/data"}
             transcripts_dir=None, sync_transcripts=True)
DockerExecutor(image="agentd-sandbox-agents", ...)   # same options

# Harness calls also accept session_id= (resume a native session by id)

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
- `ptc_streaming.py` - streaming PTC output

See [`config/`](./config/) for agent daemon configs.

---

## Architecture

```
 patch_openai_with_ptc(client, executor=..., harness=...)
   ├─ harness="ptc"          agentd's loop on the host; code fences run in the sandbox
   │                         (model: LiteLLM with an API key, or `claude -p` with no
   │                          tools inside the sandbox on a Claude subscription)
   ├─ harness="claude-code"  `claude -p --output-format stream-json`, in the sandbox
   └─ harness="codex"        `codex exec --json`, in the sandbox

 HOST                                            SANDBOX (microVM or container, no network)
 ┌──────────────────────────────┐   one host-   ┌────────────────────────────────────┐
 │ SandboxSession               │   held, muxed │ sandboxd: exec, shell, endpoints   │
 │  ├ model proxies (real creds)│◄── channel ──►│  anthropic 127.0.0.1:8080          │
 │  │   → api.anthropic.com,    │               │  openai    127.0.0.1:8081          │
 │  │     chatgpt.com, OpenAI   │               │  chatgpt.com:443 (TLS, agentd CA)  │
 │  ├ MCP bridge (Unix socket)  │               │  /run/agentd/bridge.sock ← skills  │
 │  │   → MCP servers, @tool    │               │                                    │
 │  └ shared: workspace,        │               │ workspace & transcripts at their   │
 │    transcripts               │               │ host paths; `agent` user           │
 └──────────────────────────────┘               └────────────────────────────────────┘
```

| Module | Role |
|---|---|
| `agentd/sandbox/` | `base` (channel, exec/shell, endpoints), `krun` / `docker` backends, `sandboxd` + `mux` (inside the sandbox), `session`, `executor`, `tls`, `rootfs`, `launcher.c` |
| `agentd/model_proxy.py` | Host-side proxies that add credentials |
| `agentd/harness/` | Claude Code and Codex drivers, `harness=` routing for chat and Responses |
| `agentd/ptc.py` | PTC loop, skills generation, client patching |
| `agentd/mcp_bridge.py` | MCP / `@tool` bridge on a Unix socket |

---

## Upgrading from 0.8

0.9 runs everything in a sandbox and removes the old executors:

| 0.8 | 0.9 |
|---|---|
| default: code runs on the host (`SubprocessExecutor`) | default: `KrunExecutor`, else `DockerExecutor`; error if neither is set up |
| `SubprocessExecutor`, `SandboxRuntimeExecutor`, `MicrosandboxExecutor`, `MicrosandboxCLIExecutor`, `create_*_executor`, `SandboxConfig` | removed; use `KrunExecutor` or `DockerExecutor` |
| `DockerExecutor`: new `docker run --rm` per command | `DockerExecutor`: one container per session, `--network none`, persistent shell |
| snapshot / restore | removed |
| `MCPBridge(port=..., host=...)`, `start_bridge(port)`, `MCP_BRIDGE_URL` | Unix socket only: `MCPBridge(socket_path)`, `start_bridge(socket_path)`, `MCP_BRIDGE_SOCKET` |
| `setup_skills_directory(..., bridge_port=...)` | `bridge_socket_path=` required |
| skills dir added to the host `PATH` | only on the sandbox's `PATH` |
| Claude subscription fallback via `claude-agent-sdk` (tools redirected by a hook) | the `claude` CLI with every tool disabled, inside the sandbox |

New dependencies: `aiohttp`, `cryptography`.

---

## License

MIT
