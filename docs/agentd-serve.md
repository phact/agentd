# agentd serve: sandboxed agent sessions over p2claw

Status: implemented in agentd 0.9.0 (2026-10-01); see "Implementation" at the
end. agentd and p2claw changes are owned by their maintainer; this doc
records the agreed shape.

## Summary

Every box runs `agentd serve`: an API over that box's sandboxed agent sessions,
listening only on Unix sockets (one for local callers, one for p2claw). p2claw
exposes it to your other boxes as a **private route**, which gives transport
and caller identity with no sshd, keys or user accounts. Agents reach other
boxes through an `agentd.remote` client surfaced as skills, so one agent can
read, start, drive or schedule sessions on another box.

It replaces the usual ad-hoc cross-box setup:

- **sshfs mounts** for reading other boxes' Claude Code transcripts. They work,
  but they're fragile and a dead box can hang reads.
- **ssh + tmux** for starting and driving sessions, which needs sshd, keys and
  users on every box.
- **Unsandboxed remote agents**: Claude Code running raw in tmux on other
  boxes, outside any agentd sandbox.

Apps (CLIs, assistants, other agents) become clients of `agentd serve`.

## Goals and non-goals

Hard requirement: **agentd is never reachable from the public internet**, not
even by mistake.

Goals:

- One API per box for its agent sessions: list, read, start, send a turn,
  stream events, cancel, schedule.
- Box-to-box access over p2claw with caller identity built in. No sshd, keys or
  user accounts.
- Agents on other boxes run inside agentd sandboxes, not raw in tmux.
- Any agent can use other boxes through skills, like local tools today.
- agentd stays usable without p2claw: p2claw is how you deploy it, not a
  dependency.
- Works the same on macOS and Linux: libkrun sandboxes (Hypervisor.framework
  or KVM, plus libkrun inside Colima on Macs) and host credentials on both.

Non-goals for now:

- Moving existing raw Claude Code sessions onto sandboxes right away. They stay
  readable (legacy endpoints) during the transition.
- Driving raw Claude Code sessions (tmux) at all: it would run outside any
  sandbox.
- Multi-user tenancy. Callers are your own boxes.
- A public or browser-facing UI for `agentd serve`.
- Human-in-the-loop controls for fleet skills (confirming what one agent may
  start or send on another box). Scoped out for now; to be designed separately.
