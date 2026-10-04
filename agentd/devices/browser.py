"""A real Chrome as agent tools, on the host, with short-lived sessions.

    from agentd.devices.browser import Browser, enable_browser_skills

    browser = Browser(workspace="/path/to/ws", approvals=approvals,
                      logins={"github.com": {"url": "https://github.com/login",
                                             "username": "GH_USER", "password": "GH_PASSWORD",
                                             "totp": "GH_TOTP_SEED"}})
    enable_browser_skills(browser)

* Headed Google Chrome with a fresh profile, driven over the DevTools
  Protocol through a pipe (no debugging port another local process could
  use). It avoids known automation tells: no ``navigator.webdriver``
  (``--disable-blink-features=AutomationControlled``), no ``Runtime.enable``,
  pages read from an isolated world; clicks and typing are real input events
  with human-like timing.
* Access is a lease (``request_browser``, approved through the same webhook
  as egress); when it ends, or on ``browser_close``, Chrome quits and its
  profile (every cookie) is deleted. The lease is for browsing only: no
  credentials.
* Each site's login needs its own approval (``request_login(site, reason)``:
  once, for this browser session, or always). ``browser_login(site)`` then
  fills credentials from fnox (and TOTP codes from a seed in fnox) on the
  host, and only into a page on that login's hosts (its ``url``'s host, or
  ``hosts``), so they can't be steered onto another site. The agent never
  sees them, password fields are never read back, and tool output is
  scrubbed of every secret.
* Optional ``allow`` (host patterns) and the approvals hook police every
  request the browser makes (CDP ``Fetch``), the same way as sandbox egress.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import hmac
import itertools
import json
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
from typing import Any
from urllib.parse import urlsplit

from agentd import fnox
from agentd import secrets as host_secrets

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
)
# AutomationControlled keeps navigator.webdriver false (it's true when driven over the
# pipe), at the cost of Chrome's "unsupported command-line flag" bar. Don't add
# --test-type to hide the bar: it also stops Chrome's component extensions, so the
# "Google ..." speech voices vanish from speechSynthesis.getVoices() (any site can see
# that) and Google's own pages can't reach the Hangouts services extension.
STEALTH_FLAGS = ("--disable-blink-features=AutomationControlled", "--no-first-run",
                 "--no-default-browser-check", "--disable-features=Translate", "--window-size=1280,900")
LOGIN_FORM_WAIT = 10.0  # seconds browser_login waits for a password field to appear
_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
_POLICED_TARGETS = ("page", "iframe", "worker", "shared_worker", "service_worker")

# "Accept" buttons of common cookie-consent banners, clicked when a page opens and before
# a login (they steal focus and cover forms). Accepting is fine: every session's profile,
# cookies included, is deleted when it ends.
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


@dataclass
class Browser:
    workspace: str | Path | None = None
    # site -> url, hosts (patterns; default: the url's host), username/password/totp (fnox names)
    logins: dict[str, dict[str, Any]] = field(default_factory=dict)
    allow: list[str] | None = None        # host patterns the browser may reach (None: any)
    allowed: bool = False                 # skip the lease (logins still need their own approval)
    logins_allowed: bool | set[str] = False  # sites whose logins need no approval (host code decided)
    approvals: Any = None
    chrome: str | None = None
    fnox_cwd: str | Path | None = None    # where fnox finds the login secrets
    lease_until: float = 0.0
    _proc: Any = field(default=None, repr=False)
    _pipe: _Pipe | None = field(default=None, repr=False)
    _session: str | None = field(default=None, repr=False)
    _profile: Path | None = field(default=None, repr=False)
    _pending: str | None = field(default=None, repr=False)
    _page_target: str | None = field(default=None, repr=False)
    _login_pending: dict[str, str] = field(default_factory=dict, repr=False)   # site -> approval id
    _login_grants: set[str] = field(default_factory=set, repr=False)          # sites approved for this session
    _elements: list[int] = field(default_factory=list, repr=False)   # ref -> backendNodeId
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.chrome = self.chrome or find_chrome()
        if not self.chrome:
            raise RuntimeError("Google Chrome (or Chromium) not found")

    # ------------------------------------------------------------------ #
    # Lease and lifecycle
    # ------------------------------------------------------------------ #

    def request(self, minutes: int, reason: str) -> dict[str, Any]:
        if self.allowed:
            return {"status": "allowed"}
        if self.approvals is None:
            raise BrowserAccessError("browser access needs an approver (none is configured)")
        approval = self.approvals.request("browser", "", {"minutes": int(minutes)}, reason)
        self._pending = approval.id
        return {"id": approval.id, "status": approval.status}

    async def _check(self) -> None:
        if self.allowed:
            return
        if self._pending and self.approvals is not None:
            a = self.approvals.items.get(self._pending)
            if a is not None and a.status in ("once", "session", "always"):
                self.lease_until = (a.decided or time.time()) + 60 * int(a.details.get("minutes", 15))
                self._pending = None
        if time.time() < self.lease_until:
            return
        await self.close()  # an ended lease wipes the session
        raise BrowserAccessError("no access to the browser right now: call request_browser(minutes, reason) "
                                 "and wait for a human to approve it")

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
        if status not in ("once", "session", "always"):
            raise BrowserAccessError(f"{site}'s credentials are locked in fnox: unlock approval {approval.id} is "
                                     f"{status}" + ("" if status == "deny" else "; call browser_login again once "
                                                    "it's approved"))
        if await asyncio.to_thread(self._uncached, missing):
            raise BrowserAccessError(f"{site}'s credentials are still locked in fnox")

    def _use_login(self, site: str, spend: bool = True) -> None:
        """Raise unless ``site``'s login is approved; a 'once' approval is spent here (with ``spend``)."""
        if self._login_granted(site):
            return
        if self.approvals is not None:
            a = self.approvals.items.get(self._login_pending.get(site, ""))
            if a is not None and a.status in ("once", "session", "always"):
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

    async def _login_host_ok(self, site: str) -> tuple[bool, str]:
        url = (await self._send("Page.getFrameTree"))["frameTree"]["frame"].get("url", "")  # Chrome's, not the page's
        host = (urlsplit(url).hostname or "").lower()
        return any(fnmatch.fnmatchcase(host, h) for h in self._login_hosts(site)), host or url

    async def _on_login_host(self, site: str) -> None:
        ok, host = await self._login_host_ok(site)
        if not ok:
            raise BrowserAccessError(f"the page is on {host!r}, not {site}'s login hosts "
                                     f"{self._login_hosts(site)}: its credentials are only typed there")

    async def _ensure(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pipe is not None and self._proc is not None and self._proc.poll() is None:
                return
            await self._launch()

    async def _launch(self) -> None:
        self._profile = Path(tempfile.mkdtemp(prefix="agentd-browser-"))
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()

        # Chrome reads commands on fd 3 and writes on fd 4: sh puts the pipes
        # there (Python closes unlisted fds after any pre-exec hook). cwd: the
        # throwaway profile, never the caller's directory (on macOS, file
        # access by our child is attributed to us).
        self._proc = subprocess.Popen(
            ["/bin/sh", "-c", f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w}', self.chrome,
             f"--user-data-dir={self._profile}", "--remote-debugging-pipe", *STEALTH_FLAGS, "about:blank"],
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
        await self._send("Page.enable")  # lifecycle only; never Runtime.enable
        self._pipe.handlers["Fetch.requestPaused"] = self._on_request
        self._pipe.handlers["Target.attachedToTarget"] = self._on_attached
        await self._send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        # Every other target (new tabs and popups, iframes in their own process, workers) is held
        # at start until its requests are intercepted too, so none gets past the allowlist.
        await self._pipe.send("Target.setAutoAttach", _AUTO_ATTACH)
        await self._send("Target.setAutoAttach", _AUTO_ATTACH)

    async def _on_attached(self, params: dict, session: str | None) -> None:
        info, child = params.get("targetInfo", {}), params.get("sessionId")
        try:
            if info.get("targetId") != self._page_target and info.get("type") in _POLICED_TARGETS:
                await self._pipe.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                                      child)
                if info.get("type") in ("page", "iframe"):
                    await self._pipe.send("Target.setAutoAttach", _AUTO_ATTACH, child)
        except (RuntimeError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            if params.get("waitingForDebugger"):
                try:
                    await self._pipe.send("Runtime.runIfWaitingForDebugger", {}, child)
                except (RuntimeError, ConnectionError, asyncio.TimeoutError):
                    pass

    async def _send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        assert self._pipe is not None
        return await self._pipe.send(method, params, self._session, timeout)

    async def _on_request(self, params: dict, session: str | None) -> None:
        url = params.get("request", {}).get("url", "")
        host = (urlsplit(url).hostname or "").lower()
        allowed = url.startswith(("data:", "blob:", "about:", "chrome:")) or self.allow is None \
            or any(fnmatch.fnmatchcase(host, p) for p in self.allow)
        if not allowed and self.approvals is not None:
            approval = self.approvals.request("browser_host", "", {"host": host}, "")
            allowed = approval.status in ("once", "session", "always")
            if allowed and approval.status in ("session", "always"):
                self.allow = (self.allow or []) + [host]
        try:
            if allowed:
                await self._pipe.send("Fetch.continueRequest", {"requestId": params["requestId"]}, session)
            else:
                await self._pipe.send("Fetch.failRequest", {"requestId": params["requestId"],
                                                            "errorReason": "BlockedByClient"}, session)
        except (RuntimeError, ConnectionError, asyncio.TimeoutError):
            pass

    async def close(self) -> dict[str, Any]:
        """Quit Chrome and delete its profile (every cookie)."""
        if self._pipe is not None:
            try:
                await self._pipe.send("Browser.close", timeout=5)
            except Exception:
                pass
            self._pipe.close()
            self._pipe = None
        if self._proc is not None:
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._profile is not None:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None
        self._elements = []
        self._login_grants.clear()  # "session" login approvals end with the session
        return {"closed": True}

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    async def _ready(self) -> None:
        await self._check()
        await self._ensure()

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
            return host_secrets.scrub({"url": url, "error": result["errorText"]})
        for _ in range(60):  # wait for the page to settle
            state = await self._isolated("document.readyState")
            if state.get("result", {}).get("value") == "complete":
                break
            await asyncio.sleep(0.25)
        await self._dismiss_consent()
        return await self.snapshot()

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
        return await self.snapshot()

    async def _node(self, ref: int) -> str:
        """A handle on element ``ref`` in agentd's isolated world (the page's scripts can't see our calls)."""
        tree = await self._send("Page.getFrameTree")
        world = await self._send("Page.createIsolatedWorld", {"frameId": tree["frameTree"]["frame"]["id"],
                                                              "worldName": "agentd"})
        obj = await self._send("DOM.resolveNode", {"backendNodeId": self._elements[ref],
                                                   "executionContextId": world["executionContextId"]})
        return obj["object"]["objectId"]

    async def _on(self, handle: str, function: str) -> Any:
        r = await self._send("Runtime.callFunctionOn", {"objectId": handle, "functionDeclaration": function,
                                                        "returnByValue": True})
        return r.get("result", {}).get("value")

    async def _type_into(self, ref: int, text: str, *, clear: bool = False) -> None:
        """Type ``text`` into element ``ref``. Before each character, focus goes back to it if
        the page moved it (a cookie banner, a chat widget), so no keystroke lands elsewhere."""
        node = self._elements[ref]
        handle = await self._node(ref)
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

    async def _fill_field(self, ref: int, text: str) -> None:
        """Type a credential into ``ref``, verify the box got every character (by length only: the
        value is never read back), retype once if not, else fail rather than submit."""
        await self._dismiss_consent(rounds=1)  # banners often appear while the form is being filled
        handle = await self._node(ref)
        length = "function() { return (this.value !== undefined ? this.value : this.textContent || '').length; }"
        for attempt in range(2):
            await self._type_into(ref, text, clear=True)
            if (await self._on(handle, length) or 0) >= len(text):  # >=: some fields format what's typed
                return
        raise BrowserAccessError("the page took focus while typing (or the field doesn't take the whole "
                                 "credential): not submitting")

    async def type(self, ref: int, text: str, submit: bool = False) -> dict[str, Any]:
        await self._ready()
        if not 0 <= ref < len(self._elements):
            raise LookupError(f"no element {ref}: take a snapshot first")
        await self._type_into(ref, text)
        if submit:
            await self._press_enter()
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

    async def login(self, site: str) -> dict[str, Any]:
        """Fill and submit a site's login form from fnox, on the host (only on its login hosts)."""
        await self._ready()
        self._use_login(site, spend=False)  # a 'once' is spent only when credentials are typed
        await self._ensure_unlocked(site)
        conf = self.logins[site]
        kw = {"cwd": self.fnox_cwd} if self.fnox_cwd else {}
        # Already on the site's login form (the agent got there itself): stay. Otherwise
        # go to the saved URL, which may redirect (old addresses) within the site.
        if conf.get("url") and not ((await self._login_host_ok(site))[0]
                                    and any(e.get("password") for e in (await self.snapshot())["elements"])):
            await self.open(conf["url"])
        await self._on_login_host(site)
        # Many login pages build the form with JavaScript after the load: wait for it. Consent
        # banners (which cover the form or grab focus mid-typing) often load late too.
        deadline = time.monotonic() + LOGIN_FORM_WAIT
        while True:
            await self._dismiss_consent(rounds=1)
            page = await self.snapshot()
            elements = page["elements"]
            password = next((e["ref"] for e in elements if e.get("password")), None)
            if password is not None or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.5)
        if password is None:
            raise LookupError(f"no password field on {page.get('url')} after {LOGIN_FORM_WAIT:g} s")
        user = next((e["ref"] for e in reversed(elements[:password]) if e.get("tag") == "input"
                     and e.get("type") in (None, "text", "email", "tel")), None)
        if user is not None and conf.get("username"):
            await self._fill_field(user, host_secrets.secret(conf["username"], **kw))
        await self._on_login_host(site)  # still there, right before the password
        await self._fill_field(password, host_secrets.secret(conf["password"], **kw))
        self._use_login(site)  # credentials typed: a 'once' approval is now used
        await self._press_enter()
        await asyncio.sleep(1.5)
        if conf.get("totp"):  # a second-factor page: fill the first text/number field
            await self._on_login_host(site)
            page = await self.snapshot()
            code_field = next((e["ref"] for e in page["elements"] if e.get("tag") == "input"
                               and e.get("type") in (None, "text", "number", "tel")), None)
            if code_field is not None:
                code = totp(host_secrets.secret(conf["totp"], **kw))
                host_secrets.remember(code)  # derived, not read from fnox: scrub it from snapshots too
                await self._fill_field(code_field, code)
                await self._press_enter()
                await asyncio.sleep(1.5)
        return await self.snapshot()


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
    """Ask the human to let you log in to one configured site (browser_login). Each site needs its own approval; returns an approval id and its status.

    site: a configured site name
    reason: why the task needs to be logged in there, for the human approving
    """
    return await _answer(_b().request_login(site, reason))


async def browser_open(url: str) -> dict:
    """Open a URL in the browser; returns the page (text and numbered elements).

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
    """Type into an element from the last snapshot.

    ref: the element's number
    text: what to type
    submit: press Enter afterwards
    """
    return await _b().type(ref, text, submit)


async def browser_screenshot() -> dict:
    """Take a screenshot of the page; returns the PNG's path."""
    return await _b().screenshot()


async def browser_login(site: str) -> dict:
    """Log in to a configured site with credentials the human stored (you never see them). Needs request_login(site) approved first.

    site: a configured site name
    """
    return await _b().login(site)


async def browser_close() -> dict:
    """Close the browser and delete its session (cookies included)."""
    return await _b().close()


TOOLS = (request_browser, request_login, browser_open, browser_snapshot, browser_click, browser_type, browser_screenshot,
         browser_login, browser_close)
