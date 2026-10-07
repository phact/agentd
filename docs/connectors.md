# Connectors: MCP tools for the user's services, OAuth through approvals

Status: agreed design (2026-10-06), not implemented. agentd is owned by its
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
secret, the default is **p2claw Connect** (`p2claw/docs/connect.md`): p2claw's
OAuth app, so the user sets nothing up; the user's own client is the
alternative.

## Design

### 1. Connectors

A connector is an MCP server agentd talks to **from the host** (like the
browser and phone devices), with its auth state and a tool policy. The owner
configures connectors; the agent can only ask to use a configured one.

```python
Connector(name="calendar",
          transport=Stdio(command=["agentd-mcp-google-calendar"]),   # or Http(url="https://…/mcp")
          auth=P2clawConnect(provider="google"),                     # or OwnClient(...), McpAuth()
          scopes=["calendar.app.created", "calendar.events.freebusy"],
          write_scopes=[],                                           # requested on first write, if any
          policy={...})                                              # overrides of the tool classification
```

- **Transports:** `Http` (a first-party hosted MCP server, streamable HTTP) or
  `Stdio` (a local server agentd launches and supervises).
- **Auth:**
  - `P2clawConnect(provider)`: p2claw's Connect app; flows through the box
    agent's `/v1/connect` API (p2claw does exchange and refresh with its
    secret, tokens sealed to the box).
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
- **Step-up:** scopes come in tiers; the first use of a tool needing a scope
  not yet granted raises a new `connector` approval for just that scope
  (`include_granted_scopes`), with the reason.
- **Re-consent** when a refresh fails or the server answers `insufficient_scope`.

### 3. Token store

- Refresh tokens (and MCP-server tokens) are kept in an encrypted store,
  `~/.agentd/connectors/tokens`, whose key is a fnox secret: locked in fnox
  means the first use after a vault change carries an `unlock` in its approval
  (the human's master password, as for the browser profile).
- Access tokens live in memory and are refreshed until a refresh fails.
- **Disconnect:** revoke at the provider (Google revokes the whole grant for
  the app, so every connector sharing it; the approver says so), then delete.
- One grant per provider and app, growing by scope; connectors declare the
  scopes they need.

### 4. Tokens for local servers

A local server gets **a short-lived access token, never the refresh token**:
an environment variable at launch, with agentd restarting the server when the
token nears expiry (about an hour), or, for servers that support it, a token
endpoint on a Unix socket agentd serves. Hosted servers get
`Authorization: Bearer …` on every request, and only that server's token
(no passthrough).

### 5. Tool policy

- Classify each tool from MCP tool annotations (`readOnlyHint`,
  `destructiveHint`), with owner overrides in the connector config.
- **Reads:** covered by the connector's grant for the session (one
  `connector_use` approval per session, or always).
- **Writes:** an approval per call that **shows the arguments** ("Create event
  'Committee meeting', Tue Oct 27 18:00–19:30 on Rosey's calendar"): once,
  session (that tool), always.
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

`agentd-mcp-google-calendar`, a small stdio server maintained with agentd,
calling the Calendar REST API with the access token agentd gives it. Scopes
are all **non-sensitive**, so p2claw Connect needs no Google verification:

- `calendar.app.created`: the agent's **own** calendar ("Rosey"), which it
  creates on first use and fully manages; the human overlays it in Google
  Calendar.
- `calendar.events.freebusy`: busy blocks (no titles or details) on the
  human's calendars and ones shared with them, by calendar ID or email.

| Tool | Kind |
|---|---|
| `calendar_ensure` (create the agent's calendar if missing) | write (once) |
| `events_list(time_min, time_max)`, `event_get(id)` | read (own calendar) |
| `freebusy(calendars, time_min, time_max)` | read (availability only) |
| `suggest_time(duration, window, calendars)` | read |
| `event_create(...)`, `event_update(id, ...)` | write |
| `event_delete(id)` | destructive |

Later, behind verification of the Connect app (or with the user's own client):
`calendar.events` / `calendar.readonly` for event details on the human's own
calendars, with a per-connector `calendars = [...]` allowlist enforced by the
server and shown in approvals. Strongest isolation, when the agent has its own
Google account: the human shares specific calendars with it, and Google
enforces it per calendar.

## Changes

agentd:

- `Connector` (Http, Stdio; P2clawConnect, OwnClient, McpAuth), MCP client on
  the host, tools bridged to every harness
- approvals: `connector` (consent, step-up) and `connector_use`; per-call write
  approvals with arguments
- the encrypted token store (fnox key, unlock through approvals), refresh,
  revoke
- local-server token handoff (env + restart, or a token socket)
- `agentd-mcp-google-calendar`
- docs: this file; `egress-and-secrets.md` (connectors section)

p2claw: the Connect broker and callback relay (`p2claw/docs/connect.md`).

Rosey: the phone's Connect button and argument-showing approval cards, a
Connectors list in settings, the prompt.

## Open questions

- MCP client: an existing Python MCP client library vs agentd's own (stdio and
  streamable HTTP).
- Which calendars `freebusy` covers by default: `primary` only, or a configured
  list (the human's partner's email, a family calendar).
- Read grants: a per-session approval, or free once connected.
