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

# Linux (KVM): one command installs pinned, checksum-verified libkrunfw + libkrun,
# grants /dev/kvm access, builds the launcher and the base image (it asks first; uses sudo)
agentd-sandbox linux status
agentd-sandbox linux setup
```

**libkrun inside Colima (macOS on M3 or later, macOS 15+)**: the same microVMs, run with Linux libkrun on KVM inside a Colima VM with nested virtualization:
```bash
agentd-sandbox colima status    # read-only: what's missing
agentd-sandbox colima setup     # shows the plan, asks, then sets it up
```
Setup uses its own Colima profile (`agentd`, 4 CPUs / 8 GiB / 60 GiB by default), so your other Colima VMs are never touched. It enables nested virtualization, turns on transparent huge pages (without them, nested microVMs boot ~5 s slower per GiB of memory), raises the open files limit, installs pinned, checksum-verified libkrun/libkrunfw, builds the launcher, and builds the base image on the VM's own disk. It also restores your active Docker context afterwards (Colima switches it to the VM it starts). Nothing is changed without your approval; recreating an existing VM needs a separate confirmation (or `--yes --recreate`). Then use `KrunExecutor(colima=True)` or `AGENTD_SANDBOX=krun-colima`. Paths are the same inside, since Colima shares `$HOME`.

**Docker (a container per session)**
```bash
python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents   # also tags agentd-sandbox-agents
```

The `agents` image contains Python, git, ripgrep, Claude Code and Codex, with an `agent` user at your uid. On macOS with Docker Desktop or Colima, workspaces must be inside a directory shared with the Docker VM (`$HOME` by default); agentd checks this and tells you if not.

**Custom images.** Any Dockerfile directory can be a sandbox image. It can build on the `agents` image (`FROM agentd-sandbox-agents`) or start from scratch; it needs `python3` 3.9+ and `bash`, and agentd adds the `agent` user itself. In Colima:
```bash
agentd-sandbox colima setup --image-dir ble-mic/sandbox --image rosey   # builds agents first if needed
agentd-sandbox colima setup --image rosey     # later: rebuilds from the same directory if anything changed
agentd-sandbox colima images                  # what's built, and from where
```
```python
KrunExecutor(colima=True, image="rosey")
```
The directory is streamed into the VM, so it doesn't need to be shared with it, and its `.dockerignore` applies. An image is rebuilt when its files, an agentd image it builds `FROM`, or the VM user changes. For native libkrun, `python -m agentd.sandbox.rootfs build DIR NAME` and `KrunExecutor(image=NAME)`.

By default agentd uses native libkrun if it is set up, then libkrun in Colima, then Docker (`agentd-sandbox status` shows which are ready; `AGENTD_SANDBOX=krun|krun-colima|docker` picks one). Code never runs directly on the host.

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

`harness=` picks who runs the agent loop. `"ptc"` (default) is agentd's own loop above; `"claude-code"`, `"codex"`, `"opencode"` ([OpenCode](https://opencode.ai)) and `"omp"` ([oh-my-pi](https://github.com/can1357/oh-my-pi)) run the real CLI agent, with its own tools, **entirely inside the sandbox**.

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
- **Responses API:** `client.responses.create(harness=..., input=..., instructions=...)` works too, including `previous_response_id` and `stream=True` (standard `response.*` events). Give harnesses tools via `mcp_servers=` (they appear as skills), not `tools=`. PTC (`harness="ptc"`) uses the same response store (`~/.agentd/responses`): `previous_response_id` replays the stored conversation, including PTC's code rounds, to the model as input, so you can switch between PTC and a harness mid-conversation in either direction. Ids agentd didn't issue, such as a response stored by OpenAI, are passed through to the provider.
- **Tool calls in streams:** every tool the harness runs (shell commands, file reads/edits, skills, web search) is streamed the same way PTC streams its code executions: a completed `code_interpreter_call` item with the tool name, the command or arguments, and the output, which `display_events` renders as `CodeExecution`. They already ran in the sandbox; clients never execute them. In Responses streams they are separate output items between the assistant `message` items.
- **Stopping:** abandoning a stream (`break`, `close()`, cancelling the task) stops the harness: the CLI and everything it started are killed in the sandbox.
- **Models:** pass the harness's model (`claude-*` for Claude Code, OpenAI models for Codex), or `None` for its default. A model from the other vendor falls back to the default, so switching keeps working. OpenCode and omp take any model: `claude-*` goes through agentd's Anthropic endpoint, an OpenAI model needs `OPENAI_API_KEY` on the host, and any OpenAI-compatible server works through an `upstream` (they need a `model=`, there's no default):
  ```python
  sabik = ModelUpstream("http://10.0.2.58:8001/v1", api="chat")
  client = patch_openai_with_ptc(OpenAI(), harness="opencode", harness_options={
      "opencode": {"upstream": sabik, "model": "Qwen/Qwen3.8-27B"},
      "omp":      {"upstream": sabik, "model": "Qwen/Qwen3.8-27B", "config": {"memory": {"backend": "off"}}},
  })
  ```
  `config` is merged into OpenCode's config (`OPENCODE_CONFIG_CONTENT`) or passed to omp as a `--config` overlay. OpenCode runs with auto-update, model-catalog fetches, LSP downloads and sharing off (the sandbox has no network).
- **Transcripts:** each harness's sessions are stored in `~/.agentd/transcripts/<harness>/<workspace>/`, mounted into the sandbox where the CLI expects them (the sandbox sees only this workspace's sessions). After every turn they're copied to the CLI's usual place, `~/.claude/projects/<workspace>/`, `~/.codex/sessions/` and `~/.omp/agent/sessions/--<workspace>--/`, so `claude --resume` / `codex resume` / `omp --resume` on the host find them. OpenCode keeps sessions in a SQLite database, so its store holds one `opencode export` JSON per session (agentd imports it back to resume in a fresh sandbox; `opencode import FILE` brings one into your own OpenCode). `KrunExecutor(transcripts_dir=..., sync_transcripts=False)` changes the store root or turns the copy off. agentd also writes its own JSONL log of every turn (`AGENTD_LOG_DIR`, default `./logs`).
- **Resume by id:** every response carries `agentd.session_id`, the native session id. Pass it back as `session_id=` to resume that session, even from a new process or a fresh sandbox (if you continued it on the host, the newer copy is used). `previous_response_id` works across restarts too.
- **Instructions:** Claude Code and Codex fix a session's system prompt when it starts; resuming ignores new ones. So when a turn's `instructions` (or system message) differ from those its session started with, agentd starts a new native session seeded with the conversation, and later turns with the same instructions resume that one. With `previous_response_id`, omitted `instructions` keep the conversation's (unlike OpenAI's API, where they lapse), so the native session continues; `instructions=""` clears them.
- **Codex on another model server:** point Codex at any OpenAI-compatible server (a LAN box, vLLM, ...) and set Codex config:
  ```python
  from agentd import ModelUpstream
  client = patch_openai_with_ptc(OpenAI(), harness="codex", harness_options={"codex": {
      "upstream": ModelUpstream("http://10.0.2.58:8080/v1", api="chat"),   # or api="responses"
      "model": "qwen3-coder",                                            # default; a call's model= wins
      "config": {"web_search": "disabled", "features": {"multi_agent": False}},
  }})
  ```
  The sandbox still has no network: agentd proxies the server from the host (adding `api_key=` / `api_key_env=` there) on its own sandbox-side port. Codex only speaks the Responses API, so for `api="chat"` servers the proxy translates to chat completions (function tools included; the model's reply isn't streamed token by token, which Codex doesn't show anyway). `config` keys are Codex's `config.toml` settings, passed as `-c` flags each turn.
- **Lifecycle:** one sandbox per client (executor), started on first use and stopped by `executor.close()`; conversations and harnesses share it. Sessions outlive it: their transcripts are on the host.
- **What's available:** `agentd.available(executor_or_client)` reports, per harness, whether it's ready (its CLI is in the sandbox image and it has a working route to a model) and why not, its default model, and its models: the Anthropic models API for Claude Code, ChatGPT's (or with `OPENAI_API_KEY`, OpenAI's) list for Codex, and an upstream's `/v1/models`; lists come from the same credentials the model proxies use and are cached for 10 minutes.
  ```python
  for name, h in agentd.available(client).items():     # or available(KrunExecutor()), available_async(...)
      print(name, h.ready, h.reasons, h.default_model, [m.id for m in h.models])
  ```
  `default_model=None` means the harness picks (Claude Code; Codex with an API key). `agentd serve` answers the same at `GET /v1/harnesses`, and `GET /v1/models` is an OpenAI-style list (so `client.models.list()` works) where each model names the ready harnesses that can run it.

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
| Channel to host | one host-dialed vsock connection (with Colima: relayed over `colima ssh` stdio) | the host-held `docker run -i` stdio |
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

agentd refuses any share (workspace or mount) that would expose its own sockets (`~/.agentd/run`, `~/.agentd/serve`), its CA key or your Claude/Codex login files, so e.g. mounting `~` is an error rather than a leak.

---

## Network access and secrets (libkrun)

By default a sandbox has no network. `egress=` gives a libkrun sandbox a network card whose every connection goes through agentd on the host, and keeps secrets outside the sandbox:

```python
from agentd import KrunExecutor
from agentd.egress import Egress
from agentd.egress.approvals import Approvals

