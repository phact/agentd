# Browser logins: profile clones per agent, gated long-lived sessions

Status: implemented (2026-10-04); see "Implementation" at the end. agentd is
owned by its maintainer; this doc records the agreed shape and why.

## Why

Logging the agent in to an airline's site (2026-10-03) failed many ways in a
row, and locked the account:

| what broke | cause |
|---|---|
| the saved login URL | the login page saved in the password manager is a 404 now |
| typing | the site's OneTrust cookie banner took focus a few seconds after load; part of the username or password went nowhere (fixed: refocus per character, length check, consent dismissal) |
| the standalone login page | the site's dedicated login page never posted the credentials; the homepage's "Log in" modal does |
| the sign-in itself | from the modal, PingFederate accepted the password (`POST …/pf-ws/authn/flows/<id>` → 200) and then rejected the next step (400): most likely its device check flagging a fresh, automated browser |
| retries | the agent and our probes kept trying; ~15 sign-in attempts in a day |

Lessons:

1. **The profile matters most.** A brand-new throwaway Chrome profile per
   session is the strongest bot signal there is; device checks trust a
   long-lived profile with history. (Pipe vs `chrome.debugger` makes no
   difference: both send the same CDP input and leave the same CDP traces. No
   existing agent extension avoids `chrome.debugger` (checked: Claude in Chrome
   1.0.98, real-browser-mcp, LiveMCP, browser-controller, agent360dk/browser-mcp,
   blueprint-mcp, mcp-chrome), and an extension would cost agentd its control of
   the browser.)
2. **Logging in rarely beats logging in well.** People stay signed in for
   weeks; a device that logs in and out several times a day is itself odd, and
   every login is a credential fill and a chance to trip a site's limits.
   Consumer agents (Instinct, Grok Bot, ChatGPT Dots) keep long-lived sessions
   for this reason, on cloud browsers behind residential proxies. We have the
   real home IP and hardware for free.
3. **Long-lived sessions are fine only if they're gated.** A signed-in profile
   that the agent can use without asking is the exposure of the user's real
   profile. agentd launches Chrome on the host and sees every request it makes,
   so it can gate each site's session per agent session.
4. **The tool shouldn't guess a site's shape, and the agent mustn't choose
   where a credential goes.** The agent navigates; agentd picks the fields the
   way a password manager does.
5. **Never retry a login automatically.** (Instinct got a user's account on a
   booking site banned by hammering it.)

## Design

### 1. A base profile, a clone per agent

- **Base profile:** `~/.agentd/browser/base`, persistent, never run directly.
  Chrome keeps nothing of value in it beyond cookies and site storage: password
  saving, autofill and sync off, no Google account.
- **One clone per agent session.** At lease start (`request_browser`), agentd
  clones the base into a temporary directory and starts a Chrome on the clone.
  On macOS the clone is an APFS copy-on-write (`clonefile`, `cp -c`): instant and
  free until written. Clone the user-data directory, not just the profile:
  `Local State` (field-trial state, so the browser looks the same every time) and,
  in the profile, `Cookies`, `Local Storage`, `IndexedDB`, `Preferences`, plus
  the state anti-fraud checks read: `Trust Tokens` (Private State Tokens),
  `TransportSecurity`, `Network Persistent State`. Not caches, history
  thumbnails, etc.
- **Each agent drives its own Chrome, with as many tabs as it needs.** Tab
  tools join the browser tools: open in a new tab, list tabs, switch, close a
  tab. N agents means N clone instances; nothing is shared between them while
  they run, so every gate below is simply per instance.
