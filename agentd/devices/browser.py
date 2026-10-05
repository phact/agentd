"""A real Chrome as agent tools, on the host.

    from agentd.devices.browser import Browser, enable_browser_skills
    from agentd.devices.browser_profile import BaseProfile

    browser = Browser(workspace="/path/to/ws", approvals=approvals,
                      profile=BaseProfile(key="AGENTD_BROWSER_KEY"),   # None: a throwaway profile
                      logins={"example.com": {"url": "https://www.example.com/", "tier": "everyday",
                                              "username": "EXAMPLE_USER", "password": "EXAMPLE_PASSWORD",
                                              "totp": "EXAMPLE_TOTP"}})
    enable_browser_skills(browser)

* Headed Google Chrome driven over the DevTools Protocol through a pipe (no
  debugging port another local process could use). No ``navigator.webdriver``
  (``--disable-blink-features=AutomationControlled``), no ``Runtime.enable``,
  pages read from an isolated world; clicks and typing are real input events.
* Access is a lease (``request_browser``). With a ``profile``, each lease gets
  its own Chrome on a clone of a long-lived base profile, merged back when it
  ends (agentd.devices.browser_profile); without one, a throwaway profile.
* Sites with a stored session (every configured login, plus ``gated``) are
  reachable only with a grant for this session (``request_site``); the gate
  covers each site's registrable domain, every tab, popup, iframe and worker,
  WebSockets, and (through a local proxy Chrome is pointed at) anything else.
* Each site's login needs its own approval (``request_login``). The agent
  navigates to the form; ``browser_fill_login(site)`` fills it from fnox,
  picking the fields itself (the agent never points at one) and only on the
  login's hosts; the agent clicks submit. At most two failed fills per site per
  24 h. ``tier = "sensitive"`` logins are logged out when the session ends.
* An optional ``allow`` (host patterns) limits where the browser may go at all.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import fnmatch
import hashlib
import hmac
import itertools
import json
import logging
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from agentd import fnox
from agentd import secrets as host_secrets
from agentd.devices.browser_profile import ROOT, BaseProfile, Clone, site_of
from agentd.devices.browser_proxy import PolicyProxy

logger = logging.getLogger(__name__)

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
)
# AutomationControlled keeps navigator.webdriver false (it's true when driven over the
# pipe), at the cost of Chrome's "unsupported command-line flag" bar. Don't add
# --test-type to hide the bar: it also stops Chrome's component extensions, so the
# "Google ..." speech voices vanish from speechSynthesis.getVoices() (any site can see
# that) and Google's own pages can't reach the Hangouts services extension.
# No fixed --window-size: a fixed size is a tell.
STEALTH_FLAGS = ("--disable-blink-features=AutomationControlled", "--no-first-run",
                 "--no-default-browser-check", "--disable-features=Translate")
# The profile's cookies use their own key, not your everyday Chrome's "Chrome Safe Storage".
KEYCHAIN_FLAGS = ("--use-mock-keychain",) if sys.platform == "darwin" else ("--password-store=basic",)
LOGIN_FORM_WAIT = 10.0  # seconds browser_fill_login waits for a form to fill
MAX_FAILED_FILLS = 2      # per site per 24 h, across agents, before the human must approve a retry
TOTP_WINDOW = 600.0       # seconds after a password fill when a lone numeric box counts as the TOTP step
_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
_POLICED_TARGETS = ("page", "iframe", "worker", "shared_worker", "service_worker")

# "Accept" buttons of common cookie-consent banners, clicked when a page opens and while a
# login form is filled (they steal focus and cover forms). Accepting cookies is fine.
CONSENT_ACCEPT = (
    "#onetrust-accept-btn-handler",                                  # OneTrust ("I understand", "Accept all")
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",        # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",
    "#truste-consent-button",                                        # TrustArc
    "#didomi-notice-agree-button",                                   # Didomi
    ".osano-cm-accept-all",                                          # Osano
    ".cky-btn-accept",                                               # CookieYes
    ".cmplz-btn.cmplz-accept",                                       # Complianz
    "[data-testid='uc-accept-all-button']",                          # Usercentrics
    ".fc-cta-consent",                                               # Google consent (Funding Choices)
    ".cc-window .cc-allow, .cc-window .cc-dismiss",                  # Insites Cookie Consent
)
_CONSENT_JS = """(() => {
  const roots = [document, ...[...document.querySelectorAll('*')].filter(e => e.shadowRoot).map(e => e.shadowRoot)];
  for (const sel of %s) for (const root of roots) for (const el of root.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none')
      return {x: r.left + r.width / 2, y: r.top + r.height / 2, sel};
  }
  return null;
})()""" % json.dumps(list(CONSENT_ACCEPT))

# Read the page from an isolated world: interactive elements with what a person sees.
_SNAPSHOT_JS = r"""
(() => {
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=tab],[contenteditable=true],summary';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    let value = null;
    if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && type !== 'password') value = el.value || null;
    out.push({tag: el.tagName.toLowerCase(), type: type || null, role: el.getAttribute('role'),
              text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                     el.getAttribute('title') || el.getAttribute('name') || '').trim().slice(0, 120),
              value, password: type === 'password'});
    if (out.length >= 150) break;
  }
  return out;
})()
"""


def find_chrome() -> str | None:
    for path in CHROME_PATHS:
        if os.path.exists(path):
            return path
    return shutil.which("google-chrome") or shutil.which("chromium")


def totp(seed_base32: str, at: float | None = None, digits: int = 6, period: int = 30) -> str:
    """RFC 6238 code for a base32 seed (as authenticator apps show)."""
    key = base64.b32decode(seed_base32.replace(" ", "").upper() + "=" * (-len(seed_base32.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


class BrowserAccessError(PermissionError):
    pass


class _Pipe:
    """CDP over --remote-debugging-pipe: JSON messages, NUL-terminated."""

    def __init__(self, write_fd: int, read_fd: int):
        self.write_fd, self.read_fd = write_fd, read_fd
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.handlers: dict[str, Any] = {}
        self.reader: asyncio.Task | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._buf = b""
        self._queue: asyncio.Queue = asyncio.Queue()
        loop.add_reader(self.read_fd, self._readable)
        self.reader = asyncio.ensure_future(self._dispatch())

    def _readable(self) -> None:
        try:
            data = os.read(self.read_fd, 1 << 16)
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.read_fd)
            self._queue.put_nowait(None)
            return
        self._buf += data
        while b"\0" in self._buf:
            msg, self._buf = self._buf.split(b"\0", 1)
            self._queue.put_nowait(json.loads(msg))

    async def _dispatch(self) -> None:
        while (msg := await self._queue.get()) is not None:
            if "id" in msg:
                fut = self.pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("method") in self.handlers:
                asyncio.ensure_future(self.handlers[msg["method"]](msg.get("params", {}), msg.get("sessionId")))
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Chrome went away"))

    async def send(self, method: str, params: dict | None = None, session: str | None = None,
                   timeout: float = 30) -> dict:
        i = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        msg = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self.write_fd, json.dumps(msg).encode() + b"\0")
        res = await asyncio.wait_for(fut, timeout)
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error'].get('message')}")
        return res.get("result", {})

    def close(self) -> None:
        try:
            asyncio.get_running_loop().remove_reader(self.read_fd)
        except Exception:
            pass
        for fd in (self.write_fd, self.read_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.reader:
            self.reader.cancel()



# Which fields a login fill uses, chosen by structure like a password manager (the agent
# never points at one). Returns elements (as remote objects) and a step, or an error.
_LOGIN_FIELDS_JS = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
                     return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none' && !e.disabled && !e.readOnly; };
  const inputs = [...document.querySelectorAll('input')].filter(vis);
  const ac = e => (e.getAttribute('autocomplete') || '').toLowerCase();
  const texty = inputs.filter(e => ['', 'text', 'email', 'tel', 'number'].includes((e.getAttribute('type') || '').toLowerCase()));
  const named = e => ((e.name || '') + ' ' + (e.id || '')).toLowerCase();
  const rank = e => /username|email/.test(ac(e)) ? 0 : e.type === 'email' ? 1 : /user|email|login/.test(named(e)) ? 2 : 9;
  const pw = inputs.filter(e => e.type === 'password');
  if (pw.length > 1) return {error: 'several password boxes (a sign-up or change-password form?)'};
  if (pw.length === 1) {
    const p = pw[0];
    let scope = p.form;
    if (!scope) { scope = p.parentElement;
      for (let i = 0; scope && i < 6 && !texty.some(e => scope.contains(e)); i++) scope = scope.parentElement; }
    const users = texty.filter(e => scope && scope.contains(e) && (!p.form || e.form === p.form) && rank(e) < 9)
                       .sort((a, b) => rank(a) - rank(b));
    return {step: 'password', password: p, username: users[0] || null};
  }
  const otp = texty.filter(e => ac(e).includes('one-time-code'));
  if (otp.length === 1) return {step: 'totp', totp: otp[0]};
  const numeric = e => e.inputMode === 'numeric' || ['tel', 'number'].includes(e.type) || /code|otp|token|2fa|mfa/.test(named(e));
  if (texty.length === 1 && numeric(texty[0]) && !(texty[0].maxLength > 10)) return {step: 'totp-lone', totp: texty[0]};
  const explicit = texty.filter(e => /username|email/.test(ac(e)) || e.type === 'email');
  if (explicit.length === 1 && texty.length === 1) return {step: 'username', username: explicit[0]};
  if (!inputs.length) return {error: 'nothing to fill on this page'};
  return {error: 'no unambiguous login fields (fill it by hand in the browser window, or navigate to the login form)'};
})()
"""