executor = KrunExecutor(egress=Egress(
    allow=["pypi.org", "files.pythonhosted.org", "github.com:22"],      # reachable as-is
    approvals=Approvals(webhook="https://approver.example/hook"),       # optional: ask instead of refusing
))
```

- **Everything goes through agentd.** `agentd-net` (Rust, `agentd/sandbox/net`) is the sandbox's network card: a user-space TCP/IP stack on the host that answers DNS with stand-in addresses (so every connection is known by name) and hands each connection to agentd's egress proxy. Every program is covered, whether or not it honors proxy settings.
- **Secrets stay on the host.** Secret rules come from [fnox](https://fnox.jdx.dev) (`[proxy.rules]` in the workspace's `fnox.toml`). The sandbox gets placeholders (same length and prefix as the real value) in its environment; for a rule's host, agentd terminates TLS with a per-session CA the sandbox trusts, puts the real value into the rule's header only for the rule's methods and paths, and scrubs real values out of responses. HTTP/1.1 and HTTP/2, streaming, no size limits.
- **Allowed hosts pass through untouched** (the server sees the client's own TLS); raw TCP by host and port (`github.com:22`). Everything else is refused: HTTPS and HTTP get a 403 explaining why.
- **Approvals:** with `approvals=`, a blocked connection or a secret used outside its rule is announced to your webhook (signed, `X-Agentd-Signature`) and held ~25 s while an approver decides (`agentd serve`: `POST /v1/approvals/{id}` with `once`, `session`, `always` or `deny`). Not decided in time: the agent gets a 403 with the pending approval's id and retries. `always` writes the rule into fnox's config (host allowances into `~/.agentd/egress/allow.toml`). Agents can ask ahead with the `request_access` skill (`agentd.egress.approvals.enable_access_skill`).
- **Audit log** of every decision: `~/.agentd/egress/audit.jsonl` (never header values or bodies).
- **Docker sandboxes have no network** (`--network none`); egress is libkrun-only for now.

Setup: `agentd/sandbox/build.sh` builds `agentd-net` (needs cargo); `agentd-sandbox colima setup` / `linux setup` build libkrun with networking.

**Host-side tools that need secrets** get them from fnox on the host, never the sandbox: `agentd.secrets.secret("DATABASE_URL")` in `@tool` functions, `secret_env(["GITHUB_TOKEN"])` for an MCP server's environment. Everything the bridge returns to a sandbox is scrubbed of every secret resolved this way.

**Phones and browsers** are host-side tools too (the sandbox never touches them), granted as time-limited leases through the same approvals:

- `agentd.devices.android`: `Android(serial=..., apps={...})` + `enable_android_skills(...)`: screenshot (into the workspace), UI elements, tap, tap-by-text, type, keys, swipe, open app (allowlist), list apps; over adb.
- `agentd.devices.browser`: `Browser(logins={...})` + `enable_browser_skills(...)`: a real headed Chrome with a fresh profile per lease (wiped at the end), driven over the DevTools Protocol through a pipe with no automation tells; `browser_login(site)` fills credentials and TOTP codes from fnox on the host (the agent never sees them); an optional host allowlist enforced on every request.

Design and security model: [docs/egress-and-secrets.md](docs/egress-and-secrets.md).

---

## agentd serve: sessions as a service, across boxes

`agentd serve` runs this box's sandboxed sessions as an HTTP API on two Unix sockets, never TCP:

- `~/.agentd/serve/serve.sock` for local callers (the socket's permissions are the gate);
- `~/.agentd/serve/peers.sock` for other boxes over a [p2claw](https://github.com/phact/p2claw-skill) private route; every request must carry the caller's `X-P2claw-Peer` identity.

```bash
agentd serve                                              # --config ~/.agentd/serve/config.json
p2claw apps expose agentd --socket ~/.agentd/serve/peers.sock   # private: never public, no URL
p2claw apps share agentd --with <peer>
```

Turns use the Responses API above (`harness`, `model`, `input`, `instructions`, `previous_response_id`, `stream`), plus:

| | |
|---|---|
| `session_id` / `workspace` / `image` | continue a session, or start one in a workspace under an allowed root (default: a fresh directory) |
| `background=true` | the turn runs server-side; reattach with `GET /v1/responses/{id}?stream=true&starting_after=N`, stop it with `POST /v1/responses/{id}/cancel` (otherwise a turn is tied to its request: hanging up cancels it) |
| `/v1/sessions[/{id}[/transcript]]`, `DELETE /v1/sessions/{id}` | list, inspect, read (paged) and close sessions |
| `/v1/schedules` | timed turns: `{"input": ..., "every": "0 9 * * 1-5", "timezone": "America/New_York", "session_id": ...}` or `"at"`, or `"every": "30m"`; persisted, never overlapping |
| `/v1/legacy/claude-code[/{id}]` | Claude Code sessions run on the host outside agentd, read-only and paged |
| `/v1/harnesses`, `/v1/models` | which harnesses are ready (and why not) with their default and available models; an OpenAI-style model list naming the harnesses for each |
| `/v1/info` | box, version, harnesses (and which are ready), images |

Sessions are durable; their sandboxes stop after `idle_timeout` (default 10 min) and the next turn resumes the native session in a fresh one. The peer that starts a session owns it: other peers can read it but only the owner (or peers listed in `drivers`) can run turns, cancel, close or schedule in it. Settings (`~/.agentd/serve/config.json`, all optional): `box_name`, `workspace_roots`, `idle_timeout`, `drivers`, `default_harness`, `sandbox` (`backend`, `image`, `cpus`, `mem_mib`, `mounts`), `harness_options` (e.g. a Codex `upstream`).

**From code or agents** (`agentd.remote`; reaching other boxes needs `pip install p2claw-agent-client`):

```python
from agentd.remote import Fleet, enable_fleet_skills