- **Merge back on close.** At `browser_close` or lease end, after any
  logouts (section 4):
  - cookies, under a lock, **per site the session used**: the base's cookies for
    that site's registrable domain are replaced by the clone's. Replacing, not
    upserting, is what carries deletions back: a site that logs out by deleting
    its session cookie, or a cleared site (section 4), would otherwise leave the
    old session in the base. A cookie's identity is Chrome's unique key,
    `(host_key, top_frame_site_key, has_cross_site_ancestor, name, path,
    source_scheme, source_port)` (verified on a current profile), not (domain,
    path, name), which would clobber partitioned cookies. When two clones touched
    the same site, keep each cookie's row with the newer `last_update_utc` rather
    than the clone that merged last, so an older clone closing later can't roll
    back a newer "remember this device" token. This keeps the reputation sessions
    earn (refreshed Akamai `_abck`, device tokens) and sessions approved to stay
    signed in.
  - storage: per-origin IndexedDB directories are replaced for sites the
    session used. `Local Storage` is one LevelDB for every origin, so replacing
    one origin means writing Chrome's LevelDB format; leave it out at first
    (cookies carry most sessions) and add it if a site needs it.
  - the clone is deleted.
- **Crash safety:** agentd keeps a journal of live clones (and of sites logged
  in during each). On start it finishes what a crash left: pending logouts,
  merge, delete.
- Cookie encryption: launch with `--use-mock-keychain` (macOS) /
  `--password-store=basic` (Linux), so the agent profile doesn't use your
  everyday Chrome's "Chrome Safe Storage" key (by default it would: same app,
  same Keychain item). The encrypted base (section 2) protects it at rest.
- Launch flags trimmed to what's needed; no fixed `--window-size` (a fixed size
  is a tell). Keep `--disable-blink-features=AutomationControlled`.

### 2. The base is encrypted at rest

Between leases the base's cookies and storage are unusable: the base lives in
an encrypted disk image (macOS sparse bundle via `hdiutil -stdinpass`;
gocryptfs or LUKS on Linux). (Chrome can't be pointed at a separate locked
keychain, so the disk image is the option.) agentd mounts the base just long
enough to clone it and to merge back. Clones exist only for the life of their
lease.

The image's password is a fnox secret (e.g. `AGENTD_BROWSER_KEY` in Enpass), so
unlocking the base is an ordinary fnox unlock: `request_browser` carries an
`unlock` when it's locked, the approver collects the master password (phone
fingerprint), and fnox's daemon keeps it until its idle timeout (e.g. `8h`).
One fingerprint per stretch of work, not one per lease.

### 3. Per-site session gate

A site whose session lives in the profile is reachable only with a grant for
this agent session:

- agentd intercepts every request the clone's Chrome makes (CDP `Fetch`, on
  the host; the agent never touches Chrome, it only calls agentd's tools),
  including new tabs, popups, out-of-process iframes and workers: Chrome holds
  each new target at start (`Target.setAutoAttach` with
  `waitForDebuggerOnStart`) until its requests are intercepted. (Done in 0.9.4:
  until then a `target=_blank` popup escaped the allowlist.) For a **gated
  site** (one with a stored session, i.e. any configured login, plus sites the
  human marks) it fails every request to the site unless the session holds a
  grant: approval `browser_site` ("Use your account on example.com", once / this
  session / always).
- **The gate covers the site's registrable domain** (eTLD+1, e.g. every
  `*.example.com` host), plus the login's identity-provider hosts, not just the
  hosts listed: session cookies are usually set for `.example.com` and go to every
  subdomain.
- Trade-off, on purpose: public pages of a gated site (searching fares without
  the account) also need the grant. Stripping cookies instead would miss apps
  that send a token from `localStorage` in their own header. So `browser_site`
  approvals should be one tap.
- Blocking whole hosts, not just stripping cookies, also covers apps that keep
  their token in `localStorage` and send it themselves: no request, no token.
  Pages of other sites can't call a gated site either.
- Service workers are bypassed on every tab (`Network.setBypassServiceWorker`);
  push and background sync are off in the profile, so no site code runs between
  tasks. WebSocket connections are checked against the gate too
  (`Network.webSocketCreated`).
- `request_login(site)` implies the site grant for the session.

### 4. Tiers: who stays signed in

- **Everyday sites: stay signed in**, behind sections 2 and 3. Most tasks then
  need no login at all.
