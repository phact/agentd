# Sandboxes

Setup, backends, mounts and how the pieces fit.

## Setup

Then set up a sandbox backend (once per machine, from a checkout of this repo):

**libkrun (recommended: a real microVM per session; macOS on Apple Silicon, or Linux with KVM)**
```bash
# macOS: libkrun from its maintainer's Homebrew tap
brew tap slp/krun
brew trust --formula slp/krun/libkrun slp/krun/libkrunfw slp/krun/virglrenderer-krun
brew install slp/krun/libkrun

agentd/sandbox/build.sh                       # from a checkout only: build + sign the launcher
python -m agentd.sandbox.rootfs build agents  # base image

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
python -m agentd.sandbox.rootfs build agents   # also tags agentd-sandbox-agents
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

### Prebuilt binaries

The macOS arm64 and Linux x86_64/aarch64 wheels carry the `agentd-krun` launcher (ad-hoc signed with the hypervisor entitlement on macOS), `agentd-net`, and libkrun built with networking for Linux (the macOS wheel also has the Linux aarch64 launcher and libkrun, for Colima). `linux setup` and `colima setup` install those instead of compiling, so they need no compiler, Rust or libclang; libkrunfw is still downloaded (pinned, sha256-checked). `bin/prebuilt.json` records the `launcher.c` and libkrun they were built from, and binaries that don't match the installed code are ignored, so a checkout with edited sources builds as before.

## Backends

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