fleet = Fleet.from_config()   # ~/.agentd/fleet.json: {"boxes": {"sabik": {"peer": "<alias>", "drive": true}, ...}}
r = await fleet.box("sabik").start("Fix the flaky test", workspace="app", background=True)

enable_fleet_skills(fleet)    # fleet_boxes, fleet_sessions, fleet_read, fleet_start, fleet_send,
                              # fleet_result, fleet_cancel, fleet_schedule, fleet_unschedule as skills
```

Fleet skills run on the host (through the MCP bridge), so the p2claw socket never enters a sandbox; boxes without `"drive": true` are read-only. Design and security model: [docs/agentd-serve.md](docs/agentd-serve.md).

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
             image=None,                   # or a base image by name instead of rootfs=
             colima=None,                  # True or a profile name: libkrun inside Colima
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
| `agentd/serve/` | `agentd serve`: the session API on Unix sockets (turns, sessions, schedules, legacy transcripts, idle sandboxes) |
| `agentd/egress/` | Egress proxy (policy, per-session CA, HTTP/1.1 + HTTP/2 interception, approvals) |
| `agentd/sandbox/net/` | `agentd-net` (Rust): the sandbox's network card on the host |
| `agentd/secrets.py`, `agentd/devices/` | Host-side secrets from fnox; Android (adb) and browser (Chrome) tools |
| `agentd/remote.py`, `remote_skills.py` | Client for `agentd serve` here or on other boxes (p2claw), and the `fleet_*` skills |

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
