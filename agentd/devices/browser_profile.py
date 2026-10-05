"""A long-lived base browser profile, cloned per agent session and merged back.

    profile = BaseProfile(key="AGENTD_BROWSER_KEY")   # the disk image's password, a fnox secret
    Browser(profile=profile, ...)

Device checks trust a long-lived profile with history; a fresh profile per
session is the strongest bot signal there is. So agentd keeps one base profile
(``~/.agentd/browser/base``), never runs Chrome on it directly, and gives each
agent session its own copy (APFS copy-on-write on macOS). When the session ends,
what it changed is merged back:

  * **cookies, per site the session used** (registrable domain): a three-way
    merge against the copy taken at clone time, so deletions (a logout) carry
    back too. A cookie is Chrome's unique key (host_key, top_frame_site_key,
    has_cross_site_ancestor, name, path, source_scheme, source_port); when the
    base changed the same cookie meanwhile (another clone), the newer
    ``last_update_utc`` wins;
  * **IndexedDB, per origin** of those sites (replaced);
  * small whole files (``Local State``, ``Preferences``, ``Trust Tokens``,
    ``TransportSecurity``, ``Network Persistent State``): last writer wins.
    ``Local Storage`` (one LevelDB for every origin) isn't merged back.

Sites in ``clear`` (sensitive logins, idle sites) lose their cookies and
storage in the base instead. Between sessions the base sits in an encrypted
disk image (macOS sparse bundle; gocryptfs on Linux) whose password is a fnox
secret, mounted only to clone and to merge. A journal of live clones lets a
restarted agentd finish what a crash left (clear, merge, delete).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from agentd.sandbox.base import DEFAULT_HOME

ROOT = DEFAULT_HOME / "browser"
IDLE_DAYS = 14
COOKIE_KEY = ("host_key", "top_frame_site_key", "has_cross_site_ancestor", "name", "path", "source_scheme",
              "source_port")
# Profile files worth keeping between sessions (relative to the user-data dir).
CLONED = ("Local State", "Default/Cookies", "Default/Preferences", "Default/Local Storage", "Default/IndexedDB",
          "Default/Trust Tokens", "Default/TransportSecurity", "Default/Network Persistent State",
          "Default/WebStorage")
MERGED_FILES = ("Local State", "Default/Preferences", "Default/Trust Tokens", "Default/TransportSecurity",
                "Default/Network Persistent State")
# Chrome keeps nothing of value in the profile beyond cookies and site storage, and runs
# no site code between tasks.
PREFERENCES = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
    "signin": {"allowed": False},
    "browser": {"has_seen_welcome_page": True},
    "profile": {"password_manager_enabled": False, "exit_type": "Normal", "exited_cleanly": True,
                "default_content_setting_values": {"notifications": 2, "background_sync": 2,
                                                   "periodic_background_sync": 2}},
    # "Continue where you left off": Chrome then keeps session cookies (the ones without an
    # expiry, which many sites sign you in with) across restarts, like a person's browser. The
    # clone never copies Chrome's saved tabs, so there's nothing to reopen.
    "session": {"restore_on_startup": 1},
}

_psl = None


def site_of(host: str) -> str:
    """The registrable domain of ``host`` (``signin.example.co.uk`` -> ``example.co.uk``); the host itself
    for IPs and single-label names."""
    global _psl
    host = (host or "").lower().lstrip(".").rstrip(".")
    if _psl is None:
        import tldextract

        _psl = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
    r = _psl(host)
    domain = r.top_domain_under_public_suffix if hasattr(r, "top_domain_under_public_suffix") else r.registered_domain
    return domain or host


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #

def _rows(db: Path, sites: set[str] | None = None) -> tuple[list[str], dict[tuple, tuple]]:
    """(columns, {key: row}) of a Cookies database, optionally only for ``sites``."""
    if not db.exists():
        return [], {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(cookies)")]
        idx = [cols.index(k) for k in COOKIE_KEY if k in cols]
        out = {}
        for row in con.execute("SELECT * FROM cookies"):
            if sites is None or site_of(row[cols.index("host_key")]) in sites:
                out[tuple(row[i] for i in idx)] = row
        return cols, out
    finally:
        con.close()


def merge_cookies(base: Path, start: Path, clone: Path, sites: Iterable[str], clear: Iterable[str] = ()) -> dict:
    """Merge ``clone``'s cookies for ``sites`` into ``base`` (three-way, against ``start``, the base's
    cookies when the clone was made); drop every cookie of the ``clear`` sites. Returns counts."""
    sites, clear = set(sites) - set(clear), set(clear)
    if not clone.exists():
        return {"upserted": 0, "deleted": 0}
    if not base.exists():  # the first session: its cookies start the base
        base.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(clone, base)
        return {"upserted": len(_rows(base)[1]), "deleted": _delete_sites(base, clear), "created": True}
    cols, end = _rows(clone, sites)
    base_cols, now = _rows(base, sites | clear)
    _, before = _rows(start, sites)
    if base_cols and cols and base_cols != cols:  # Chrome changed the schema: the clone's (newer) file wins
        shutil.copy2(clone, base)
        _delete_sites(base, clear)
        return {"upserted": len(end), "deleted": 0, "replaced": True}
    upd = cols.index("last_update_utc") if "last_update_utc" in cols else None
    upserts = [row for key, row in end.items()
               if key not in now or upd is None or row[upd] >= now[key][upd]]
    deletes = [key for key, row in before.items()
               if key not in end and key in now and (upd is None or now[key][upd] <= row[upd])]
    con = sqlite3.connect(base)
    try:
        with con:
            con.executemany(f"INSERT OR REPLACE INTO cookies ({', '.join(cols)}) VALUES "
                            f"({', '.join('?' for _ in cols)})", upserts)
            keys = [k for k in COOKIE_KEY if k in cols]
            con.executemany(f"DELETE FROM cookies WHERE {' AND '.join(f'{k} = ?' for k in keys)}", deletes)
    finally:
        con.close()
    removed = _delete_sites(base, clear)
    return {"upserted": len(upserts), "deleted": len(deletes) + removed}


def _delete_sites(db: Path, sites: set[str]) -> int:
    if not sites or not db.exists():
        return 0
    con = sqlite3.connect(db)
    try:
        hosts = [h for (h,) in con.execute("SELECT DISTINCT host_key FROM cookies") if site_of(h) in sites]
        with con:
            n = sum(con.execute("DELETE FROM cookies WHERE host_key = ?", (h,)).rowcount for h in hosts)
        return n
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# IndexedDB (one directory per origin: scheme_host_port.indexeddb.leveldb / .blob)
# --------------------------------------------------------------------------- #

def _idb_site(name: str) -> str | None:
    stem = name.split(".indexeddb.")[0]
    parts = stem.split("_")
    return site_of(parts[1]) if len(parts) >= 3 else None


def merge_indexeddb(base_dir: Path, clone_dir: Path, sites: set[str], clear: set[str] = frozenset()) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    for d in list(base_dir.iterdir()):
        s = _idb_site(d.name)
        if s in clear or (s in sites and not (clone_dir / d.name).exists()):
            shutil.rmtree(d, ignore_errors=True)
    if clone_dir.is_dir():
        for d in clone_dir.iterdir():
            s = _idb_site(d.name)
            if s in sites and s not in clear:
                shutil.rmtree(base_dir / d.name, ignore_errors=True)
                shutil.copytree(d, base_dir / d.name)


# --------------------------------------------------------------------------- #
# The base profile
# --------------------------------------------------------------------------- #

def _copy(src: Path, dst: Path) -> None:
    """Copy-on-write where the filesystem can (APFS: cp -c), else a plain copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        if subprocess.run(["cp", "-c", "-R", str(src), str(dst)], capture_output=True).returncode == 0:
            return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


