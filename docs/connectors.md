# Connectors: MCP tools for the user's services, OAuth through approvals

Status: the native Google Calendar connector over p2claw Connect is
implemented (2026-10-07; see "Implementation" at the end); MCP-server
transports, `OwnClient` and `McpAuth` are not yet. agentd is owned by its
maintainer; this doc records the agreed shape and why.

## Why

Agents need the user's own services (calendar first). Options weighed:

- **Vendor connectors** (Claude's, Grok's): the vendor's OAuth app, the vendor
  holds the tokens, one harness only, writes bypass our approvals. Out.
- **Hosted MCP aggregators** (Composio, Pipedream, Zapier): a third party holds
  the tokens. Out.
- **First-party hosted MCP servers** (the service's own: Google for Calendar):
  in, when they're open. Google's Calendar MCP server is preview-gated, so not
  for Calendar today.
- **Local MCP servers** that agentd runs on the host: in.

Either way **agentd does the OAuth dance** and the human consents through the
approvals flow (on the phone, for Rosey). Where the provider needs a client
secret, the default is **p2claw Connect** (`p2claw/docs/connect.md`; live in
the box agent since 0.10.22): p2claw's OAuth app, so the user sets nothing up;
the user's own client is the alternative. agentd stays mostly p2claw-agnostic:
Connect is one `auth` option among three.

## Design

### 1. Connectors

A connector is a set of tools for one of the user's services, with its auth
state and a tool policy. The owner configures connectors; the agent can only
ask to use a configured one. Its tools are either **native** (Python in
agentd's process, for connectors maintained with agentd, like the browser and
phone tools) or an **MCP server** agentd talks to.

```python
Connector(name="calendar",
          transport=Native("agentd.connectors.google_calendar"),   # or Http(url=…), Stdio(command=[…])
          auth=P2clawConnect(provider="google"),                     # or OwnClient(...), McpAuth()
          scopes=["calendar.app.created", "calendar.events.freebusy"],
          write_scopes=[],                                           # requested on first write, if any
          policy={...})                                              # overrides of the tool classification
```

- **Transports:** `Native` (agentd's own tools: the access token never leaves
  agentd's process, so nothing to hand off), `Http` (a first-party hosted MCP
  server, streamable HTTP) or `Stdio` (someone else's local server).
- **Someone else's local server runs in an agentd sandbox, not on the host**
  (it's third-party code; on the host it would have the user's full access).
  Its egress allows only its API's hosts, and its token goes in the way
  secrets already do: the server holds a placeholder and the egress proxy
  swaps in the current access token on the host, for that API's domain only
  (refreshed transparently, scrubbed from responses).
- **MCP client:** the `mcp` SDK agentd already depends on (1.25: stdio and
  streamable-HTTP clients, and `OAuthClientProvider`, which implements the MCP
  authorization spec below: RFC 9728 discovery, client ID metadata documents,
  PKCE), with agentd's token storage and an approval as its redirect.
- **Auth:**
  - `P2clawConnect(provider)`: p2claw's Connect app, through the box agent's
    local API (`/tmp/p2claw-<uid>/agent.sock`; CLI `p2claw oauth-grants`):
    `GET /v1/oauth-grants/providers`; `POST /v1/oauth-grants/flows`
    `{provider, scopes, code_challenge, nonce_hash}` → `{flow_id,
    authorize_url}`; `GET /v1/oauth-grants/flows/{id}?wait=1` (long-poll, up
    to 55 s) → `{status, code, state}`; `POST
    /v1/oauth-grants/flows/{id}/exchange` `{code_verifier, store: true}` →
    access token and a stored grant id; `GET /v1/oauth-grants/{id}/token`
    (refreshed as needed); `DELETE /v1/oauth-grants/{id}` (revoke and
    forget). agentd makes the PKCE verifier and nonce. Providers today:
    `google`, with `calendar.app.created` and `calendar.events.freebusy`
    (non-sensitive, so no Google verification).
  - `OwnClient(client_id, client_secret, authorize_url, token_url)` from fnox,
    redirect to a callback the host serves.
  - `McpAuth()`: the MCP authorization spec for hosted servers that support
    it: protected-resource metadata (RFC 9728) → authorization-server metadata
    (RFC 8414) → client ID metadata document or dynamic registration →
    authorization code + PKCE, with `resource` (RFC 8707) so the token is valid
    only for that server.
- Tools reach **every harness** through the bridge, like agentd's other host
  skills. Tokens never enter a sandbox.

### 2. Consent through approvals

- `request_connector(name, reason)` raises an approval of kind `connector`:
  `{name, server, provider, scopes, authorize_url}`. The approver (Rosey's
  phone) shows "Connect **Calendar** (manage its own calendar, see when you're
  busy)" with an **Open** button; the human consents in the browser; the
  approval resolves when the callback completes (the tool waits up to the
  hold, like the other request tools).
- The callback must be reachable from wherever the human consents: Connect
  relays it to the box; an `OwnClient` with a loopback redirect only works
  when the human consents on the host itself (from the phone it needs a relay
  too).
- **Step-up:** scopes come in tiers; the first use of a tool needing a scope
  not yet granted raises a new `connector` approval for just that scope
  (`include_granted_scopes`), with the reason.
- **Re-consent** when a refresh fails or the server answers `insufficient_scope`.

### 3. Token store

- **Connect grants are kept by the p2claw agent** (`exchange` with `store`):
  sealed to the box by the broker, in the agent's `oauth-grants.json` (0600)
  in its data directory. agentd records only grant ids and asks for a current
  access token when it needs one. agentd refuses sandbox shares that would
  expose that directory or the agent's socket (`protected_host_paths`).
- Other refresh tokens (`OwnClient`, `McpAuth`) are kept in an encrypted
  store, `~/.agentd/connectors/tokens`, under the same fnox key as the browser
  base profile, so one unlock covers both: locked in fnox means the first use
  after a vault change carries an `unlock` in its approval (the human's master
  password).
- Access tokens live in memory and are refreshed until a refresh fails.
- **Disconnect:** revoke at the provider (Google revokes the whole grant for
  the app, so every connector sharing it; the approver says so), then delete.
- One grant per provider and app, growing by scope; connectors declare the
  scopes they need.

### 4. Tokens for local servers

Native tools use the access token in agentd's process. Someone else's local
server (in a sandbox) never holds a token: it holds a placeholder that the
egress proxy swaps for the current access token on the host, only on
requests to the API's domain (section 1). Hosted servers get
`Authorization: Bearer …` on every request, and only that server's token (no
passthrough).