- Live migration of running sandboxes (memory snapshots, connections held by a
  host-side proxy while a sandbox moves, as in Sail Research's Sailboxes).
  libkrun has no snapshots; it would take a Firecracker or Cloud Hypervisor
  backend (Linux, and Macs through Colima). Parked.

## Architecture

```
+------------------ box A ------------------+        +----------------- box B -----------------+
|  client app (CLI, assistant, agent)       |        |                                          |
|  fleet skills                             |        |   Public edge / browsers  --X  no route  |
|        | agentd.remote                    |        |                                          |
|        v                                  |  iroh  |   p2claw agent                           |
|  p2claw agent  /v1/proxy (local socket) --+------->|   private route "agentd", shares only    |
|                                           |        |        | adds X-P2claw-Peer               |
|  agentd serve  (serve.sock, local)        |        |        v                                 |
|  sandboxed sessions (microVM each)        |        |   agentd serve  (peers.sock; serve.sock  |
+-------------------------------------------+        |                  for local callers)      |
                                                     |        | starts, drives, schedules        |
                                                     |        v                                 |
                                                     |   sandboxed sessions (microVM each)      |
                                                     |   legacy ~/.claude/projects, read-only   |
                                                     +------------------------------------------+
```

Fleet skills call `agentd.remote`, which dials the remote box through the
local p2claw agent's `/v1/proxy` over iroh. The remote p2claw agent admits
only shared peers, adds `X-P2claw-Peer`, and hands the request to
`agentd serve` on its peers socket. The public edge has no route to it at all.

## Where each piece lives

Anything generic to running agents goes in agentd. Transport and identity stay
in p2claw. Application UX stays in the apps.

| Piece | Lives in | Why |
| --- | --- | --- |
| `agentd serve` (session API on Unix sockets) | agentd | Sandboxed sessions as a service suits any client: CLI, web UI, other agents. |
| Scheduled turns | agentd | Every client gets the same timed turns, running on the box that owns the session. |
| `agentd.remote` client + fleet skills | agentd | Any agentd app should be able to call other boxes' agents. |
| Caller identity (trusted header on the peers socket) | agentd, pluggable | A trusted-header hook keeps agentd free of p2claw code. |
| Private routes, shares, `/v1/proxy`, peer identity, agent SDKs | p2claw (shipped, 0.10.19) | Transport, hole punching and peer auth are p2claw's job. |
| Box-signed identity JWT verification | agentd adapter, later | When p2claw's identity attestation spec lands. |
| Voice, phone, conversation UX | the apps | Specific to each app. |

agentd stays p2claw-agnostic. It listens on two sockets:

- **`serve.sock`** for local callers (apps on the same box, the CLI). Its
  permissions (0600 in a 0700 directory) are the gate; no identity header.
- **`peers.sock`** for p2claw, registered as the private route. Every request
  must carry the caller identity header (configurable; `X-P2claw-Peer` on a
  p2claw box), which agentd uses for session ownership. A request without it
  is refused, so a misconfigured route can never pass as a local caller.
  agentd keeps no allowlist of its own: p2claw's shares decide who gets in.

## p2claw private routes (what we rely on)

From `p2claw/docs/private-routes.md`, `crates/box-agent` and
`libs/agent-client`, as of p2claw 0.10.19:

- `p2claw apps expose agentd --socket ~/.agentd/serve/peers.sock`: a
  Unix-socket upstream, which implies `--private` (p2claw only allows socket
  upstreams on private routes). Private routes have no public URL, are never
  announced to coordination, are invisible to the edge and DNS, and don't
  count against the app quota. Visibility is sticky: re-exposing without
  `--public` keeps a route private.
- Shares deny by default: `p2claw apps share agentd --with <peer>` (repeatable
  `--with`; `apps unshare`, `apps shares`). Anyone else gets a 404 shaped like
  an unknown app.
- Per request, p2claw checks the share, strips incoming `X-P2claw-*`, and
  injects `X-P2claw-Peer: <z32 peer id>`.
- Callers on another box go through their local agent's
  `/v1/proxy/<peer>/<app>/...` (HTTP, streaming and WebSockets), never the
  magic hostname, which would leak the private name to coordination. The
  `p2claw-agent-client` Python SDK wraps this: `AgentClient().fetch(peer,
  "agentd", path, stream=True)`.
- Not for agentd: `p2claw apps connect <peer>/agentd` serves the remote app on
  a local TCP port, which any local user could then use with this box's
  identity. `agentd.remote` talks to the agent's Unix socket instead.

## agentd serve API sketch

Plain HTTP over the Unix sockets, JSON in and out, server-sent events for
streams. Turns use the **OpenAI Responses API that agentd already implements**
(`harness=`, `session_id=`, `previous_response_id`, standard `response.*`
stream events, harness tool calls as `code_interpreter_call` items), so a
client can be the OpenAI SDK pointed at the socket, and local and remote turns
render the same way. agentd adds session and schedule endpoints for what the
Responses API lacks.

| Method + path | Does |
| --- | --- |
| `GET /v1/info` | Box name, agentd version, harnesses and models available, images. |
| `GET /v1/harnesses` | Per harness: ready (CLI in the image, a working model route) or why not, default model, models (`agentd.available`). |
| `GET /v1/models` | OpenAI-style model list: every model a ready harness can run, each naming those harnesses. |
| `POST /v1/responses` | Run a turn: `harness`, `model`, `input`, `instructions`, plus agentd's `session_id` / `workspace` (under an allowed root) / `image`. `stream=true` streams events. |
| `POST /v1/responses` with `background=true` | Run the turn server-side, detached from the request (see below). |
| `GET /v1/responses/{id}` | A response; with `stream=true&starting_after=N`, (re)attach to its events. |
| `POST /v1/responses/{id}/cancel` | Stop a running turn; agentd kills it in the sandbox. |
| `GET /v1/sessions` | Sessions: id, harness, model, workspace, owner, title (first prompt), last activity, sandbox state. |
| `GET /v1/sessions/{id}` | One session's metadata. |
| `GET /v1/sessions/{id}/transcript?cursor=&limit=` | Turns so far, paged, from the native transcript. |
| `DELETE /v1/sessions/{id}` | Close the session and stop its sandbox; the transcript stays. |
| `POST /v1/schedules` | Schedule turns (see below). |
| `GET /v1/schedules`, `GET /v1/schedules/{id}` | Schedules: target, input, timing, next run, recent runs (response ids). |
| `DELETE /v1/schedules/{id}` | Remove a schedule; a run in progress finishes (cancel it separately). |
| `GET /v1/legacy/claude-code` | Raw Claude Code sessions in `~/.claude/projects`, read-only. agentd's own sessions (copied there by transcript sync) are left out. |
| `GET /v1/legacy/claude-code/{id}?cursor=&limit=` | One legacy transcript, paged. |

**Paging.** Transcripts are JSONL and can be large, so transcript endpoints
return pages of turns with an opaque `cursor` (a position in the file) and a
`limit`; newest-first and oldest-first are both supported.

**Turns that outlive the connection.** By default a turn is tied to its
request, as in-process today: if the client goes away, the turn is cancelled
and killed in the sandbox. With `background=true` (the Responses API's own
background mode), agentd runs the turn as a server-side task and keeps its
events (in memory while running, plus the final response in the response
store). A client that drops off, e.g. over a flaky link, reattaches with
`GET /v1/responses/{id}?stream=true&starting_after=<last sequence number>`, or
polls for the result; only `cancel` stops it. One turn at a time per session:
a second one gets 409.

**Schedules.** A schedule runs turns at set times, each as a background turn:

- Target: an existing `session_id`, or a new session per run (`workspace`,
  `harness`, `model`, `image`).
- What to send: `input` and `instructions`, as in `POST /v1/responses`.
- When: `at` (one time, RFC 3339) or `every` (an interval or a cron
  expression, with a `timezone`).
- Each run is a normal response: its id is listed on the schedule, its events
  can be attached to and its result read like any other.
- If the target session is busy when a run is due, the run waits for the
  current turn; runs of the same schedule never overlap.
- Schedules persist across restarts (`~/.agentd/serve/schedules.json`). A run
  missed while agentd was down runs once at startup; earlier misses are
  skipped and recorded.
- A schedule belongs to the caller that created it and acts as that caller.

**Ownership.** Each session and schedule records the peer that created it
(`local` for `serve.sock`). Any peer that gets in can list and read sessions;
only the owner, or peers granted it in agentd's config, can run turns in a
session, cancel or close it, or schedule turns in it.

Left out of v1:

- **Arbitrary shell on the host.** Every command runs inside a session's sandbox.

## Sessions and sandboxes

A session (a conversation: harness, model, workspace, native session id,
owner) is durable; its sandbox is not. agentd already resumes a native session
by id in a fresh sandbox, from the transcript store, so `agentd serve`:

- keeps one sandbox per workspace while it is in use, and stops it after an
  idle timeout (each microVM holds about 2 GiB);
- starts it again on the session's next turn, resuming the native session.

What survives an idle stop: the workspace and the transcript. What doesn't:
background processes and files outside the workspace (the sandbox root is an
in-memory overlay).

### What we take from Agent Substrate

[Agent Substrate](https://github.com/agent-substrate/substrate) (Google,
Kubernetes) separates an agent's state ("actor") from the sandbox running it
("worker", from a warm pool). Suspend checkpoints the actor and frees the
worker; resume restores it on any free worker. Its microVM backend (Kata +
Cloud Hypervisor) snapshots guest memory and restores it with demand paging;
the rootfs writes live in a host-side overlay upper directory shared into the
VM over virtio-fs, so they survive the VM. Clients address actors, never VMs:
a router resumes a suspended actor on the first request (one resume for
concurrent requests, parked briefly if no worker is free, ready when a
wake-up probe answers). Idle detection is explicit `SuspendActor` calls today;
automatic idle suspend is on their roadmap.

For agentd:

- **Memory snapshots don't apply.** libkrun has no snapshot/restore (its
  unreleased pause API only stops vCPUs), so a stopped sandbox restarts cold:
  ~0.3 s native, ~1 s in Colima. Processes don't survive; the native session
  resumes from its transcript, which agentd already does.
