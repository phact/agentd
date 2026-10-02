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
  profile (every cookie) is deleted.
* ``browser_login(site)`` fills credentials from fnox (and TOTP codes from a
  seed in fnox) on the host: the agent never sees them, password fields are
  never read back, and tool output is scrubbed of every secret.
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

from agentd import secrets as host_secrets

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
)
STEALTH_FLAGS = ("--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check",
                 "--disable-features=Translate", "--window-size=1280,900")

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
    logins: dict[str, dict[str, str]] = field(default_factory=dict)  # site -> url/username/password/totp (fnox names)
    allow: list[str] | None = None        # host patterns the browser may reach (None: any)
    allowed: bool = False                 # skip the lease
    approvals: Any = None
    chrome: str | None = None
    fnox_cwd: str | Path | None = None    # where fnox finds the login secrets
    lease_until: float = 0.0
    _proc: Any = field(default=None, repr=False)
    _pipe: _Pipe | None = field(default=None, repr=False)
    _session: str | None = field(default=None, repr=False)
    _profile: Path | None = field(default=None, repr=False)
    _pending: str | None = field(default=None, repr=False)
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
        self._session = (await self._pipe.send("Target.attachToTarget",
                                               {"targetId": page["targetId"], "flatten": True}))["sessionId"]
        await self._send("Page.enable")  # lifecycle only; never Runtime.enable
        await self._send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        self._pipe.handlers["Fetch.requestPaused"] = self._on_request

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
        return await self.snapshot()

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
        x, y = await self._center(ref)
        x += random.uniform(-2, 2)
        y += random.uniform(-2, 2)
        await self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await asyncio.sleep(random.uniform(0.05, 0.15))
        for kind in ("mousePressed", "mouseReleased"):
            await self._send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                          "clickCount": 1})
            await asyncio.sleep(random.uniform(0.04, 0.12))
        await asyncio.sleep(0.5)
        return await self.snapshot()

    async def _type_into(self, ref: int, text: str) -> None:
        node = self._elements[ref]
        await self._send("DOM.focus", {"backendNodeId": node})
        for ch in text:
            await self._send("Input.dispatchKeyEvent", {"type": "char", "text": ch})
            await asyncio.sleep(random.uniform(0.03, 0.09))

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
        """Fill and submit a site's login form from fnox, on the host."""
        await self._ready()
        conf = self.logins.get(site)
        if conf is None:
            raise LookupError(f"no login configured for {site!r} (have: {sorted(self.logins)})")
        kw = {"cwd": self.fnox_cwd} if self.fnox_cwd else {}
        if conf.get("url"):
            await self.open(conf["url"])
        page = await self.snapshot()
        elements = page["elements"]
        password = next((e["ref"] for e in elements if e.get("password")), None)
        if password is None:
            raise LookupError("no password field on the page")
        user = next((e["ref"] for e in reversed(elements[:password]) if e.get("tag") == "input"
                     and e.get("type") in (None, "text", "email", "tel")), None)
        if user is not None and conf.get("username"):
            await self._type_into(user, host_secrets.secret(conf["username"], **kw))
        await self._type_into(password, host_secrets.secret(conf["password"], **kw))
        await self._press_enter()
        await asyncio.sleep(1.5)
        if conf.get("totp"):  # a second-factor page: fill the first text/number field
            page = await self.snapshot()
            code_field = next((e["ref"] for e in page["elements"] if e.get("tag") == "input"
                               and e.get("type") in (None, "text", "number", "tel")), None)
            if code_field is not None:
                await self._type_into(code_field, totp(host_secrets.secret(conf["totp"], **kw)))
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
            register_request_tool(func, browser.approvals if not browser.allowed else None)
        else:
            tool(func)


def _b() -> Browser:
    if _BROWSER is None:
        raise RuntimeError("browser tools aren't enabled")
    return _BROWSER


def request_browser(minutes: int, reason: str) -> dict:
    """Ask the human for a browser session for some minutes. Returns an approval id; browser tools work once approved.

    minutes: how long the task needs the browser
    reason: what for, for the human approving
    """
    return _b().request(minutes, reason)


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
    """Log in to a configured site with credentials the human stored (you never see them).

    site: a configured site name
    """
    return await _b().login(site)


async def browser_close() -> dict:
    """Close the browser and delete its session (cookies included)."""
    return await _b().close()


TOOLS = (request_browser, browser_open, browser_snapshot, browser_click, browser_type, browser_screenshot,
         browser_login, browser_close)