_GRANTED = ("once", "session", "always")


@dataclass
class Browser:
    workspace: str | Path | None = None
    # site -> url (a suggested start page), hosts (patterns; default: the url's site and its
    # subdomains), tier ("everyday" | "sensitive"), logout_url, username/password/totp (fnox names)
    logins: dict[str, dict[str, Any]] = field(default_factory=dict)
    allow: list[str] | None = None        # host patterns the browser may reach (None: any)
    allowed: bool = False                 # skip the lease (logins and gated sites still need their own approval)
    logins_allowed: bool | set[str] = False  # sites whose logins need no approval (host code decided)
    gated: list[str] = field(default_factory=list)  # more sites with a stored session (registrable domains)
    profile: BaseProfile | None = None    # a long-lived base profile, cloned per session (None: throwaway)
    approvals: Any = None
    chrome: str | None = None
    fnox_cwd: str | Path | None = None    # where fnox finds the login secrets
    lease_until: float = 0.0
    _proc: Any = field(default=None, repr=False)
    _pipe: _Pipe | None = field(default=None, repr=False)
    _session: str | None = field(default=None, repr=False)
    _profile: Path | None = field(default=None, repr=False)
    _clone: Clone | None = field(default=None, repr=False)
    _proxy: PolicyProxy | None = field(default=None, repr=False)
    _pending: str | None = field(default=None, repr=False)
    _page_target: str | None = field(default=None, repr=False)
    _pages: dict[str, str] = field(default_factory=dict, repr=False)            # tab target id -> session
    _login_pending: dict[str, str] = field(default_factory=dict, repr=False)    # site -> approval id
    _login_grants: set[str] = field(default_factory=set, repr=False)           # logins approved for this session
    _site_pending: dict[str, str] = field(default_factory=dict, repr=False)     # gated site -> approval id
    _site_grants: set[str] = field(default_factory=set, repr=False)            # gated sites granted this session
    _used: set[str] = field(default_factory=set, repr=False)                   # sites this session reached
    _logged_in: set[str] = field(default_factory=set, repr=False)              # logins filled this session
    _totp_until: dict[str, float] = field(default_factory=dict, repr=False)
    _attempt: dict[str, Any] | None = field(default=None, repr=False)          # the last password fill, unjudged
    _elements: list[int] = field(default_factory=list, repr=False)   # ref -> backendNodeId
    _blocked: list[str] = field(default_factory=list, repr=False)
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.chrome = self.chrome or find_chrome()
        if not self.chrome:
            raise RuntimeError("Google Chrome (or Chromium) not found")

    @property
    def _root(self) -> Path:
        return self.profile.root if self.profile is not None else ROOT

    # ------------------------------------------------------------------ #
    # Lease
    # ------------------------------------------------------------------ #

    def _base_unlock(self) -> tuple[list[str], Any] | None:
        """(the base profile's fnox key, an unlocker) when the encrypted base is locked."""
        p = self.profile
        if p is None or not p.key or p.password is not None:
            return None
        cwd = Path(p.fnox_cwd or self.fnox_cwd or os.getcwd()).resolve()
        missing = fnox.uncached([p.key], cwd)
        return (missing, lambda names, pw: fnox.fill(names, pw, cwd=cwd)) if missing else None

    def request(self, minutes: int, reason: str) -> dict[str, Any]:
        if self.allowed:
            return {"status": "allowed"}
        if self.approvals is None:
            raise BrowserAccessError("browser access needs an approver (none is configured)")
        approval = self.approvals.request("browser", "", {"minutes": int(minutes)}, reason,
                                          unlock=self._base_unlock())
        self._pending = approval.id
        return {"id": approval.id, "status": approval.status}

    async def _check(self) -> None:
        if self.allowed:
            return
        if self._pending and self.approvals is not None:
            a = self.approvals.items.get(self._pending)
            if a is not None and a.status in _GRANTED:
                self.lease_until = (a.decided or time.time()) + 60 * int(a.details.get("minutes", 15))
                self._pending = None
        if time.time() < self.lease_until:
            return
        await self.close()  # an ended lease ends the session
        raise BrowserAccessError("no access to the browser right now: call request_browser(minutes, reason) "
                                 "and wait for a human to approve it")

    async def _unlock_base(self) -> None:
        """The encrypted base profile is locked in fnox: ask for an unlock and wait for it."""
        unlock = await asyncio.to_thread(self._base_unlock)
        if unlock is None:
            return
        if self.approvals is None:
            raise BrowserAccessError("the browser profile is locked in fnox, and no approver is configured to "
                                     "unlock it")
        approval = self.approvals.request("unlock", "", {"secrets": unlock[0], "what": "browser profile"},
                                          "open the browser profile", unlock=unlock)
        status = await self.approvals.wait(approval.id)
        if status not in _GRANTED:
            raise BrowserAccessError(f"the browser profile is locked in fnox: unlock approval {approval.id} is "
                                     f"{status}")

    # ------------------------------------------------------------------ #
    # Logins and their approvals
    # ------------------------------------------------------------------ #

    def _login_hosts(self, site: str) -> list[str]:
        conf = self.logins.get(site)
        if conf is None:
            raise LookupError(f"no login configured for {site!r} (have: {sorted(self.logins)})")
        if conf.get("hosts"):
            return [h.lower() for h in conf["hosts"]]
        host = (urlsplit(conf["url"]).hostname or "").lower() if conf.get("url") else ""
        if not host:
            raise ValueError(f"login {site!r} needs a url or hosts (where its credentials may be typed)")
        # The url's site and its subdomains, so a saved example.com/login that redirects to
        # www.example.com/signin still counts.
        base = host[4:] if host.startswith("www.") else host
        return [base, f"*.{base}"]

    def _login_sites(self, site: str) -> set[str]:
        """The registrable domains a login's session lives on (its hosts, identity providers included)."""
        return {site_of(h.lstrip("*.")) for h in self._login_hosts(site)}

    def _login_granted(self, site: str) -> bool:
        """Allowed by host code, for this session, or "always"."""
        hosts = self._login_hosts(site)
        if self.logins_allowed is True or site in (self.logins_allowed or ()) or site in self._login_grants:
            return True
        return self.approvals is not None and f"{site}@{hosts[0]}" in self.approvals.saved_logins()

    def request_login(self, site: str, reason: str) -> dict[str, Any]:
        hosts = self._login_hosts(site)
        if self._login_granted(site):
            return {"status": "allowed"}
        if self.approvals is None:
            raise BrowserAccessError("logins need an approver (none is configured)")
        names = self._login_secrets(site)
        missing = self._uncached(names)  # locked in fnox: approving will need the master password
        approval = self.approvals.request("browser_login", "", {
            "site": site, "host": hosts[0], "hosts": hosts, "secrets": names}, reason,
            unlock=(missing, self._fill) if missing else None)
        self._login_pending[site] = approval.id
        return {"id": approval.id, "status": approval.status}

    def _login_secrets(self, site: str) -> list[str]:
        conf = self.logins[site]
        return [conf[k] for k in ("username", "password", "totp") if conf.get(k)]

    def _fnox_dir(self) -> Path:
        return Path(self.fnox_cwd or os.getcwd()).resolve()

    def _uncached(self, names: list[str]) -> list[str]:
        return fnox.uncached(names, self._fnox_dir())

    def _fill(self, names: list[str], password: bytearray) -> dict[str, str]:
        return fnox.fill(names, password, cwd=self._fnox_dir())

    async def _ensure_unlocked(self, site: str) -> None:
        """A login approved earlier (always, session, by host code) whose secrets are locked in
        fnox asks for an unlock (the human's master password) and waits for it."""
        missing = await asyncio.to_thread(self._uncached, self._login_secrets(site))
        if not missing:
            return
        if self.approvals is None:
            raise BrowserAccessError(f"{site}'s credentials ({', '.join(missing)}) are locked in fnox, "
                                     "and no approver is configured to unlock them")
        approval = self.approvals.request("unlock", "", {"site": site, "host": self._login_hosts(site)[0],
                                                         "secrets": missing}, f"log in to {site}",
                                          unlock=(missing, self._fill))
        status = await self.approvals.wait(approval.id)
        if status not in _GRANTED:
            raise BrowserAccessError(f"{site}'s credentials are locked in fnox: unlock approval {approval.id} is "
                                     f"{status}" + ("" if status == "deny" else "; call browser_fill_login again "
                                                    "once it's approved"))
        if await asyncio.to_thread(self._uncached, missing):
            raise BrowserAccessError(f"{site}'s credentials are still locked in fnox")

    def _use_login(self, site: str, spend: bool = True) -> None:
        """Raise unless ``site``'s login is approved; a 'once' approval is spent here (with ``spend``)."""
        if self._login_granted(site):
            return
        if self.approvals is not None:
            a = self.approvals.items.get(self._login_pending.get(site, ""))
            if a is not None and a.status in _GRANTED:
                self._site_grants |= self._login_sites(site)  # the approval opens its sites for the session
                if a.status != "once":
                    self._login_grants.add(site)
                    del self._login_pending[site]
                elif spend:
                    del self._login_pending[site]
                return
            if a is not None and a.status == "deny":
                del self._login_pending[site]
                raise BrowserAccessError(f"the human denied logging in to {site}")
        raise BrowserAccessError(f"logging in to {site} needs approval: call request_login({site!r}, reason) "
                                 "and wait for a human to approve it")

    def _login_approved(self, site: str) -> bool:
        """Granted, or approved and not used yet (a login approval implies its sites' grant)."""
        if self._login_granted(site):
            return True
        a = self.approvals.items.get(self._login_pending.get(site, "")) if self.approvals is not None else None
        if a is not None and a.status in _GRANTED:
            self._site_grants |= self._login_sites(site)  # for the session, even once a 'once' is spent
            return True
        return False

    async def _login_host_ok(self, site: str) -> tuple[bool, str]:
        url = (await self._send("Page.getFrameTree"))["frameTree"]["frame"].get("url", "")  # Chrome's, not the page's
        host = (urlsplit(url).hostname or "").lower()
        return any(fnmatch.fnmatchcase(host, h) for h in self._login_hosts(site)), host or url

    async def _on_login_host(self, site: str) -> None:
        ok, host = await self._login_host_ok(site)
        if not ok:
            raise BrowserAccessError(f"the page is on {host!r}, not {site}'s login hosts "
                                     f"{self._login_hosts(site)}: its credentials are only typed there")

    # ------------------------------------------------------------------ #
    # The per-site gate
    # ------------------------------------------------------------------ #

    def _gated_sites(self) -> set[str]:
        sites = {site_of(g) for g in self.gated}
        for name in self.logins:
            with contextlib.suppress(LookupError, ValueError):
                sites |= self._login_sites(name)
        return sites

    def _site_granted(self, site: str) -> bool:
        if site in self._site_grants:
            return True
        if self.approvals is not None:
            if site in self.approvals.saved_sites():
                return True
            a = self.approvals.items.get(self._site_pending.get(site, ""))
            if a is not None and a.status in _GRANTED:
                self._site_grants.add(site)  # once and session both last this browser session
                del self._site_pending[site]
                return True
        for name in self.logins:
            with contextlib.suppress(LookupError, ValueError):
                if site in self._login_sites(name) and self._login_approved(name):
                    return True
        return False

    def _gate_closed(self, host: str) -> str | None:
        """The gated site ``host`` belongs to, if this session has no grant for it."""
        site = site_of(host)
        return site if site in self._gated_sites() and not self._site_granted(site) else None

    def _allowlisted(self, host: str) -> bool:
        return self.allow is None or any(fnmatch.fnmatchcase(host, p) for p in self.allow)

    def _proxy_allows(self, host: str) -> bool:
        host = host.lower()
        return self._allowlisted(host) and self._gate_closed(host) is None

    def request_site(self, site: str, reason: str) -> dict[str, Any]:
        site = site_of(site)
        if site not in self._gated_sites() or self._site_granted(site):
            return {"status": "allowed"}
        if self.approvals is None:
            raise BrowserAccessError(f"using your account on {site} needs an approver (none is configured)")
        approval = self.approvals.request("browser_site", "", {"site": site}, reason)
        self._site_pending[site] = approval.id
        return {"id": approval.id, "status": approval.status}

    def _blocked_urls(self) -> list[str]:
        """WebSockets to gated sites without a grant (request interception doesn't see them)."""
        return [f"{scheme}://{h}/*" for site in sorted(self._gated_sites()) if not self._site_granted(site)
                for scheme in ("ws", "wss") for h in (site, f"*.{site}")]

    def _setup_page(self, session: str) -> list[asyncio.Future]:
        """Service workers bypassed, WebSockets to closed sites blocked. Sent at once, in order:
        a target held at start answers only once released, but applies them before it runs."""
        self._blocked = self._blocked_urls()
        return [asyncio.ensure_future(self._pipe.send(method, params, session)) for method, params in (
            ("Network.enable", {}), ("Network.setBypassServiceWorker", {"bypass": True}),
            ("Network.setBlockedURLs", {"urls": self._blocked}))]

    async def _refresh_blocks(self) -> None:
        """Grants change WebSocket blocking (checked before each action)."""
        urls = self._blocked_urls()
        if self._pipe is None or urls == self._blocked:
            return
        self._blocked = urls
        for session in list(self._pages.values()):
            with contextlib.suppress(RuntimeError, ConnectionError, asyncio.TimeoutError):
                await self._pipe.send("Network.setBlockedURLs", {"urls": self._blocked_urls()}, session)

    def _note_site(self, site: str) -> None:
        if site in self._used:
            return
        self._used.add(site)
        if self._clone is not None:
            sensitive = {s for name in self.logins if self.logins[name].get("tier") == "sensitive"
                         for s in self._login_sites(name)}
            self.profile.note(self._clone, used=[site], clear=[site] if site in sensitive else [])

    # ------------------------------------------------------------------ #
    # Chrome
    # ------------------------------------------------------------------ #

    async def _ensure(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pipe is not None and self._proc is not None and self._proc.poll() is None:
                return
            await self._launch()

    async def _launch(self) -> None:
        if self.profile is not None:
            await self._unlock_base()
            self._clone = await asyncio.to_thread(self.profile.clone)
            self._profile = self._clone.dir
        else:
            self._profile = Path(tempfile.mkdtemp(prefix="agentd-browser-"))
        self._proxy = PolicyProxy(self._proxy_allows)
        port = await self._proxy.start()
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()

        # Chrome reads commands on fd 3 and writes on fd 4: sh puts the pipes
        # there (Python closes unlisted fds after any pre-exec hook). cwd: the
        # profile, never the caller's directory (on macOS, file access by our
        # child is attributed to us). Every connection goes through the policy
        # proxy, loopback included.
        self._proc = subprocess.Popen(
            ["/bin/sh", "-c", f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w}', self.chrome,
             f"--user-data-dir={self._profile}", "--remote-debugging-pipe", *STEALTH_FLAGS, *KEYCHAIN_FLAGS,
             # A clone keeps session cookies (sites that sign you in without an expiry) like a
             # restored browser; it never has saved tabs to reopen.
             *(("--restore-last-session",) if self._clone is not None else ()),
             f"--proxy-server=http://127.0.0.1:{port}", "--proxy-bypass-list=<-loopback>", "about:blank"],
            cwd=self._profile, pass_fds=(to_chrome_r, from_chrome_w),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._pipe = _Pipe(to_chrome_w, from_chrome_r)
        await self._pipe.start()
        targets = (await self._pipe.send("Target.getTargets"))["targetInfos"]
        page = next((t for t in targets if t["type"] == "page"), None)
        if page is None:
            page = {"targetId": (await self._pipe.send("Target.createTarget", {"url": "about:blank"}))["targetId"]}
        self._page_target = page["targetId"]
        self._session = (await self._pipe.send("Target.attachToTarget",
                                               {"targetId": page["targetId"], "flatten": True}))["sessionId"]
        self._pages = {self._page_target: self._session}
        await self._send("Page.enable")  # lifecycle only; never Runtime.enable
        self._pipe.handlers["Fetch.requestPaused"] = self._on_request
        self._pipe.handlers["Target.attachedToTarget"] = self._on_attached
        self._pipe.handlers["Target.detachedFromTarget"] = self._on_detached
        await self._send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        await asyncio.gather(*self._setup_page(self._session), return_exceptions=True)
        # Every other target (new tabs and popups, iframes in their own process, workers) is held
        # at start until its requests are intercepted too.
        await self._pipe.send("Target.setAutoAttach", _AUTO_ATTACH)
        await self._send("Target.setAutoAttach", _AUTO_ATTACH)

    async def _on_attached(self, params: dict, session: str | None) -> None:
        info, child = params.get("targetInfo", {}), params.get("sessionId")
        queued: list[asyncio.Future] = []
        try:
            if info.get("targetId") != self._page_target and info.get("type") in _POLICED_TARGETS:
                await self._pipe.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                                      child)
                if info.get("type") in ("page", "iframe"):
                    await self._pipe.send("Target.setAutoAttach", _AUTO_ATTACH, child)
                    queued = self._setup_page(child)
                if info.get("type") == "page":
                    self._pages[info["targetId"]] = child
        except (RuntimeError, ConnectionError, asyncio.TimeoutError, AttributeError):
            pass
        finally:
            if params.get("waitingForDebugger") and self._pipe is not None:
                with contextlib.suppress(RuntimeError, ConnectionError, asyncio.TimeoutError):
                    await self._pipe.send("Runtime.runIfWaitingForDebugger", {}, child)
            await asyncio.gather(*queued, return_exceptions=True)

    async def _on_detached(self, params: dict, session: str | None) -> None:
        tid = params.get("targetId")
        if tid in self._pages and tid != self._page_target:
            del self._pages[tid]

    async def _send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        assert self._pipe is not None
        return await self._pipe.send(method, params, self._session, timeout)

    async def _on_request(self, params: dict, session: str | None) -> None:
        url = params.get("request", {}).get("url", "")
        host = (urlsplit(url).hostname or "").lower()
        local = url.startswith(("data:", "blob:", "about:", "chrome:"))
        allowed = local or self._allowlisted(host)
        if not allowed and self.approvals is not None:
            approval = self.approvals.request("browser_host", "", {"host": host}, "")
            allowed = approval.status in _GRANTED
            if allowed and approval.status in ("session", "always"):
                self.allow = (self.allow or []) + [host]
        if allowed and not local:
            gated = self._gate_closed(host)
            if gated is not None:
                allowed = False
                if self.approvals is not None and gated not in self._site_pending:
                    approval = self.approvals.request("browser_site", "", {"site": gated},
                                                      f"the agent opened {host}")
                    self._site_pending[gated] = approval.id
        if allowed and not local and host:
            self._note_site(site_of(host))
        try:
            if allowed:
                await self._pipe.send("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
            else:
                await self._pipe.send("Fetch.failRequest", {"requestId": params["requestId"],
                                                            "errorReason": "BlockedByClient"}, session)
        except (RuntimeError, ConnectionError, asyncio.TimeoutError):
            pass

    async def close(self) -> dict[str, Any]:
        """Log out of sensitive sites, quit Chrome, and merge the session back into the base
        profile (or delete a throwaway one)."""
        sensitive = [name for name in self.logins if self.logins[name].get("tier") == "sensitive"
                     and self._login_sites(name) & self._used]
        if self._pipe is not None:
            for name in sensitive:  # end the session server-side too, where the site has a logout URL
                if self.logins[name].get("logout_url"):
                    with contextlib.suppress(Exception):
                        await self._send("Page.navigate", {"url": self.logins[name]["logout_url"]}, timeout=10)
                        await asyncio.sleep(2)
            with contextlib.suppress(Exception):
                await self._pipe.send("Browser.close", timeout=5)
            self._pipe.close()
            self._pipe = None
        if self._proc is not None:
            try:
                await asyncio.to_thread(self._proc.wait, 5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._proxy is not None:
            await self._proxy.stop()
            self._proxy = None
        if self._clone is not None:
            clear = {s for name in sensitive for s in self._login_sites(name)}
            try:
                await asyncio.to_thread(self.profile.finish, self._clone, self._used, clear)
            except Exception as e:  # e.g. the base's key relocked: the journal finishes it next time
                logger.warning("browser: couldn't merge the session back yet (%s); it will be on the next start", e)
            self._clone = None
            self._profile = None
        elif self._profile is not None:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None
        self._elements = []
        self._pages = {}
        self._login_grants.clear()  # "session" approvals end with the session
        self._site_grants.clear()
        self._used, self._logged_in = set(), set()
        return {"closed": True}

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    async def _ready(self) -> None:
        await self._check()
        await self._ensure()
        await self._refresh_blocks()

    async def _isolated(self, expression: str, by_value: bool = True) -> dict:
        tree = await self._send("Page.getFrameTree")
        world = await self._send("Page.createIsolatedWorld", {"frameId": tree["frameTree"]["frame"]["id"],
                                                              "worldName": "agentd"})
        return await self._send("Runtime.evaluate", {"expression": expression, "contextId": world["executionContextId"],
                                                     "returnByValue": by_value, "awaitPromise": True})

    async def open(self, url: str) -> dict[str, Any]:
        await self._ready()
        if not urlsplit(url).scheme:
            url = "https://" + url
        result = await self._send("Page.navigate", {"url": url})
        if result.get("errorText"):
            host = (urlsplit(url).hostname or "").lower()
            gated = self._gate_closed(host) if host else None
            error = result["errorText"]
            if gated is not None:
                error = (f"{gated} holds your signed-in session and this session has no grant for it: call "
                         f"request_site({gated!r}, reason)" + (" (or request_login)" if any(
                             gated in self._login_sites(n) for n in self.logins) else ""))
            return host_secrets.scrub({"url": url, "error": error})
        await self._settle()
        await self._dismiss_consent()
        await self._judge_attempt()
        return await self.snapshot()

    async def _settle(self) -> None:
        for _ in range(60):  # wait for the page to settle
            state = await self._isolated("document.readyState")
            if state.get("result", {}).get("value") == "complete":
                break
            await asyncio.sleep(0.25)

    async def _dismiss_consent(self, rounds: int = 2) -> list[str]:
        """Click a known cookie-consent banner's accept button (real mouse events), up to ``rounds``
        times for two-step banners. Returns the selectors clicked."""
        clicked = []
        for _ in range(rounds):
            hit = (await self._isolated(_CONSENT_JS))["result"].get("value")
            if not hit:
                break
            await self._click_at(hit["x"], hit["y"])
            clicked.append(hit["sel"])
            await asyncio.sleep(0.6)
        return clicked

    async def _click_at(self, x: float, y: float) -> None:
        x += random.uniform(-2, 2)
        y += random.uniform(-2, 2)
        await self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await asyncio.sleep(random.uniform(0.05, 0.15))
        for kind in ("mousePressed", "mouseReleased"):
            await self._send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                          "clickCount": 1})
            await asyncio.sleep(random.uniform(0.04, 0.12))

    async def snapshot(self) -> dict[str, Any]:
        """The page: URL, title, visible text (trimmed) and numbered interactive elements."""
        await self._ready()
        info = await self._isolated("({url: location.href, title: document.title, "
                                    "text: (document.body ? document.body.innerText : '').slice(0, 4000)})")
        elements = (await self._isolated(_SNAPSHOT_JS)).get("result", {}).get("value", []) or []
        handles = await self._isolated(_SNAPSHOT_JS.replace("out.push({tag", "out.push(el); ({tag"), by_value=False)
        self._elements = []
        object_id = handles.get("result", {}).get("objectId")
        if object_id:
            props = await self._send("Runtime.getProperties", {"objectId": object_id, "ownProperties": True})
            for p in sorted((p for p in props.get("result", []) if p["name"].isdigit()), key=lambda p: int(p["name"])):
                oid = p.get("value", {}).get("objectId")
                if oid:
                    node = await self._send("DOM.describeNode", {"objectId": oid})
                    self._elements.append(node["node"]["backendNodeId"])
        value = info.get("result", {}).get("value", {})
        listed = [{"ref": i, **{k: v for k, v in e.items() if v not in (None, "", False)}}
                  for i, e in enumerate(elements[: len(self._elements)])]
        return host_secrets.scrub({"url": value.get("url"), "title": value.get("title"),
                                   "text": value.get("text"), "elements": listed})

    async def _center(self, ref: int) -> tuple[float, float]:
        if not 0 <= ref < len(self._elements):
            raise LookupError(f"no element {ref}: take a snapshot first")
        node = self._elements[ref]
        await self._send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node})
        quad = (await self._send("DOM.getBoxModel", {"backendNodeId": node}))["model"]["content"]
        return sum(quad[0::2]) / 4, sum(quad[1::2]) / 4

    async def click(self, ref: int) -> dict[str, Any]:
        await self._ready()
        await self._click_at(*await self._center(ref))
        await asyncio.sleep(0.5)
        await self._judge_attempt(wait=True)
        return await self.snapshot()

    async def _handle(self, node: int) -> str:
        """A handle on a DOM node in agentd's isolated world (the page's scripts can't see our calls)."""
        tree = await self._send("Page.getFrameTree")
        world = await self._send("Page.createIsolatedWorld", {"frameId": tree["frameTree"]["frame"]["id"],
                                                              "worldName": "agentd"})
        obj = await self._send("DOM.resolveNode", {"backendNodeId": node,
                                                   "executionContextId": world["executionContextId"]})
        return obj["object"]["objectId"]

    async def _on(self, handle: str, function: str) -> Any:
        r = await self._send("Runtime.callFunctionOn", {"objectId": handle, "functionDeclaration": function,
                                                        "returnByValue": True})
        return r.get("result", {}).get("value")

    async def _type_into(self, node: int, text: str, *, clear: bool = False) -> None:
        """Type ``text`` into a DOM node. Before each character, focus goes back to it if the
        page moved it (a cookie banner, a chat widget), so no keystroke lands elsewhere."""
        handle = await self._handle(node)
        focused = "function() { const r = this.getRootNode(); return (r.activeElement || document.activeElement) === this; }"
        await self._send("DOM.focus", {"backendNodeId": node})
        if clear:  # select what's there and delete it with a real key press (frameworks see the change)
            await self._on(handle, "function() { if (this.select) this.select(); else document.execCommand('selectAll'); }")
            for kind in ("keyDown", "keyUp"):
                await self._send("Input.dispatchKeyEvent", {"type": kind, "key": "Backspace", "code": "Backspace",
                                                            "windowsVirtualKeyCode": 8})
        for ch in text:
            if not await self._on(handle, focused):
                await self._send("DOM.focus", {"backendNodeId": node})
            await self._send("Input.dispatchKeyEvent", {"type": "char", "text": ch})
            await asyncio.sleep(random.uniform(0.03, 0.09))

    async def _fill_field(self, node: int, text: str) -> None:
        """Type a credential, verify the box got every character (by length only: the value is
        never read back), retype once if not, else fail rather than let it be submitted."""
        await self._dismiss_consent(rounds=1)  # banners often appear while the form is being filled
        handle = await self._handle(node)
        length = "function() { return (this.value !== undefined ? this.value : this.textContent || '').length; }"
        for attempt in range(2):
            await self._type_into(node, text, clear=True)
            if (await self._on(handle, length) or 0) >= len(text):  # >=: some fields format what's typed
                return
        raise BrowserAccessError("the page took focus while typing (or the field doesn't take the whole "
                                 "credential): don't submit")

    async def type(self, ref: int, text: str, submit: bool = False) -> dict[str, Any]:
        await self._ready()
        if not 0 <= ref < len(self._elements):
            raise LookupError(f"no element {ref}: take a snapshot first")
        await self._type_into(self._elements[ref], text)
        if submit:
            await self._press_enter()
            await asyncio.sleep(0.3)
            await self._judge_attempt(wait=True)
        await asyncio.sleep(0.3)
        return await self.snapshot()

    async def _press_enter(self) -> None:
        for kind in ("keyDown", "keyUp"):
            await self._send("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                        "windowsVirtualKeyCode": 13, "text": "\r" if kind == "keyDown" else ""})

    async def screenshot(self) -> dict[str, Any]:
        await self._ready()
        shot = await self._send("Page.captureScreenshot", {"format": "png"})
        base = Path(self.workspace or ".") / ".agentd" / "browser"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"page-{int(time.time() * 1000)}.png"
        path.write_bytes(base64.b64decode(shot["data"]))
        return {"path": str(path)}

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #

    async def tabs(self) -> list[dict[str, Any]]:
        await self._ready()
        infos = (await self._pipe.send("Target.getTargets"))["targetInfos"]
        return host_secrets.scrub([{"id": t["targetId"][:8], "url": t["url"], "title": t.get("title", ""),
                                    "current": t["targetId"] == self._page_target}
                                   for t in infos if t["type"] == "page" and t["targetId"] in self._pages])

    def _tab(self, tab_id: str) -> str:
        matches = [t for t in self._pages if t.startswith(tab_id)]
        if len(matches) != 1:
            raise LookupError(f"no tab {tab_id!r}: see browser_tabs")
        return matches[0]

    async def switch_tab(self, tab_id: str) -> dict[str, Any]:
        await self._ready()
        target = self._tab(tab_id)
        self._page_target, self._session = target, self._pages[target]
        await self._pipe.send("Target.activateTarget", {"targetId": target})
        self._elements = []
        return await self.snapshot()

    async def new_tab(self, url: str) -> dict[str, Any]:
        await self._ready()
        target = (await self._pipe.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        for _ in range(50):  # attached (and policed) by auto-attach
            if target in self._pages:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("the new tab didn't attach")
        self._page_target, self._session = target, self._pages[target]
        self._elements = []
        return await self.open(url)

    async def close_tab(self, tab_id: str) -> dict[str, Any]:
        await self._ready()
        target = self._tab(tab_id)
        if len(self._pages) == 1:
            raise LookupError("that's the last tab: use browser_close to end the session")
        await self._pipe.send("Target.closeTarget", {"targetId": target})
        self._pages.pop(target, None)
        if target == self._page_target:
            self._page_target = next(iter(self._pages))
            self._session = self._pages[self._page_target]
            await self._pipe.send("Target.activateTarget", {"targetId": self._page_target})
            self._elements = []
        return {"closed": tab_id, "tabs": await self.tabs()}

    # ------------------------------------------------------------------ #
    # Filling a login form
    # ------------------------------------------------------------------ #

    async def _login_fields(self) -> dict[str, Any]:
        """{step, password/username/totp: backendNodeId} for the current page, or {error}."""
        r = await self._isolated(_LOGIN_FIELDS_JS, by_value=False)
        oid = r.get("result", {}).get("objectId")
        if not oid:
            return {"error": "couldn't read the page"}
        props = (await self._send("Runtime.getProperties", {"objectId": oid, "ownProperties": True}))["result"]
        plan: dict[str, Any] = {}
        for p in props:
            v = p.get("value", {})
            if v.get("type") == "string":
                plan[p["name"]] = v.get("value")
            elif v.get("objectId") and v.get("subtype") == "node":
                plan[p["name"]] = (await self._send("DOM.describeNode", {"objectId": v["objectId"]}))["node"]["backendNodeId"]
        return plan

    @contextlib.contextmanager
    def _fill_lock(self, site: str) -> Iterator[None]:
        """One fill at a time per site, across every agent on this machine."""
        self._root.mkdir(parents=True, exist_ok=True)
        with open(self._root / f"fill-{site_of(site)}.lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BrowserAccessError(f"another agent is signing in to {site} right now") from None
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _attempts(self) -> Iterator[dict]:
        """The persisted fill attempts per site (shared by every agent), read and written under a lock."""
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / "attempts.json"
        with open(self._root / "attempts.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(path.read_text())
                except (OSError, ValueError):
                    data = {}
                try:
                    yield data
                finally:  # saved even when the caller raises (a refusal records its retry approval)
                    cutoff = time.time() - 86400
                    data = {s: [a for a in v if a["at"] > cutoff] for s, v in data.items()}
                    path.write_text(json.dumps({s: v for s, v in data.items() if v}))
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _check_attempts(self, site: str) -> None:
        """Refuse a fill after MAX_FAILED_FILLS unsuccessful ones in 24 h, until the human approves a retry."""
        with self._attempts() as data:
            recent = [a for a in data.get(site, []) if a["at"] > time.time() - 86400 and not a.get("cleared")
                      and a.get("failed") is not False]
            if len(recent) < MAX_FAILED_FILLS:
                return
            retry = self.approvals.items.get(data.get(f"retry:{site}", [{}])[0].get("id", "")) \
                if self.approvals is not None and data.get(f"retry:{site}") else None
            if retry is not None and retry.status in _GRANTED:
                for a in recent:
                    a["cleared"] = True
                data.pop(f"retry:{site}", None)
                return
            if retry is None or retry.status not in ("pending",):
                if self.approvals is None:
                    raise BrowserAccessError(f"{len(recent)} sign-ins to {site} in the last 24 h didn't get through; "
                                             "not trying again (sign in by hand in the browser window)")
                retry = self.approvals.request("browser_login_retry", "", {
                    "site": site, "attempts": [{"at": a["at"], "failed": a.get("failed")} for a in recent]},
                    f"{len(recent)} sign-ins to {site} in the last 24 h didn't get through")
                data[f"retry:{site}"] = [{"id": retry.id, "at": time.time()}]
            raise BrowserAccessError(f"{len(recent)} sign-ins to {site} in the last 24 h didn't get through "
                                     f"(wrong credentials, a challenge, or the site blocking us); not trying again "
                                     f"until the human approves a retry (approval {retry.id}). Never retry a login "
                                     "on your own.")

    async def _judge_attempt(self, wait: bool = False) -> None:
        """After a password fill was submitted: did the page move past the login form?"""
        a = self._attempt
        if a is None or time.time() - a["at"] < 1:
            return
        if wait:
            await asyncio.sleep(1.5)
            await self._settle()
        site = a["site"]
        on_login, _ = await self._login_host_ok(site)
        plan = await self._login_fields() if on_login else {}
        failed = bool(on_login and plan.get("step") == "password")
        self._attempt = None
        with self._attempts() as data:
            for rec in data.get(site, []):
                if rec["id"] == a["id"]:
                    rec["failed"] = failed
        if not failed:
            self._logged_in.add(site)

    async def fill_login(self, site: str) -> dict[str, Any]:
        """Fill the login form on the current page from fnox: fields picked by structure, only on
        the login's hosts, not submitted (the agent clicks the form's button)."""
        await self._ready()
        # A 'once' is spent when the password is typed; the TOTP step right after it is part of
        # the same login (only a code may be filled then).
        code_step_only = False
        try:
            self._use_login(site, spend=False)
        except BrowserAccessError:
            if time.time() > self._totp_until.get(site, 0):
                raise
            code_step_only = True
        await self._ensure_unlocked(site)
        await self._judge_attempt()
        self._check_attempts(site)
        await self._on_login_host(site)
        conf = self.logins[site]
        kw = {"cwd": self.fnox_cwd} if self.fnox_cwd else {}
        with self._fill_lock(site):
            deadline = time.monotonic() + LOGIN_FORM_WAIT  # forms built by JavaScript after the load
            while True:
                await self._dismiss_consent(rounds=1)
                plan = await self._login_fields()
                if plan.get("step") or time.monotonic() >= deadline or "several" in plan.get("error", ""):
                    break
                await asyncio.sleep(0.5)
            step = plan.get("step")
            if step is None:
                raise BrowserAccessError(f"can't fill {site}'s login here: {plan.get('error')}")
            if code_step_only and step not in ("totp", "totp-lone"):
                raise BrowserAccessError(f"logging in to {site} again needs approval: call request_login({site!r}, "
                                         "reason)")
            filled = []
            if step == "password":
                if plan.get("username") and conf.get("username"):
                    await self._fill_field(plan["username"], host_secrets.secret(conf["username"], **kw))
                    filled.append("username")
                await self._on_login_host(site)  # still there, right before the password
                await self._fill_field(plan["password"], host_secrets.secret(conf["password"], **kw))
                filled.append("password")
                self._use_login(site)  # credentials typed: a 'once' approval is now used
                self._totp_until[site] = time.time() + TOTP_WINDOW
                rec = {"id": os.urandom(6).hex(), "at": time.time(), "failed": None}
                with self._attempts() as data:
                    data.setdefault(site, []).append(rec)
                self._attempt = {"site": site, **rec}
            elif step in ("totp", "totp-lone"):
                if not conf.get("totp"):
                    raise BrowserAccessError(f"this page asks for a one-time code, and {site}'s login has no TOTP "
                                             "seed (enter it by hand in the browser window)")
                if step == "totp-lone" and time.time() > self._totp_until.get(site, 0):
                    raise BrowserAccessError("a lone code box, but no password was filled for this site just "
                                             "before: not filling it")
                code = totp(host_secrets.secret(conf["totp"], **kw))
                host_secrets.remember(code)  # derived, not read from fnox: scrub it from snapshots too
                await self._fill_field(plan["totp"], code)
                filled.append("totp")
            elif step == "username":
                await self._fill_field(plan["username"], host_secrets.secret(conf["username"], **kw))
                filled.append("username")
        page = await self.snapshot()
        return {"filled": filled, "next": "click the form's sign-in button once (never retry a failed login)",
                **page}


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

_BROWSER: Browser | None = None


def enable_browser_skills(browser: Browser) -> None:
    global _BROWSER
    from agentd.egress.approvals import register_request_tool
    from agentd.tool_decorator import tool

    _BROWSER = browser
    for func in TOOLS:
        if func is request_browser:
            a = browser.approvals if not browser.allowed else None
            register_request_tool(func, a, *([a.wait_note()] if a else []))
        elif func is request_login:
            a = browser.approvals if browser.logins_allowed is not True else None
            register_request_tool(func, a, *([a.wait_note()] if a else []))
        elif func is request_site:
            register_request_tool(func, browser.approvals, *([browser.approvals.wait_note()]
                                                             if browser.approvals else []))
        else:
            tool(func)


def _b() -> Browser:
    if _BROWSER is None:
        raise RuntimeError("browser tools aren't enabled")
    return _BROWSER


async def _answer(r: dict) -> dict:
    b = _b()
    if r.get("status") == "pending" and b.approvals is not None:
        r["status"] = await b.approvals.wait(r["id"])
    return r


async def request_browser(minutes: int, reason: str) -> dict:
    """Ask the human for a browser session for some minutes. Returns an approval id and its status; browser tools work once approved.

    minutes: how long the task needs the browser
    reason: what for, for the human approving
    """
    return await _answer(_b().request(minutes, reason))


async def request_login(site: str, reason: str) -> dict:
    """Ask the human to let you log in to one configured site (browser_fill_login). Each site needs its own approval, which also opens the site; returns an approval id and its status.

    site: a configured site name
    reason: why the task needs to be logged in there, for the human approving
    """
    return await _answer(_b().request_login(site, reason))


async def request_site(site: str, reason: str) -> dict:
    """Ask the human to let this session use a site where they're signed in (e.g. one browser_open said holds a signed-in session). Returns an approval id and its status.

    site: the site (e.g. example.com)
    reason: why the task needs it, for the human approving
    """
    return await _answer(_b().request_site(site, reason))


async def browser_open(url: str) -> dict:
    """Open a URL in the current tab; returns the page (text and numbered elements).

    url: the address
    """
    return await _b().open(url)


async def browser_snapshot() -> dict:
    """The current page: URL, title, visible text and numbered interactive elements."""
    return await _b().snapshot()


async def browser_click(ref: int) -> dict:
    """Click an element from the last snapshot.

    ref: the element's number
    """
    return await _b().click(ref)


async def browser_type(ref: int, text: str, submit: bool = False) -> dict:
    """Type into an element from the last snapshot (not for credentials: use browser_fill_login).

    ref: the element's number
    text: what to type
    submit: press Enter afterwards
    """
    return await _b().type(ref, text, submit)


async def browser_screenshot() -> dict:
    """Take a screenshot of the page; returns the PNG's path."""
    return await _b().screenshot()


async def browser_fill_login(site: str) -> dict:
    """Fill the login form on the current page with credentials the human stored (you never see them, and you don't pick the fields). Navigate to the form first; afterwards click its sign-in button once. Never retry a failed login. Needs request_login(site) approved.

    site: a configured site name
    """
    return await _b().fill_login(site)


async def browser_tabs() -> list:
    """List the open tabs: id, URL, title and which one is current."""
    return await _b().tabs()


async def browser_new_tab(url: str) -> dict:
    """Open a URL in a new tab and make it current.

    url: the address
    """
    return await _b().new_tab(url)


async def browser_switch_tab(tab_id: str) -> dict:
    """Make another tab current (see browser_tabs); returns its page.

    tab_id: the tab's id
    """
    return await _b().switch_tab(tab_id)


async def browser_close_tab(tab_id: str) -> dict:
    """Close a tab.

    tab_id: the tab's id
    """
    return await _b().close_tab(tab_id)


async def browser_close() -> dict:
    """End the browser session (signed-in sessions are kept, except for sensitive sites)."""
    return await _b().close()


TOOLS = (request_browser, request_login, request_site, browser_open, browser_snapshot, browser_click, browser_type,
         browser_screenshot, browser_fill_login, browser_tabs, browser_new_tab, browser_switch_tab, browser_close_tab,
         browser_close)