@dataclass
class Clone:
    dir: Path            # Chrome's --user-data-dir for this session
    id: str


@dataclass
class BaseProfile:
    """The long-lived profile agent sessions clone. ``key``: the fnox secret holding the encrypted disk
    image's password (None: a plain directory, e.g. for tests or a machine you trust)."""

    root: Path = ROOT
    key: str | None = None
    fnox_cwd: str | Path | None = None
    idle_days: float = IDLE_DAYS
    password: Callable[[], str] | None = field(default=None, repr=False)  # overrides fnox (tests)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    @property
    def base(self) -> Path:
        return self.root / "base"

    @property
    def image(self) -> Path:
        return self.root / ("base.sparsebundle" if sys.platform == "darwin" else "base.gocryptfs")

    # -- encryption ----------------------------------------------------------

    def _password(self) -> str:
        if self.password is not None:
            return self.password()
        from agentd import fnox

        return fnox.get(self.key, Path(self.fnox_cwd or os.getcwd()).resolve())  # SecretMissing if locked

    @contextlib.contextmanager
    def mounted(self) -> Iterator[Path]:
        """The base, mounted for the duration (a plain directory when there's no key)."""
        if not self.key:
            self.base.mkdir(parents=True, exist_ok=True)
            yield self.base
            return
        password = self._password()
        self.base.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            if not self.image.exists():
                _run(["hdiutil", "create", "-size", "8g", "-type", "SPARSEBUNDLE", "-fs", "APFS", "-encryption",
                      "AES-256", "-stdinpass", "-volname", "agentd-browser", str(self.image)], password + "\0")
            _run(["hdiutil", "attach", "-stdinpass", "-nobrowse", "-noautoopen", "-owners", "on",
                  "-mountpoint", str(self.base), str(self.image)], password + "\0")
            try:
                yield self.base
            finally:
                if subprocess.run(["hdiutil", "detach", str(self.base)], capture_output=True).returncode != 0:
                    subprocess.run(["hdiutil", "detach", "-force", str(self.base)], capture_output=True)
        else:
            if shutil.which("gocryptfs") is None:
                raise RuntimeError("an encrypted base profile needs gocryptfs on Linux (or BaseProfile(key=None))")
            if not (self.image / "gocryptfs.conf").exists():
                self.image.mkdir(parents=True, exist_ok=True)
                _run(["gocryptfs", "-init", "-q", str(self.image)], password + "\n")
            _run(["gocryptfs", "-q", str(self.image), str(self.base)], password + "\n")
            try:
                yield self.base
            finally:
                subprocess.run(["fusermount", "-u", str(self.base)], capture_output=True)

    # -- bookkeeping ---------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "merge.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self, name: str) -> dict:
        try:
            return json.loads((self.root / name).read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, data: dict) -> None:
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.root / name)

    def note(self, clone: Clone, **updates: Iterable[str]) -> None:
        """Record sites in the journal entry of a live clone (``used``, ``logged_in``, ``clear``)."""
        with self._lock():
            journal = self._read("journal.json")
            entry = journal.get(clone.id)
            if entry is None:
                return
            for k, v in updates.items():
                entry[k] = sorted(set(entry.get(k, [])) | set(v))
            self._write("journal.json", journal)

    # -- clone and merge -----------------------------------------------------

    def clone(self) -> Clone:
        """A fresh copy of the base for one session (finishing crashed sessions and expiring idle sites first)."""
        clones = self.root / "clones"
        clones.mkdir(parents=True, exist_ok=True)
        with self._lock(), self.mounted() as base:
            self._recover(base)
            self._expire_idle(base)
            d = Path(tempfile.mkdtemp(prefix="agentd-browser-", dir=clones))
            for rel in CLONED:
                if (base / rel).exists():
                    _copy(base / rel, d / rel)
            if (base / "Default" / "Cookies").exists():
                shutil.copy2(base / "Default" / "Cookies", d / "agentd-start-cookies")
            _apply_preferences(d / "Default" / "Preferences")
            clone = Clone(dir=d, id=d.name)
            journal = self._read("journal.json")
            journal[clone.id] = {"dir": str(d), "pid": os.getpid(), "started": time.time(), "used": [],
                                 "logged_in": [], "clear": []}
            self._write("journal.json", journal)
            return clone

    def finish(self, clone: Clone, used: Iterable[str] = (), clear: Iterable[str] = ()) -> dict:
        """Merge a closed clone back into the base (Chrome must have exited) and delete it."""
        with self._lock(), self.mounted() as base:
            entry = self._read("journal.json").get(clone.id, {})
            used = set(used) | set(entry.get("used", []))
            clear = set(clear) | set(entry.get("clear", []))
            result = self._merge(base, clone.dir, used, clear)
            self._forget(clone)
            return result

    def _merge(self, base: Path, d: Path, used: set[str], clear: set[str]) -> dict:
        result = merge_cookies(base / "Default" / "Cookies", d / "agentd-start-cookies", d / "Default" / "Cookies",
                               used, clear)
        merge_indexeddb(base / "Default" / "IndexedDB", d / "Default" / "IndexedDB", used - clear, clear)
        for rel in MERGED_FILES:
            if (d / rel).exists():
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(d / rel, base / rel)
        seen = self._read("sites.json")
        now = time.time()
        for s in used - clear:
            seen[s] = now
        for s in clear:
            seen.pop(s, None)
        self._write("sites.json", seen)
        return result

    def _forget(self, clone: Clone) -> None:
        shutil.rmtree(clone.dir, ignore_errors=True)
        journal = self._read("journal.json")
        journal.pop(clone.id, None)
        self._write("journal.json", journal)

    def _recover(self, base: Path) -> None:
        """Finish sessions whose agentd died: clear their sensitive sites, merge, delete."""
        journal = self._read("journal.json")
        for cid, entry in list(journal.items()):
            if _alive(entry.get("pid", 0)):
                continue
            d = Path(entry["dir"])
            if d.exists():
                clear = set(entry.get("clear", []))
                self._merge(base, d, set(entry.get("used", [])), clear)
            self._forget(Clone(dir=d, id=cid))

    def _expire_idle(self, base: Path) -> None:
        """Sign out of (clear) sites no session has used for ``idle_days``."""
        seen = self._read("sites.json")
        cutoff = time.time() - self.idle_days * 86400
        stale = {s for s, t in seen.items() if t < cutoff}
        if stale:
            _delete_sites(base / "Default" / "Cookies", stale)
            merge_indexeddb(base / "Default" / "IndexedDB", base / "agentd-none", set(), stale)
            self._write("sites.json", {s: t for s, t in seen.items() if s not in stale})


def _apply_preferences(path: Path) -> None:
    try:
        prefs = json.loads(path.read_text())
    except (OSError, ValueError):
        prefs = {}

    def deep(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict):
                if not isinstance(dst.get(k), dict):
                    dst[k] = {}
                deep(dst[k], v)
            else:
                dst[k] = v
    deep(prefs, PREFERENCES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs))


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _run(argv: list[str], stdin: str) -> None:
    r = subprocess.run(argv, input=stdin.encode(), capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]} {argv[1]} failed: {(r.stderr or r.stdout).decode(errors='replace').strip()[:300]}")
