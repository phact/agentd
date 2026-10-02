# Egress and secrets: real network access without secrets in the sandbox

Status: implemented, 2026-10-02 (see "Implementation" at the end). Builds on the sandbox architecture (no
network in the sandbox; one host-dialed connection per sandbox) and on
[agentd serve](agentd-serve.md) for approvals.

## Summary

Agents need to call real services (GitHub, package registries, SaaS APIs),
drive a browser and a phone, and use secrets to do it. Two rules hold
throughout:

1. **Secrets never enter the sandbox.** The sandbox only ever holds
   placeholders.
2. **Nothing leaves the sandbox except through agentd's host-side policy.**

For libkrun sandboxes, a new Rust component (`agentd-net`) gives the microVM
a virtual network card whose packets go to a user-space TCP/IP stack on the
host, so every connection from any program reaches agentd's egress proxy. The
proxy allows, refuses, or rewrites each connection by policy, and for
rule-matched HTTPS swaps placeholders for real secrets from
[fnox](https://fnox.jdx.dev). Anything that can't work as an HTTP header
(databases, SSH, signing, phones, browsers with logins) runs as a host-side
tool: the agent gets the capability, never the credential.

**Docker sandboxes keep no egress at all** (`--network none`); a comparable
design for Docker (iptables-based) comes later.

## Why not fnox's own MCP server or proxy

- `fnox mcp` offers `get_secret` (returns the value) and `exec` (runs an
  agent-chosen command with secrets in its environment, on whatever host it
  runs on). Exposed through agentd's bridge, the first copies secrets into the
  sandbox and the second runs agent commands on the host. Its output
  redaction is literal-match only: `echo $SECRET | base64` gets through. fnox
  says itself that neither tool is a sandbox.
- `fnox proxy run` uses the right model (placeholders, rule-matched header
  injection, response scrubbing), but it buffers whole requests and
  responses (10 MiB cap; streaming and chunked bodies break), handles headers
  over HTTP/1.1 on port 443 only, and is tied to a child process's lifetime.
- So **fnox supplies secrets and rules; agentd enforces them**, in its own
  streaming proxy.

## Architecture

```
 microVM (libkrun)                     host (agentd, unprivileged)
 ┌──────────────────────┐              ┌──────────────────────────────────────────────┐
 │ any program          │              │ agentd-net (Rust, one per sandbox)            │
 │  → connect()/DNS     │  Ethernet    │  user-space TCP/IP stack; DNS on the gateway  │
 │ guest kernel         │──frames────► │  every TCP connection → stream + {dst, host}  │
 │  eth0 (virtio-net)   │  Unix socket │                    │                          │
 │                      │              │                    ▼                          │
 │ env: placeholders,   │              │ egress proxy (Python)                         │
 │ agentd CA (public)   │              │  1. rule-matched HTTPS → TLS (session CA),     │
 └──────────────────────┘              │     inject secret, verified TLS upstream,     │
                                       │     scrub responses (streaming)               │
                                       │  2. allowed host:port → bytes passed through  │
                                       │  3. else → approval flow or refusal           │
                                       │  secrets + rules ← fnox; grants ← approvals    │
                                       │  audit log                                    │
                                       └──────────────────────────────────────────────┘
 host-side tools (behind the MCP bridge): fnox-backed MCP servers and @tools, adb, browser
```

The existing channels stay as they are: model proxies (sandbox-side
endpoints), the MCP bridge, and sandboxd's mux.

## Components

### agentd-net (Rust)

- Launcher: `krun_add_net_unixstream` (Linux) or the vfkit frame format
  (macOS) on a per-sandbox Unix socket. The guest gets an ordinary `eth0` (a
  standard virtio-net driver, no privileges, nothing installed in the
  guest); addressing via libkrun's DHCP client flag or a static setup by
  sandboxd.
- A user-space stack (smoltcp, or the `ipstack` crate with a thin
  Ethernet/ARP layer) completes every TCP connection the guest opens.
- DNS is answered on the gateway address, so every connection is known by
  hostname, not just IP. Other UDP and ICMP are dropped (QUIC falls back to
  TCP).
- Each connection is handed to agentd over a Unix socket with a small
  header: destination address, port, hostname. Policy lives entirely in
  agentd; Rust only turns packets into connections. Memory-safe, since it
  parses agent-generated packets.
- Lifecycle tied to the launcher (exits with its parent).
- Colima: runs inside the VM next to the launcher; its connections need a
  path to the host, so the relay becomes a multiplexer (today it is a byte
  pipe for the mux).
- Builds for macOS arm64 and Linux x86_64/aarch64, wired into `build.sh`,
  `agentd-sandbox linux setup` and `agentd-sandbox colima setup` (rustup is
  already part of the Linux setups), or shipped prebuilt.

### Egress proxy (Python, host)

One decision per connection:

1. **Rule-matched HTTPS**: terminate TLS with a leaf certificate from a
   **per-session CA held in memory only** (its public certificate is
   installed in the sandbox: system bundle, `SSL_CERT_FILE`,
   `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO`, ...).
   Inject the real value into the matched header, forward over verified TLS,
   and scrub real values from response headers and bodies.
   - Fully streaming: no size limits, chunked and server-sent-event bodies
     work; scrubbing uses a sliding window (secret length minus one byte of
     overlap).
   - HTTP/1.1 and HTTP/2 (TLS protocol negotiation on both sides; `h2` toward the
     sandbox, `httpx`/h2 upstream), so gRPC works.
2. **Allowed without secrets** (host, port): bytes passed through untouched,
   so the server sees the client's own TLS handshake (matters for sites that
   fingerprint TLS). Includes raw TCP (ssh, postgres, ...) by host and port.
