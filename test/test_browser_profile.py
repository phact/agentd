"""
The base browser profile (agentd.devices.browser_profile): registrable
domains, the three-way cookie merge on Chrome's real schema (deletions carry
back, partitioned cookies stay apart, the newer row wins), IndexedDB per
origin, clone and finish, crash recovery, idle expiry, and (macOS) the
encrypted disk image.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentd.devices import browser_profile as bp

SCHEMA = ("CREATE TABLE cookies(creation_utc INTEGER NOT NULL,host_key TEXT NOT NULL,top_frame_site_key TEXT NOT NULL,"
          "name TEXT NOT NULL,value TEXT NOT NULL,encrypted_value BLOB NOT NULL,path TEXT NOT NULL,"
          "expires_utc INTEGER NOT NULL,is_secure INTEGER NOT NULL,is_httponly INTEGER NOT NULL,"
          "last_access_utc INTEGER NOT NULL,has_expires INTEGER NOT NULL,is_persistent INTEGER NOT NULL,"
          "priority INTEGER NOT NULL,samesite INTEGER NOT NULL,source_scheme INTEGER NOT NULL,"
          "source_port INTEGER NOT NULL,last_update_utc INTEGER NOT NULL,source_type INTEGER NOT NULL,"
          "has_cross_site_ancestor INTEGER NOT NULL)")
INDEX = ("CREATE UNIQUE INDEX cookies_unique_index ON cookies(host_key, top_frame_site_key, has_cross_site_ancestor, "
         "name, path, source_scheme, source_port)")


def db(path: Path, *cookies) -> Path:
    """A Cookies database with (host, name, value, updated[, top_frame_site]) rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    con.execute(INDEX)
    for c in cookies:
        host, name, value, updated, top = (*c, "")[:5]
        con.execute("INSERT INTO cookies VALUES (?,?,?,?,'',?,'/',0,1,1,0,1,1,1,0,2,443,?,0,0)",
                    (updated, host, top, name, value.encode(), updated))
    con.commit()
    con.close()
    return path


def rows(path: Path) -> set:
    con = sqlite3.connect(path)
    try:
        return {(h, n, v if isinstance(v, str) else bytes(v).decode(), t) for h, n, v, t in
                con.execute("SELECT host_key, name, encrypted_value, top_frame_site_key FROM cookies")}
    finally:
        con.close()


def test_site_of():
    assert bp.site_of("www.example.com") == "example.com"
    assert bp.site_of(".signin.example.co.uk") == "example.co.uk"
    assert bp.site_of("me.github.io") == "me.github.io", "private suffixes are their own sites"
    assert bp.site_of("127.0.0.1") == "127.0.0.1" and bp.site_of("localhost") == "localhost"


def test_merge_cookies_three_way(tmp_path):
    start = db(tmp_path / "start", (".example.com", "session", "s0", 10), ("www.example.com", "pref", "p0", 10),
               (".other.com", "o", "o0", 10), (".example.com", "embed", "e0", 10, "https://site.test"))
    base = tmp_path / "base"
    shutil.copy(start, base)
    # Meanwhile another clone merged: other.com changed, and example.com's pref was refreshed later.
    con = sqlite3.connect(base)
    con.execute("UPDATE cookies SET encrypted_value = 'o1', last_update_utc = 30 WHERE host_key = '.other.com'")
    con.execute("UPDATE cookies SET encrypted_value = 'p2', last_update_utc = 40 WHERE name = 'pref'")
    con.commit()
    con.close()
    # This clone: logged out of example.com (session deleted), set a device token, touched pref earlier
    # (20 < 40), and changed other.com (but didn't use it, so it isn't merged).
    clone = db(tmp_path / "clone", ("www.example.com", "pref", "p1", 20), (".example.com", "device", "d1", 25),
               (".other.com", "o", "o9", 50), (".example.com", "embed", "e1", 26, "https://site.test"))
    result = bp.merge_cookies(base, start, clone, sites={"example.com"})
    assert rows(base) == {
        ("www.example.com", "pref", "p2", ""),         # the base's newer row wins
        (".example.com", "device", "d1", ""),          # new in the clone
        (".other.com", "o", "o1", ""),                 # a site this session didn't use is untouched
        (".example.com", "embed", "e1", "https://site.test"),  # partitioned cookie: its own key
    }, "the deleted session cookie is gone from the base too"
    assert result["deleted"] == 1


def test_merge_cookies_clear_and_first_session(tmp_path):
    base = db(tmp_path / "base", (".bank.example", "s", "x", 1), (".shop.example", "s", "y", 1))
    clone = db(tmp_path / "clone", (".bank.example", "s", "x2", 5), (".shop.example", "s", "y2", 5))
    bp.merge_cookies(base, base, clone, sites={"bank.example", "shop.example"}, clear={"bank.example"})
    assert rows(base) == {(".shop.example", "s", "y2", "")}, "a cleared (sensitive) site leaves nothing"
    fresh = tmp_path / "fresh" / "Cookies"
    assert bp.merge_cookies(fresh, tmp_path / "none", clone, sites=set(), clear={"bank.example"})["created"]
    assert rows(fresh) == {(".shop.example", "s", "y2", "")}