- **Keep the overlay's upper layer on the host** (per session, e.g.
  `~/.agentd/sessions/<id>/upper`, shared over virtiofs) instead of tmpfs in
  the VM. Then packages and files a session installed outside its workspace
  survive an idle stop, and those writes stop counting against VM memory. To
  prototype: overlayfs needs an upper layer with xattr and whiteout support,
  which agentd's virtiofs shares may not give (Substrate builds the overlay on
  a Linux host; macOS has no overlayfs). Docker can do the same with a stopped,
  not removed, container.
- **Resume on the request path.** A turn for a session whose sandbox is
  stopped starts it (one start for concurrent requests) and then runs; the
  caller only sees a slower first turn.
- **Stop on an idle timer** per sandbox, plus an explicit stop
  (`DELETE /v1/sessions/{id}` closes; a lighter `POST .../suspend` could stop
  only the sandbox).
- **Not now:** warm pools of pre-booted VMs (boots are already ~0.3–1 s) and
  per-template "golden" snapshots (need memory snapshots).

## Security and exposure

agentd is reachable only through layers that each deny by default; any one of
them alone keeps it off the internet.

1. **Unix sockets only.** `agentd serve` never opens a TCP port, loopback
   included. The sockets live in a private directory (0700, owner only).
2. **Never inside a sandbox.** The socket directory is never shared with a
   sandbox, so an agent can't call `agentd serve` and escalate.