- **Sensitive sites: log out at the end of every session.** Email, account
  roots, money, health (`tier = "sensitive"` on the login). Logout: open the
  login's `logout_url`; fallback: clear that site's data for its hosts in the
  clone (`Storage.clearDataForOrigin`: cookies, local storage, IndexedDB, cache,
  service workers) before merging back. A session that can reset every other
  password isn't worth keeping even behind a gate.
- **Idle expiry:** a site not used by any agent for 14 days is logged out (or
  its data cleared) on the next lease start.

### 5. When a login is needed: the agent navigates, agentd fills

`browser_login(site)` is replaced by:

- **The agent navigates** with the normal tools: opens the site (the login's
  `url` is just a suggested start page), clicks "Log in", gets past banners and
  modals, handles two-step forms.
- **`browser_fill_login(site)`:** the agent asks agentd to fill the login form
  on the current page **without pointing at a field** (letting it pick would
  let it put a credential anywhere on an approved host, say a forum post
  title). agentd picks the fields by structure:
  - **password:** the single visible `input[type=password]` on a page on the
    login's hosts. None or several (sign-up and change-password forms usually
    have two): refuse.
  - **username:** an input in the **same `<form>`** as that password box (or the
    same container if there's no form), preferring `autocomplete="username"` or
    `"email"`, then `type=email`, then a `user`/`email`/`login` name or id.
  - **two-step (username first):** fill the username only if the field says so
    explicitly (`autocomplete="username"`/`"email"` or `type=email`) and the page
    has no other text inputs.
  - **TOTP:** `autocomplete="one-time-code"`, or a lone short numeric input right
    after a login step.
  - anything ambiguous: refuse and say why (the human can log in by hand in the
    agent's window).
  - fill as today: dismiss consent banners, refocus before each character,
    check the length (never read the value back), retype once, else fail.
  - what the agent sees stays scrubbed: snapshots show non-password field
    values, so the username is scrubbed (it was read from fnox) and so is the
    typed TOTP code (derived from the seed, so agentd remembers it explicitly;
    done in 0.9.4).
- **The agent submits** by clicking the form's button, once.
- Approvals are unchanged: `request_login(site, reason)` (per site; fnox
  unlock with the master password when its secrets are locked), credentials
  typed only on the login's hosts, tool output scrubbed.

### 6. Attempt cap

At most **2 fills per site per 24 h**, counted across all agents (the lockout
risk is per account) and kept on disk, so restarting agentd doesn't reset it. After a fill agentd checks whether the next page still
shows a password box on the login's hosts (failed or a challenge); after two
such, `browser_fill_login` refuses for that site until the human approves a
retry (an approval saying what failed). Never retry automatically. One fill at
a time per site across clones.

### 7. Config

Logins (`Browser(logins=...)`, e.g. Rosey's `.rosey_logins.toml`):

```toml
["www.example.com"]
url = "https://www.example.com/"        # suggested start page (fields are never guessed from it)
hosts = ["www.example.com", "signin.example.com"]   # gated; credentials typed only here (default: url's site)
tier = "everyday"                       # or "sensitive": log out every session
logout_url = "https://www.example.com/logout"
username = "EXAMPLE_USERNAME"           # fnox secret names
password = "EXAMPLE_PASSWORD"
totp = "EXAMPLE_TOTP"                   # optional
```

## Concurrency notes

- Two clones signed in to the same site at once are two concurrent sessions on
  one "device"; most sites allow it, some (streaming, banks) end the older one.
- Merges are serialized; a clone made while another merges sees the base as of
  its last completed merge.
- Logging in to a site in one clone doesn't sign in the others until it merges
  back; the next lease picks it up.

## Changes

agentd:

- base profile + per-lease clone instances (copy-on-write), tab tools, merge
  back on close, the journal; keep the throwaway profile as an option (tests)
- encrypted base, unlocked through approvals (`request_browser` with `unlock`)
- per-site session gate (`browser_site` approvals; service workers bypassed;
  push/background sync off; WebSocket check)
- tiers: end-of-session logout for `sensitive` (logout URL, clear-data
  fallback), idle expiry
- `browser_fill_login` replacing `browser_login`; structural field selection
- the attempt cap
- docs: `egress-and-secrets.md` (browser section)

Rosey:

- phone: `browser_site` approvals; the base-profile unlock reuses the
  fingerprint flow
- prompt: ask for the site, use an existing session if there is one; else
  navigate to the login form, `browser_fill_login`, click submit; never retry a
  failed login
- logins: `tier` and `logout_url` (`enpass_map.py --draft` marks the
  money/health/account-root groups `sensitive`)

## Open questions

- Verifying a logout worked before trusting it over the clear-data fallback.
- Logins whose form is on another host (an identity provider such as
  `signin.example.com` or an Auth0 domain): `hosts` must list it.
- Sites whose device check still fails with a long-lived profile: hand those
  to the human.
- Merging storage for sites both a clone and the base changed (rare: last
  writer wins per origin).
- Google accounts: Device Bound Session Credentials tie a sign-in to keys held
  by that browser. Unverified whether a cloned profile keeps them working; test
  before relying on cloned Google sign-ins.

## Implementation

`agentd/devices/browser_profile.py` (base profile, clones, merge, journal,
encrypted image), `agentd/devices/browser_proxy.py` (policy proxy) and
`agentd/devices/browser.py`. Use: `Browser(profile=BaseProfile(key="AGENTD_BROWSER_KEY"), logins=..., gated=[...])`;
without `profile`, a throwaway profile as before.

Tools: `request_browser`, `request_login`, `request_site`, `browser_open`,
`browser_snapshot`, `browser_click`, `browser_type`, `browser_screenshot`,
`browser_fill_login` (replaces `browser_login`), `browser_tabs`,
`browser_new_tab`, `browser_switch_tab`, `browser_close_tab`, `browser_close`.

Found while building it, and how it's handled:

- **WebSockets bypass request interception** (CDP `Fetch` never sees them; so
  did the old allowlist). Gated sites' WebSockets are blocked with
  `Network.setBlockedURLs`; the newer allow/block `urlPatterns` didn't block
  WebSockets in Chrome 155. For everything else, including an allowlist's "all
  but these hosts", Chrome is pointed at a local **policy proxy**
  (`--proxy-server`, loopback included) that refuses `CONNECT` and absolute-URI
  requests to hosts the allowlist or the gate refuses, one request per
  connection. Interception stays in front (it raises approvals and gives clean
  errors).
- **A target held at start answers `Network.enable` only once released**, so
  per-tab setup is sent first and awaited after `Runtime.runIfWaitingForDebugger`.
- **Session cookies** (no expiry; many sites sign you in with them) are dropped
  by Chrome at startup unless it restores the last session: clones run with
  `--restore-last-session` and "continue where you left off"; they never carry
  saved tabs, so nothing reopens.
- A login approval grants its sites for the whole session, so a `once` spent by
  the password fill doesn't close the gate before the form posts; the TOTP step
  within 10 minutes of the password fill is part of the same login.
- Cookie values use `--use-mock-keychain` (macOS) / `--password-store=basic`
  (Linux); the encrypted image is the protection at rest.
- Grants: `browser_site` once and session both last the browser session;
  always is saved as `browser_sites` in `allow.toml`.
- The attempt cap counts unjudged fills against the site; a fill is judged
  after the next click (or Enter, or navigation): failed if the page is still on
  the login's hosts showing a password step.

Not done yet: merging `Local Storage`; verifying a logout worked (sensitive
sites are cleared from the base regardless); gocryptfs on Linux is implemented
but untested (macOS sparse bundle is tested).

Tests: `test/test_browser_profile.py` (merge on Chrome's real schema, recovery,
expiry, the encrypted image), `test/test_browser_proxy.py`, and live
`test/test_browser.py` (gate incl. WebSockets, fill by structure, two-step and
TOTP, refusals, attempt cap, banners, popups, tabs, sessions kept across
sessions and sensitive ones cleared).