def test_merge_indexeddb(tmp_path):
    base, clone = tmp_path / "base", tmp_path / "clone"
    for d, names in ((base, ["https_www.example.com_0.indexeddb.leveldb", "https_mail.example.com_0.indexeddb.leveldb",
                             "https_other.com_0.indexeddb.leveldb", "https_bank.example_0.indexeddb.leveldb"]),
                     (clone, ["https_www.example.com_0.indexeddb.leveldb", "https_other.com_0.indexeddb.leveldb"])):
        for n in names:
            (d / n).mkdir(parents=True)
            (d / n / "CURRENT").write_text(d.name)
    bp.merge_indexeddb(base, clone, sites={"example.com"}, clear={"bank.example"})
    assert (base / "https_www.example.com_0.indexeddb.leveldb" / "CURRENT").read_text() == "clone"
    assert not (base / "https_mail.example.com_0.indexeddb.leveldb").exists(), "deleted in a used site"
    assert (base / "https_other.com_0.indexeddb.leveldb" / "CURRENT").read_text() == "base", "unused site untouched"
    assert not (base / "https_bank.example_0.indexeddb.leveldb").exists(), "cleared"


def _profile_files(d: Path, *cookies):
    db(d / "Default" / "Cookies", *cookies)
    (d / "Local State").write_text("{}")
    (d / "Default" / "Preferences").write_text(json.dumps({"credentials_enable_service": True, "keep": 1}))


def test_clone_finish_recover_and_expire(tmp_path):
    p = bp.BaseProfile(root=tmp_path / "browser")
    _profile_files(p.base, (".example.com", "s", "old", 1))
    c = p.clone()
    assert (c.dir / "Default" / "Cookies").exists() and (c.dir / "agentd-start-cookies").exists()
    prefs = json.loads((c.dir / "Default" / "Preferences").read_text())
    assert prefs["credentials_enable_service"] is False and prefs["keep"] == 1, "password saving off, rest kept"
    assert json.loads((p.root / "journal.json").read_text())[c.id]["pid"] == os.getpid()
    con = sqlite3.connect(c.dir / "Default" / "Cookies")
    con.execute("UPDATE cookies SET encrypted_value = 'new', last_update_utc = 9")
    con.commit()
    con.close()
    p.note(c, used=["example.com"])
    p.finish(c)
    assert rows(p.base / "Default" / "Cookies") == {(".example.com", "s", "new", "")}
    assert not c.dir.exists() and json.loads((p.root / "journal.json").read_text()) == {}
    assert "example.com" in json.loads((p.root / "sites.json").read_text())

    # A crashed session (its agentd is gone): finished by the next clone, its sensitive site cleared.
    crashed = p.clone()
    journal = json.loads((p.root / "journal.json").read_text())
    journal[crashed.id].update(pid=999999, used=["example.com"], clear=["example.com"])
    (p.root / "journal.json").write_text(json.dumps(journal))
    nxt = p.clone()
    assert not crashed.dir.exists() and rows(p.base / "Default" / "Cookies") == set()
    p.finish(nxt)

    # Idle expiry: a site unused for 14 days is cleared when the next session starts.
    db_path = p.base / "Default" / "Cookies"
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO cookies VALUES (1,'.idle.example','','s','',X'00','/',0,1,1,0,1,1,1,0,2,443,1,0,0)")
    con.commit()
    con.close()
    (p.root / "sites.json").write_text(json.dumps({"idle.example": time.time() - 15 * 86400}))
    p.finish(p.clone())
    assert rows(db_path) == set() and "idle.example" not in json.loads((p.root / "sites.json").read_text())


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("hdiutil"), reason="macOS encrypted disk image")
def test_encrypted_base_on_macos(tmp_path):
    p = bp.BaseProfile(root=tmp_path / "browser", key="X", password=lambda: "correct horse")
    with p.mounted() as base:
        assert subprocess.run(["mount"], capture_output=True, text=True).stdout.count(str(base)) == 1
        (base / "secret.txt").write_text("cookies")
    assert not (p.base / "secret.txt").exists(), "unmounted: nothing readable"
    assert p.image.exists()
    with p.mounted() as base:
        assert (base / "secret.txt").read_text() == "cookies"
    wrong = bp.BaseProfile(root=tmp_path / "browser", key="X", password=lambda: "nope")
    with pytest.raises(RuntimeError, match="hdiutil attach"):
        with wrong.mounted():
            pass