### 5. Tool policy

- Classify each tool from MCP tool annotations (`readOnlyHint`,
  `destructiveHint`), with owner overrides in the connector config. The MCP
  spec says annotations are **untrusted** unless the server is: a tool
  without them counts as a write, and for third-party servers the owner's
  policy is what counts.
- **Pin the tool set at consent:** each tool's name, description and input
  schema are recorded when the connector is approved; a tool that appears or
  changes later (`tools/list_changed`) needs a new approval before the agent
  can use it.
- **Reads:** free once connected (the consent covers them).
- **Writes:** an approval per call that **shows the arguments** as labelled
  fields, verbatim (title, start, end, calendar), not prose built from them:
  the agent writes the arguments, and a title can be written to mislead the
  approver ("… (you approved this yesterday)"). Once, session (that tool),
  always.
- **Destructive** (delete, cancel): an approval per call, never always.
- **Results are untrusted data** (an event someone else wrote can carry a
  prompt injection); tool output says so.
- Audit log: connector, tool, an argument summary, decision; never tokens.

### 6. Security rules

- Owner-configured connectors only; the agent can't add a connector or point
  agentd at a new URL.
- HTTPS only and no private addresses during discovery (SSRF); the
  authorization server's `issuer` must match its metadata.
- PKCE S256 and a one-time `state` on every flow; tokens sent only to the
  server they were issued for.

## First connector: Google Calendar (local)

A **native** connector (`agentd.connectors.google_calendar`): tools in
agentd's process calling the Calendar REST API with the access token from the
Connect grant. Scopes are both **non-sensitive** (the reason they were
chosen), so p2claw Connect needs no Google verification:

- `calendar.app.created`: the agent's **own** calendar ("Rosey"), which it
  creates on first use and fully manages; the human overlays it in Google
  Calendar.
- `calendar.events.freebusy`: busy blocks (no titles or details) on the
  human's calendars and ones shared with them, by calendar ID or email (for
  someone else's calendar, only if they share at least free/busy with the
  human, or in the same Workspace domain).