3. **Everything else**: the approval flow, or refused (TCP reset; for HTTPS
   that the proxy terminates, a 403 explaining what's missing).

Secrets come from fnox on the host (`fnox get`, or its daemon), cached in
memory. Rules come from fnox's `[proxy.rules]` (domain, header, methods,
path globs, placeholder), plus agentd's grants. The sandbox gets one
placeholder per secret (format-preserving when a rule gives one).

Audit log (JSONL): time, sandbox, method, host, path, secrets injected,
decision. Never header values or bodies.

### Approvals and grants

- `request_access(what, reason, duration)` skill: the agent asks while
  planning, so most approvals happen before the request that needs them.
- `list_secrets()` skill: what the agent can use or ask for. Every secret
  in the workspace's fnox config (or the `Egress(secrets=[...])` subset)
  with its fnox `description`, the env var holding its placeholder, and the
  rules that let it be sent now; never values or providers (names come from
  the config files, not `fnox list`, which prints provider keys). Secrets
  without a rule get an opaque placeholder at start and are read from fnox
  only when a rule or approval first lets them be sent.
- A request not covered by a rule or grant is **held** for up to ~20-30 s
  (under most clients' timeouts) while the approver is notified. Approved
  in time: it proceeds. Otherwise the proxy answers 403 with
  `{"error": {"type": "agentd_egress", "approval": {"id": ..., "status":
  "pending", "retry_after": 30}, ...}}` (`"status": "deny"` and no
  `retry_after` when the human refused). Nothing went upstream, so retrying
  is safe; the agent is the retry loop. A held request is only forwarded if
  its client is still connected, so clients need a timeout of at least the
  hold + 15 s; `request_access`'s description tells the agent the hold, that
  timeout and what the 403 means.
- **Approver: a webhook.** agentd POSTs a signed (HMAC) approval request to
  the configured URL(s); the decision comes back through `agentd serve`
  (`POST /v1/approvals/{id}`), so a phone app or another box can decide.
- Grants: once, for the session, or always (writes a narrow rule); scoped to
  the workspace; optionally time-limited (a lease).
  "Once" applies to the held request, or when none is held (asked ahead,
  or the hold ran out) to the next matching request: a single-use rule.
- Named rule presets ("github: read-only", "github: this repo, write") so
  rules are picked, not written.

The human-in-the-loop design across all of this (what needs asking, how
often, presets, leases, where approvals happen) is open: the components
above are hooks for it, not its final shape.

### Host-side tools with secrets

- MCP servers behind the bridge start as `fnox exec -- <server>` with a
  per-server secret list; keys live only in that host process.
- `@tool` functions get a host-only secret accessor backed by fnox; their
  output is scrubbed before it returns to the sandbox.

### Android (adb)

Host-side tools over `adb` (USB, Wi-Fi or an emulator): screenshot (written
to the workspace as an image the harness can view), UI hierarchy dump, tap
and tap-by-text, type, key, swipe, open app, list apps. Access is a lease
("drive this phone for 30 minutes") through `request_access`, with a device
allowlist and an optional app allowlist enforced by the open-app tool. Taps
are opaque, so the real boundaries are the lease, the apps, what the phone is
logged into (a dedicated backup phone), and you watching (scrcpy). iOS
(XCUITest / WebDriverAgent, needs a Mac with Xcode) later.

### Browser

- A real, headed Chrome on the host with a fresh profile per task, driven by
  host-side tools over the Chrome DevTools Protocol (navigate, page snapshot,
  click, type, screenshot). An extension is the fallback if sites still
  block it.
- Spike (2026-10-02, Chrome 155, macOS): with remote debugging on, Chrome
  sets `navigator.webdriver = true`, which detectors flag;
  `--disable-blink-features=AutomationControlled` removes it. Never calling
  `Runtime.enable` and reading pages from an isolated world leaves no other
  trace: rebrowser's bot-detector clean, bot.sannysoft.com 0 failures,
  Cloudflare's nowsecure.nl passed. Calling `Runtime.enable` broke the
  rebrowser page. Commercial anti-bot services also score behavior, so input
  goes through CDP (trusted events) with human-like timing.
- Launch Chrome with `open -na "Google Chrome" --args ...` (macOS), not as a
  child process: otherwise macOS attributes Chrome's file access to agentd's
  own process (the spike triggered a Documents-folder privacy prompt that
  way). Use `--remote-debugging-pipe` rather than a port in the real tool.
- Logins: you type them in that window, or (approved) a host-side tool fills
  credentials from fnox, generates TOTP codes from a seed in fnox, or
  injects saved session cookies. The agent never sees passwords or cookies.
- Sessions are leases: the profile is wiped when the task ends or the lease
  expires.
- The browser's traffic goes through the same egress proxy; sites without
  secrets are passed through untouched, so Chrome's own TLS handshake reaches
  them.

## Phases

0. **Spikes**: libkrun virtio-net to a toy Rust stack on macOS and Linux;
   fnox's config, rules output and non-interactive `get`.
1. **agentd-net** (Rust) and the libkrun wiring, native and Colima.
2. **Egress proxy**: pass-through, injection, scrubbing, HTTP/1.1 + HTTP/2,
   CA trust in the sandbox, audit.
3. **Approvals**: `request_access`, hold-then-403, webhook, grants, presets.
4. **Host-side tools with secrets**: fnox-backed MCP servers and `@tool`s.
5. **Android** tools and leases.
6. **Browser** tools, logins and leases.

## Testing

In a sandbox on native libkrun and Colima: `curl`, `git`, Python, Node,
Bun, and a statically linked Go binary (ignores `HTTPS_PROXY`) through
agentd-net; DNS; pass-through vs injection; refusals; scrubbing (including
transformed values, to document the limits); large, chunked and streaming
bodies; HTTP/2 and gRPC; raw TCP allow/deny; approval hold, timeout and
retry; webhook signing; a test secret from an age-encrypted fnox file. Docker:
egress still fully blocked.

## Decisions

- No egress support for Docker; it stays `--network none`. An iptables-based
  design comes later. (2026-10-02)
- The proxy is agentd's own; fnox supplies secrets and rules. (2026-10-02)
- agentd-net is written in Rust; the network card's packets go to the host,
  nothing is added inside the guest. (2026-10-02)
- Per-session CA, in memory only. (2026-10-02)
- Approvals via webhook. (2026-10-02)
- Raw TCP egress is in scope. (2026-10-02)
- HTTP/2 from the start. (2026-10-02)

- "Always" grants are written to fnox's config; revisit if we outgrow it.
  (2026-10-02)
- Approvals: a webhook announces each new request; the approver app calls
  `agentd serve` endpoints to approve, reject, or approve always.
  (2026-10-02)
- The guest gets a static address on a small private subnet (no DHCP server
  in agentd-net). (2026-10-02)
- agentd-net always runs on the host (macOS or Linux). In Colima, a second
  `colima ssh` pipe per sandbox carries the network card's frames from the
  VM (libkrun's stream mode is length-prefixed, so it fits a pipe); the
  relay gets a mode that pumps that socket. (2026-10-02)

## Open questions

- None at the moment.

## Implementation

| Spec | Code |
| --- | --- |
| agentd-net | `agentd/sandbox/net` (Rust: smoltcp, fake-IP DNS, one stream per connection after a one-byte accept); launcher `--net-sock`; `sandboxd --configure-net` (ioctls: no `ip` needed); Colima: `colima_relay.py --net-pump` over a second `colima ssh` |
| Egress proxy | `agentd/egress/` (`policy`, `ca`, `proxy` with h11, `http2` with h2); `KrunExecutor(egress=Egress(...))`, `SandboxSession` |
| Approvals | `agentd/egress/approvals.py`; `agentd serve` `/v1/approvals`; `request_access` skill |
| Host-side secrets | `agentd/secrets.py`; bridge results scrubbed (`agentd/mcp_bridge.py`) |
| Android | `agentd/devices/android.py` |
| Browser | `agentd/devices/browser.py` |
| Setup | `build.sh` builds agentd-net; Colima and Linux setups build libkrun with `NET=1` |

Differences from the plan above:

- The browser's requests are policed with Chrome's own request interception
  (CDP `Fetch`) using the same allowlist and approvals, not by routing it
  through the egress proxy: a proxy Chrome could use would be a localhost TCP
  port any local process could use to get secrets injected. Browser logins
  use form filling from fnox instead of injected headers.
- The proxy offers the sandbox exactly the protocols the real server speaks
  (it connects upstream first), so HTTP/2 is bridged end to end and
  HTTP/1.1 clients get HTTP/1.1 upstream.
- Blocked HTTPS/HTTP connections get a 403 explaining why (TLS terminated
  with the session CA just for that), not a bare reset.
- `request_access` grants apply to the most recently started session (the
  bridge doesn't know which sandbox called).
- Verified: native libkrun (macOS), libkrun in Colima, native Linux libkrun
  (in the Colima VM as a Linux host); Docker stays without network.