3. **Private route only.** p2claw accepts a Unix-socket upstream only on a
   private route, so a socket-only agentd can't be published, even by mistake.
4. **Shares deny by default.** Only peers granted with
   `p2claw apps share agentd --with <peer>` reach it.
5. **agentd requires a caller identity.** On `peers.sock` a request without
   `X-P2claw-Peer` is refused. (Who may connect at all is p2claw's shares;
   agentd keeps no second allowlist.)
6. **Sandboxes as the blast radius.** Callers can only start, drive and
   schedule sandboxed sessions, with workspaces confined to allowed roots and
   no network inside. No endpoint runs host commands.

| Must never | Guard |
| --- | --- |
| agentd answers on a TCP port | No TCP mode in the serve code; startup refuses host or port options. |
| agentd exposed as a public route | p2claw rejects Unix-socket upstreams on public routes; `--socket` implies `--private`. It holds structurally. |
| A request without a verified caller gets through | On `peers.sock`: no `X-P2claw-Peer` → 403, logged. `serve.sock` is for local callers only and is never registered with p2claw. |
| A sandbox reaches the socket | agentd refuses any share (workspace, extra or read-only mounts, any backend) that is, contains or sits inside `~/.agentd/serve`, `~/.agentd/run` (bridge and credential proxies), `~/.agentd/ca`, or the host's Codex/Claude login files (`protected_host_paths()` in `agentd/sandbox/base.py`; done). This matters most for Docker on Linux, where a bind-mounted Unix socket is connectable from the container. |
| The private name leaks to coordination | Callers dial through `/v1/proxy`, never the magic hostname. |
| A remote agentd is reachable from a local TCP port | `agentd.remote` uses the p2claw agent's Unix socket, never `p2claw apps connect`. |

## Client side: agentd.remote and fleet skills

Agents use other boxes through skills, like local tools. A host process makes
each call; it holds the p2claw socket that sandboxes can't reach.

- **`agentd.remote`** (library): `Fleet(boxes)` → `box.sessions()`,
  `box.transcript(id)`, `box.start(...)`, `box.send(id, prompt)` (streams
  events, or runs in the background), `box.cancel(id)`, `box.schedule(...)`.
  The transport is pluggable; the p2claw one uses `p2claw-agent-client`
  (`fetch` with `stream=True` on `/v1/proxy/<peer>/agentd/...`), an optional
  dependency. No keys or identity handling in the client.