| Tool | Kind |
|---|---|
| `events_list(time_min, time_max)`, `event_get(id)` | read (own calendar) |
| `freebusy(calendars, time_min, time_max)` | read (availability only) |
| `suggest_time(duration, window, calendars)` | read |
| `event_create(...)` (creates the agent's calendar on first use), `event_update(id, ...)` | write |
| `event_delete(id)` | destructive |

Later, behind verification of the Connect app (or with the user's own client):
`calendar.events` / `calendar.readonly` for event details on the human's own
calendars, with a per-connector `calendars = [...]` allowlist enforced by the
server and shown in approvals. Strongest isolation, when the agent has its own
Google account: the human shares specific calendars with it, and Google
enforces it per calendar.

## Changes

agentd:

- `Connector` (Native, Http, Stdio in a sandbox; P2clawConnect, OwnClient,
  McpAuth), the `mcp` SDK as MCP client, tools bridged to every harness
- approvals: `connector` (consent, step-up) and `connector_use`; per-call write
  approvals with arguments
- Connect grants kept by the p2claw agent; the encrypted token store for the
  others (the browser profile's fnox key, unlock through approvals); refresh,
  revoke
- third-party local servers in a sandbox, tokens injected by egress
- tool pinning; per-call write approvals with labelled arguments
- `agentd.connectors.google_calendar` (native)
- tests: a fake OAuth server and a fake MCP server (consent, step-up,
  refresh failure, `insufficient_scope`, a changed tool); a fake p2claw local
  API; a live Calendar test with a test Google account
- docs: this file; `egress-and-secrets.md` (connectors section)

p2claw: the Connect broker and callback relay (`p2claw/docs/connect.md`).

Rosey: the phone's Connect button and argument-showing approval cards, a
Connectors list in settings, the prompt.

## Decided (2026-10-07)

- `freebusy` and `suggest_time` check `primary` by default, plus whatever the
  owner configures (`freebusy_calendars`); the agent can name others per call.
- Reads are free once connected; there's no `connector_use` approval.
- No separate `calendar_ensure` tool: as a write it would need an approval per
  call even once the calendar exists; `event_create` creates it under its own
  approval.

## Implementation

`agentd/connectors/__init__.py` (`Connector`, `Connectors`, `Native`,
`P2clawConnect`, `enable_connector_skills`) and
`agentd/connectors/google_calendar.py`.

```python
from agentd.connectors import Connector, Connectors, Native, P2clawConnect, enable_connector_skills

calendar = Connector(name="calendar", description="manage its own calendar, see when you're busy",
                     transport=Native("agentd.connectors.google_calendar"), auth=P2clawConnect("google"),
                     scopes=["calendar.app.created", "calendar.events.freebusy"],
                     options={"calendar_name": "Rosey", "freebusy_calendars": ["primary"]})
enable_connector_skills(Connectors([calendar], approvals=approvals))
```

- Skills: `request_connector(name, reason)`, `connectors_status()`, and each
  tool as `<connector>_<tool>` (`calendar_event_create`, …), for every harness
  through the bridge.
- `P2clawConnect` uses `p2claw-agent-client` (imported only when used, as
  `agentd.remote` does): `oauth_grants_connect(store=True)` makes the PKCE
  verifier and nonce and checks the callback's nonce; agentd keeps the grant
  id in `~/.agentd/connectors/grants.json` and asks the p2claw agent for an
  access token (cached in memory until a minute before it expires).
- Consent: an approval of kind `connector` with `authorize_url`; agentd waits
  for the callback in the background (up to 10 minutes) and decides the
  approval itself (`always`, by `consent`) once the grant is stored. Tapping
  approve without consenting changes nothing; asking again returns the same
  link while the flow runs.
- Writes: approvals of kind `connector_write` (`connector_destructive` for
  deletes) with `arguments: [{name, value}]` verbatim. When the hold runs out,
  the tool answers `pending` with the approval id, and calling again with the
  same arguments uses that approval. `once` is spent by one call; `session`
  covers the tool until agentd restarts; `always` is saved in
  `~/.agentd/connectors/allowed.json` (never for destructive tools).
- Read results carry `untrusted` (a note to treat them as data). Every call is
  appended to `~/.agentd/connectors/audit.jsonl` (connector, tool, kind,
  arguments truncated, decision); never tokens.
- `410 invalid_grant` from the p2claw agent forgets the grant and tells the
  agent to `request_connector` again. `Connectors.disconnect(name)` revokes
  and forgets (an owner action, not a skill).
- Tests: `test/test_connectors.py` (a fake Calendar API and a stand-in for
  Connect; the real `p2claw-agent-client` against a fake agent socket, from a
  p2claw checkout; and, with `AGENTD_CONNECT_LIVE=1` and `-s`, real Google
  through the running p2claw agent: open the printed link to consent).

Not yet: `Http` and `Stdio` (sandboxed) MCP-server transports, tool pinning
(nothing to pin for native tools), `OwnClient` and `McpAuth` with the encrypted
token store, step-up to more scopes, and wiring connectors into `agentd serve`'s
config.
