"""Android phones as agent tools, over adb on the host.

    from agentd.devices.android import Android, enable_android_skills

    phone = Android(serial="R5CT...", workspace="/path/to/workspace", apps={"com.google.android.gm"},
                    approvals=approvals)   # or allowed=True to skip the lease
    enable_android_skills(phone)

The tools run on the host (behind agentd's bridge); the sandbox never talks
to the device. Access is a lease: ``request_phone(minutes, reason)`` creates
an approval (the same webhook as egress); once approved, the phone tools work
until the lease ends. ``apps`` limits which apps ``phone_open`` may launch.
Taps are opaque (a tool can't tell "send" from "scroll"), so the real
boundaries are the lease, the app list, what the phone is logged into (use
a dedicated phone), and someone watching (e.g. scrcpy).

Screenshots are written into the workspace (``.agentd/phone/``) so the
harness can look at them (Claude Code's Read, Codex's image viewer).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KEYS = {"back": 4, "home": 3, "enter": 66, "delete": 67, "tab": 61, "menu": 82, "app_switch": 187,
        "power": 26, "volume_up": 24, "volume_down": 25, "search": 84, "escape": 111}


def find_adb() -> str | None:
    for candidate in (shutil.which("adb"),
                      os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
                      os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
                      os.path.join(os.environ.get("ANDROID_HOME", "/nonexistent"), "platform-tools", "adb")):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


class PhoneAccessError(PermissionError):
    pass


@dataclass
class Android:
    serial: str | None = None
    workspace: str | Path | None = None
    apps: set[str] | None = None          # allowed packages for phone_open (None: any)
    allowed: bool = False                 # skip the lease (host code decided)
    approvals: Any = None                 # agentd.egress.approvals.Approvals, for leases
    adb: str | None = None
    lease_until: float = 0.0
    _pending: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.adb = self.adb or find_adb()
        if not self.adb:
            raise RuntimeError("adb not found: install Android platform-tools")

    # ------------------------------------------------------------------ #

    def _run(self, *args: str, binary: bool = False, timeout: float = 30) -> Any:
        argv = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(argv, capture_output=True, timeout=timeout, text=not binary)
        if r.returncode != 0:
            err = r.stderr if not binary else r.stderr.decode(errors="replace")
            raise RuntimeError(f"adb {' '.join(args[:3])} failed: {err.strip()[:300]}")
        return r.stdout

    def devices(self) -> list[dict[str, str]]:
        out = subprocess.run([self.adb, "devices", "-l"], capture_output=True, text=True, timeout=15).stdout
        devs = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
                devs.append({"serial": parts[0], "state": parts[1], "model": info.get("model", "")})
        return devs

    # ------------------------------------------------------------------ #
    # Lease
    # ------------------------------------------------------------------ #

    def _check(self) -> None:
        if self.allowed:
            return
        if self._pending and self.approvals is not None:
            a = self.approvals.items.get(self._pending)
            if a is not None and a.status in ("once", "session", "always"):
                self.lease_until = (a.decided or time.time()) + 60 * int(a.details.get("minutes", 15))
                self._pending = None
        if time.time() < self.lease_until:
            return
        raise PhoneAccessError("no access to the phone right now: call request_phone(minutes, reason) "
                               "and wait for a human to approve it")

    def request(self, minutes: int, reason: str) -> dict[str, Any]:
        if self.allowed:
            return {"status": "allowed"}
        if self.approvals is None:
            raise PhoneAccessError("phone access needs an approver (none is configured)")
        approval = self.approvals.request("device", "", {"device": self.serial or "default",
                                                         "minutes": int(minutes)}, reason)
        self._pending = approval.id
        return {"id": approval.id, "status": approval.status}

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def screenshot(self) -> dict[str, Any]:
        self._check()
        png = self._run("exec-out", "screencap", "-p", binary=True)
        base = Path(self.workspace or ".") / ".agentd" / "phone"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"screen-{int(time.time() * 1000)}.png"
        path.write_bytes(png)
        width = int.from_bytes(png[16:20], "big") if png[:8] == b"\x89PNG\r\n\x1a\n" else None
        height = int.from_bytes(png[20:24], "big") if width else None
        return {"path": str(path), "width": width, "height": height}

    def ui(self, limit: int = 200) -> list[dict[str, Any]]:
        """On-screen elements worth acting on: text, description, id, and where to tap."""
        self._check()
        xml = self._run("exec-out", "uiautomator", "dump", "/dev/stdout")
        xml = xml[xml.find("<?xml"):] if "<?xml" in xml else xml
        xml = xml[: xml.rfind(">") + 1]
        return parse_ui(xml)[:limit]

    def tap(self, x: int, y: int) -> dict[str, Any]:
        self._check()
        self._run("shell", "input", "tap", str(int(x)), str(int(y)))
        return {"tapped": [int(x), int(y)]}

    def tap_text(self, text: str) -> dict[str, Any]:
        self._check()
        wanted = text.strip().lower()
        elements = self.ui(limit=2000)
        match = (next((e for e in elements if (e["text"] or "").lower() == wanted
                       or (e["desc"] or "").lower() == wanted), None)
                 or next((e for e in elements if wanted in (e["text"] or "").lower()
                          or wanted in (e["desc"] or "").lower()), None))
        if match is None:
            raise LookupError(f"nothing on screen says {text!r}")
        self.tap(*match["center"])
        return {"tapped": match}

    def type(self, text: str) -> dict[str, Any]:
        self._check()
        self._run("shell", "input", "text", escape_text(text))
        return {"typed": len(text)}

    def key(self, name: str) -> dict[str, Any]:
        self._check()
        code = KEYS.get(name.lower())
        if code is None:
            raise ValueError(f"unknown key {name!r}; one of {sorted(KEYS)}")
        self._run("shell", "input", "keyevent", str(code))
        return {"key": name}

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> dict[str, Any]:
        self._check()
        self._run("shell", "input", "swipe", *(str(int(v)) for v in (x1, y1, x2, y2, ms)))
        return {"swiped": [x1, y1, x2, y2]}

    def open(self, package: str) -> dict[str, Any]:
        self._check()
        if not re.fullmatch(r"[A-Za-z][\w.]*", package):
            raise ValueError(f"not a package name: {package!r}")
        if self.apps is not None and package not in self.apps:
            raise PhoneAccessError(f"{package} isn't in this phone's allowed apps: {sorted(self.apps)}")
        self._run("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        return {"opened": package}

    def list_apps(self) -> list[str]:
        self._check()
        out = self._run("shell", "pm", "list", "packages", "-3")
        pkgs = sorted(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:"))
        return [p for p in pkgs if self.apps is None or p in self.apps]


def parse_ui(xml: str) -> list[dict[str, Any]]:
    elements = []
    for node in ET.fromstring(xml).iter("node"):
        a = node.attrib
        text, desc = a.get("text") or None, a.get("content-desc") or None
        clickable = a.get("clickable") == "true"
        if not (text or desc or clickable):
            continue
        m = re.findall(r"\d+", a.get("bounds", ""))
        if len(m) != 4:
            continue
        x1, y1, x2, y2 = map(int, m)
        if x2 <= x1 or y2 <= y1:
            continue
        elements.append({"text": text, "desc": desc, "id": a.get("resource-id") or None,
                         "class": (a.get("class") or "").rsplit(".", 1)[-1], "clickable": clickable,
                         "center": [(x1 + x2) // 2, (y1 + y2) // 2], "bounds": [x1, y1, x2, y2]})
    return elements


def escape_text(text: str) -> str:
    """For `adb shell input text`: spaces as %s, shell metacharacters escaped."""
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in "()<>|;&*\\~\"'`$?#![]{}":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

_PHONE: Android | None = None


def enable_android_skills(phone: Android) -> None:
    """Register the phone_* tools (host-side, behind agentd's bridge) for ``phone``."""
    global _PHONE
    from agentd.tool_decorator import tool

    _PHONE = phone
    for func in TOOLS:
        tool(func)


def _phone() -> Android:
    if _PHONE is None:
        raise RuntimeError("phone tools aren't enabled")
    return _PHONE


def request_phone(minutes: int, reason: str) -> dict:
    """Ask the human for access to the phone for some minutes. Returns an approval id; phone tools work once approved.

    minutes: how long the task needs the phone
    reason: what for, for the human approving
    """
    return _phone().request(minutes, reason)


def phone_screenshot() -> dict:
    """Take a screenshot of the phone; returns the PNG's path (look at it with your image/file viewer)."""
    return _phone().screenshot()


def phone_ui() -> list:
    """List what's on the phone's screen: text, description, id and the center to tap."""
    return _phone().ui()


def phone_tap(x: int, y: int) -> dict:
    """Tap the phone screen at x, y (pixels).

    x: horizontal position
    y: vertical position
    """
    return _phone().tap(x, y)


def phone_tap_text(text: str) -> dict:
    """Tap the on-screen element showing this text (or with this description).

    text: the label to tap
    """
    return _phone().tap_text(text)


def phone_type(text: str) -> dict:
    """Type text into the focused field on the phone.

    text: what to type
    """
    return _phone().type(text)


def phone_key(name: str) -> dict:
    """Press a phone key: back, home, enter, delete, tab, menu, app_switch, search, escape, volume_up, volume_down.

    name: the key
    """
    return _phone().key(name)


def phone_swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> dict:
    """Swipe on the phone screen from (x1, y1) to (x2, y2).

    x1: start x
    y1: start y
    x2: end x
    y2: end y
    ms: duration in milliseconds
    """
    return _phone().swipe(x1, y1, x2, y2, ms)


def phone_open(package: str) -> dict:
    """Open an app on the phone by its package name (see phone_apps).

    package: e.g. com.google.android.gm
    """
    return _phone().open(package)


def phone_apps() -> list:
    """List the apps installed on the phone (that this agent may open)."""
    return _phone().list_apps()


TOOLS = (request_phone, phone_screenshot, phone_ui, phone_tap, phone_tap_text, phone_type, phone_key,
         phone_swipe, phone_open, phone_apps)