- **Fleet skills** (generated like today's `@tool` skills, run host-side through
  agentd's MCP bridge):
  - `fleet_boxes()`: configured and reachable boxes.
  - `fleet_sessions(box)`, `fleet_read(box, session, query)`: list and read,
    including legacy Claude Code transcripts.
  - `fleet_start(box, workspace, prompt, harness)`: start a sandboxed session.
  - `fleet_send(box, session, prompt)`: send a turn and return the reply, or
    run it in the background and return its response id to check later.
  - `fleet_schedule(box, session, prompt, when)`, `fleet_unschedule(...)`.
  - `fleet_cancel(box, session)`.
- **Config**: box list of name → p2claw peer id or alias, plus which ones this
  agent may drive versus only read.

This also enables delegation: an agent can hand a coding task to an agent on
another box, inside that box's own sandbox, and pick up the result later.

## Migration

Each phase works on its own, so sshfs mounts can retire before remote boxes
move to sandboxes.

1. **`agentd serve`, read-only, everywhere.** Run it on every box with only
   `info`, `sessions` and the legacy endpoints. Expose `peers.sock` as a
   private route and share it with your peers. Fleet read skills replace
   sshfs mounts of other boxes' `~/.claude/projects`.
2. **Sandboxed sessions on remote boxes.** Turn on turns (including
   background turns), events and cancel. New work on remote boxes runs in
   agentd sessions; legacy endpoints stay read-only for history. ssh + tmux
   driving retires.
3. **Delegation and schedules.** Fleet skills that start and follow remote
   work, and the schedule endpoints.

## Decisions

- Private route + Unix socket for `agentd serve`. (Agreed 2026-10-01.)
- Client as tools/skills. (Agreed.)
- Existing raw sessions move to sandboxes later, not immediately.
- No startup "is my route private?" check: unnecessary, since Unix-socket
  upstreams can only be private routes and `agentd serve` has no TCP mode.
- Two sockets: `serve.sock` (local, permissions only) and `peers.sock` (p2claw,
  identity header required). (Agreed 2026-10-01.)
- No agentd allowlist for now: p2claw shares are the access list. (Agreed.)
- Turns use the Responses API, extended with session endpoints. (Agreed.)
- Turns are tied to their request by default; `background=true` runs them
  server-side with reattachable events. (Agreed 2026-10-01.)
- Sessions are durable; sandboxes stop when idle and resume by session id.
  (Agreed.)
- Session ownership: the starting peer owns a session; others read only
  unless granted. (Agreed.)
- macOS and Linux both supported, for sandboxes and host credentials.
  (Agreed.)
- Human-in-the-loop for fleet skills: out of scope for now. (Agreed.)
- Socket registration: `p2claw apps expose agentd --socket <peers.sock>`
  (p2claw 0.10.19). agentd doesn't call p2claw's API. (Done.)
- No tmux driving of raw Claude Code sessions. (Agreed 2026-10-01.)
- Scheduling lives in `agentd serve`, persisted across restarts, for every
  client. (Agreed 2026-10-01.)
- `agentd.remote` reaches other boxes through the p2claw agent's Unix socket
  (`p2claw-agent-client`), never `p2claw apps connect`. (Agreed 2026-10-01.)

## Open questions

None at the moment.

## Implementation

| Spec | Code |
| --- | --- |
| Sockets, identity, API, ownership | `agentd/serve/app.py` (`agentd serve`, `agentd/serve/cli.py`; TCP options are refused) |
| Settings | `agentd/serve/config.py` (`~/.agentd/serve/config.json`); grants to drive others' sessions: `drivers` |
| Turns, background, reattach, cancel | `agentd/serve/turns.py`, on `agentd.harness.responses.stream_response` |
| Sessions and final responses | `agentd/serve/store.py` (`sessions.json`, `responses/`) |
| Sandboxes per workspace, idle stop | `agentd/serve/pool.py` (`SandboxExecutor.stop_session()`) |
| Schedules | `agentd/serve/schedules.py` (intervals, 5-field cron with time zones, one-shot `at`) |
| Legacy Claude Code sessions, paging | `agentd/serve/legacy.py`, `agentd/serve/paging.py` (byte-offset cursors, both directions) |
| `agentd.remote`, fleet skills | `agentd/remote.py` (local and p2claw transports), `agentd/remote_skills.py` |
| No sandbox reaches agentd's sockets | `protected_host_paths()` in `agentd/sandbox/base.py` |
| macOS and Linux | `agentd-sandbox linux setup` (`agentd/sandbox/linux.py`): pinned libkrun/libkrunfw for x86_64 and aarch64, launcher per platform, rootfs extracted with real owners; Claude credentials from Keychain or `~/.claude/.credentials.json` |

Conventions the spec left open: session ids are `ses_...`; every response
carries `agentd.session` (agentd's session) next to `agentd.session_id` (the
harness's native id); a cancelled turn ends with `response.failed` and status
`cancelled`; `GET /v1/sessions?all=1` includes closed sessions; transcripts
take `format=messages` (the conversation agentd recorded) or `format=raw`
(the native JSONL).

Not built (as planned): the PTC harness through `agentd serve` (it needs the
caller's model API client), a separate `suspend` endpoint, the host-side
overlay upper layer (to prototype), warm pools, live migration.
