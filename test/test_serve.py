"""
agentd serve (agentd.serve) over its real Unix sockets, with a fake harness
stream and fake sandboxes: identity per socket, ownership, turns (plain,
streamed, background, reattach, cancel, busy), sessions, transcripts, legacy
Claude Code sessions, paging, schedules and cron. With AGENTD_LIVE=1, a real
Claude Code turn in a sandbox through the socket.
"""
import asyncio
import json
import os
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from agentd.harness import responses as harness_responses
from agentd.serve import Server, ServeConfig
from agentd.serve.paging import page_jsonl, page_list
from agentd.serve.pool import SandboxPool
from agentd.serve.schedules import Cron, parse_interval

PEER = "peer-aaa"
OTHER = "peer-bbb"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeExecutor:
    def __init__(self):
        self.session = None
        self.stops = 0

    async def stop_session(self):
        self.session = None
        self.stops += 1

    def close(self):
        self.session = None


class FakePool(SandboxPool):
    def acquire(self, workspace, image):
        key = (str(workspace), image)
        if key not in self.entries:
            from agentd.serve.pool import Entry

            workspace.mkdir(parents=True, exist_ok=True)
            self.entries[key] = Entry(workspace, image, FakeExecutor(), self._client())
        entry = self.entries[key]
        entry.active += 1
        entry.executor.session = object()  # "booted"
        return entry


class FakeHarness:
    """Stands in for agentd.harness.responses.stream_response."""

    def __init__(self):
        self.calls, self.closed, self.n = [], 0, 0
        self.gate: asyncio.Event | None = None  # when set, turns wait for it before finishing

    def __call__(self, **kw):
        self.calls.append(kw)
        self.n += 1
        n = self.n
        harness = self

        async def gen():
            rid = f"resp_{n}"
            base = {"id": rid, "object": "response", "status": "in_progress", "output": [], "agentd": {}}
            try:
                yield {"type": "response.created", "response": base, "sequence_number": 0}
                yield {"type": "response.output_text.delta", "delta": "hi ", "sequence_number": 1}
                if harness.gate is not None:
                    await harness.gate.wait()
                yield {"type": "response.output_text.delta", "delta": f"#{n}", "sequence_number": 2}
                text = f"hi #{n}"
                done = {**base, "status": "completed", "agentd": {"harness": kw["harness_name"], "session_id": f"native-{n}"},
                        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}
                yield {"type": "response.completed", "response": done, "sequence_number": 3}
            finally:
                harness.closed += 1
        return gen()


def _short_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="srv-", dir="/tmp"))  # AF_UNIX paths are short on macOS


@pytest.fixture(autouse=True)
def no_model_lookups(monkeypatch):
    """/v1/info reports harness readiness; don't call real model APIs here (see test_available.py)."""
    from agentd import availability as av

    async def fake(target, harness_options=None, refresh=False):
        return {h: av.HarnessStatus(h, True) for h in ("ptc", "claude-code", "codex", "opencode", "omp")}
    monkeypatch.setattr(av, "available_async", fake)


@pytest.fixture
def env(monkeypatch):
    harness = FakeHarness()
    monkeypatch.setattr(harness_responses, "stream_response", harness)
    root = _short_tmp()
    (root / "claude").mkdir()

    def make(**overrides):
        cfg = ServeConfig(dir=root / "s", workspace_roots=[root / "ws"], claude_projects=root / "claude",
                          transcripts_root=root / "t", box_name="testbox", idle_timeout=600, **overrides)
        return Server(cfg, pool=FakePool(cfg))
    return harness, root, make


class Client:
    def __init__(self, sock: Path, who: str | None = None):
        self.sock, self.headers = sock, ({"X-P2claw-Peer": who} if who else {})

    def session(self):
        return aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(self.sock)), headers=self.headers)

    async def call(self, method, path, **kw):
        async with self.session() as http:
            async with http.request(method, "http://agentd" + path, **kw) as r:
                return r.status, await r.json()

    async def events(self, method, path, **kw):
        async with self.session() as http:
            async with http.request(method, "http://agentd" + path, **kw) as r:
                assert r.status == 200, await r.text()
                text = await r.text()
        return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def run(make, scenario, **overrides):
    async def main():
        server = make(**overrides)
        await server.start()
        try:
            local = Client(server.config.serve_socket)
            peer = Client(server.config.peers_socket, PEER)
            other = Client(server.config.peers_socket, OTHER)
            anon = Client(server.config.peers_socket)
            return await scenario(server, local, peer, other, anon)
        finally:
            await server.stop()
    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# Sockets and identity
# --------------------------------------------------------------------------- #

def test_sockets_identity_and_info(env):
    _, root, make = env

    async def scenario(server, local, peer, other, anon):
        d = server.config.dir
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        for sock in (server.config.serve_socket, server.config.peers_socket):
            assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        status, body = await anon.call("GET", "/v1/info")
        assert status == 403 and "X-P2claw-Peer" in body["error"]["message"], "peers socket needs an identity"
        status, body = await peer.call("GET", "/v1/info")
        assert status == 200 and body["you"] == PEER and body["box"] == "testbox"
        status, body = await Client(server.config.serve_socket, "spoofed").call("GET", "/v1/info")
        assert body["you"] == "local", "the header means nothing on the local socket"

    run(make, scenario)


def test_config_refuses_roots_that_expose_agentd(tmp_path):
    from agentd.sandbox.base import DEFAULT_HOME

    with pytest.raises(ValueError, match="would expose"):
        ServeConfig(dir=tmp_path / "s", workspace_roots=[DEFAULT_HOME])
    cfg = ServeConfig(dir=tmp_path / "s", workspace_roots=[tmp_path / "ws"])
    assert cfg.resolve_workspace(None, "ses_1") == (tmp_path / "ws" / "ses_1").resolve()
    assert cfg.resolve_workspace("proj", "ses_1") == (tmp_path / "ws" / "proj").resolve()
    with pytest.raises(ValueError, match="allowed root"):
        cfg.resolve_workspace("/etc", "ses_1")
    with pytest.raises(ValueError, match="allowed root"):
        cfg.resolve_workspace("../escape", "ses_1")


# --------------------------------------------------------------------------- #
# Turns and sessions
# --------------------------------------------------------------------------- #

def test_turns_sessions_and_ownership(env):
    harness, root, make = env

    async def scenario(server, local, peer, other, anon):
        status, r1 = await peer.call("POST", "/v1/responses", json={"input": "first", "harness": "codex",
                                                                      "instructions": "be brief"})
        assert status == 200 and r1["status"] == "completed" and r1["id"] == "resp_1"
        sid = r1["agentd"]["session"]
        assert harness.calls[0]["harness_name"] == "codex"
        assert harness.calls[0]["kwargs"] == {"instructions": "be brief"}
        assert harness.calls[0]["cwd"] == root.resolve() / "ws" / sid, "a fresh workspace per new session"

        # The next turn continues from the session's last response.
        status, r2 = await peer.call("POST", "/v1/responses", json={"input": "second", "session_id": sid})
        assert status == 200 and harness.calls[1]["kwargs"] == {"previous_response_id": "resp_1"}
        assert harness.calls[1]["harness_name"] == "codex", "the session keeps its harness"
        # ...or from previous_response_id alone.
        await peer.call("POST", "/v1/responses", json={"input": "third", "previous_response_id": r2["id"]})
        assert harness.calls[2]["kwargs"]["previous_response_id"] == "resp_2"

        status, s = await other.call("GET", f"/v1/sessions/{sid}")
        assert status == 200 and s["owner"] == PEER and s["native_session_id"] == "native-3"
        assert s["title"] == "first" and s["last_response_id"] == "resp_3" and s["sandbox"] == "idle"
        status, body = await other.call("POST", "/v1/responses", json={"input": "x", "session_id": sid})
        assert status == 403, "other peers can read but not drive"
        status, _ = await local.call("POST", "/v1/responses", json={"input": "local", "session_id": sid})
        assert status == 200, "local callers can drive any session"

        status, listing = await other.call("GET", "/v1/sessions")
        assert [x["id"] for x in listing["data"]] == [sid]
        status, body = await peer.call("POST", "/v1/responses", json={"input": "x", "workspace": "/etc"})
        assert status == 400 and "allowed root" in body["error"]["message"]
        status, body = await peer.call("POST", "/v1/responses", json={"input": "x", "tools": [{"type": "function"}]})
        assert status == 400

        status, closed = await peer.call("DELETE", f"/v1/sessions/{sid}")
        assert status == 200 and closed["closed"] and closed["sandbox"] == "stopped"
        status, _ = await peer.call("POST", "/v1/responses", json={"input": "x", "session_id": sid})
        assert status == 409
        status, listing = await peer.call("GET", "/v1/sessions")
        assert listing["data"] == [] and len((await peer.call("GET", "/v1/sessions?all=1"))[1]["data"]) == 1

    run(make, scenario)
    # Sessions persist across restarts.
    server = make()
    assert len(server.store.sessions) == 1


def test_drivers_setting(env):
    _, _, make = env

    async def scenario(server, local, peer, other, anon):
        _, r = await peer.call("POST", "/v1/responses", json={"input": "a"})
        status, _ = await other.call("POST", "/v1/responses", json={"input": "b", "session_id": r["agentd"]["session"]})
        assert status == 200

    run(make, scenario, drivers=[OTHER])


def test_streaming_background_reattach_cancel_and_busy(env):
    harness, _, make = env

    async def scenario(server, local, peer, other, anon):
        events = await peer.events("POST", "/v1/responses", json={"input": "go", "stream": True})
        assert [e["type"] for e in events] == ["response.created", "response.output_text.delta",
                                               "response.output_text.delta", "response.completed"]
        sid = events[-1]["response"]["agentd"]["session"]

        # Background: returns at once; the turn keeps running.
        harness.gate = asyncio.Event()
        status, bg = await peer.call("POST", "/v1/responses", json={"input": "long", "session_id": sid, "background": True})
        assert status == 200 and bg["status"] == "in_progress"
        rid = bg["id"]
        status, body = await peer.call("POST", "/v1/responses", json={"input": "again", "session_id": sid})
        assert status == 409, "one turn at a time per session"
        status, s = await peer.call("GET", f"/v1/sessions/{sid}")
        assert s["running_response_id"] == rid and s["sandbox"] == "busy"

        async def attach():
            return await other.events("GET", f"/v1/responses/{rid}?stream=true&starting_after=0")
        reader = asyncio.ensure_future(attach())
        await asyncio.sleep(0.2)
        harness.gate.set()
        events = await reader
        assert [e["sequence_number"] for e in events] == [1, 2, 3], "replayed after 0, then live to the end"
        status, final = await other.call("GET", f"/v1/responses/{rid}")
        assert final["status"] == "completed"

        # Cancel a background turn: the harness stream is closed (killing it in the sandbox).
        harness.gate = asyncio.Event()
        closed_before = harness.closed
        _, bg = await peer.call("POST", "/v1/responses", json={"input": "stop me", "session_id": sid, "background": True})
        status, _ = await other.call("POST", f"/v1/responses/{bg['id']}/cancel")
        assert status == 403, "only drivers can cancel"
        status, cancelled = await peer.call("POST", f"/v1/responses/{bg['id']}/cancel")
        assert status == 200 and cancelled["status"] == "cancelled"
        assert harness.closed == closed_before + 1
        status, s = await peer.call("GET", f"/v1/sessions/{sid}")
        assert s["running_response_id"] is None and s["last_response_id"] == rid, "a cancelled turn doesn't advance the session"

        # A plain streamed turn is tied to its caller: hanging up cancels it.
        closed_before = harness.closed
        async with peer.session() as http:
            async with http.post("http://agentd/v1/responses", json={"input": "x", "session_id": sid, "stream": True}) as r:
                await r.content.readline()
        for _ in range(50):
            if harness.closed > closed_before:
                break
            await asyncio.sleep(0.05)
        assert harness.closed == closed_before + 1
        harness.gate.set()

    run(make, scenario)


def test_transcript_messages(env, monkeypatch):
    harness, _, make = env
    records = {"resp_2": {"harness": "codex", "session_id": "n", "messages": [
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}]}}
    monkeypatch.setattr(harness_responses, "_load", lambda client, rid: records.get(rid))

    async def scenario(server, local, peer, other, anon):
        _, r = await peer.call("POST", "/v1/responses", json={"input": "a"})
        sid = r["agentd"]["session"]
        await peer.call("POST", "/v1/responses", json={"input": "c", "session_id": sid})
        _, page = await other.call("GET", f"/v1/sessions/{sid}/transcript?limit=3")
        assert [m["content"] for m in page["data"]] == ["a", "b", "c"] and page["next_cursor"] == "3"
        _, page = await other.call("GET", f"/v1/sessions/{sid}/transcript?limit=3&cursor=3")
        assert [m["content"] for m in page["data"]] == ["d"] and page["next_cursor"] is None
        _, page = await other.call("GET", f"/v1/sessions/{sid}/transcript?limit=3&order=desc")
        assert [m["content"] for m in page["data"]] == ["d", "c", "b"] and page["next_cursor"] == "1"

    run(make, scenario)


# --------------------------------------------------------------------------- #
# Legacy Claude Code sessions and paging
# --------------------------------------------------------------------------- #

def _write_session(path: Path, prompt: str, n: int = 3):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "cwd": "/w", "message": {"role": "user", "content": "<command-name>/x</command-name>"}},
             {"type": "user", "cwd": "/w", "message": {"role": "user", "content": prompt}}]
    lines += [{"type": "assistant", "i": i} for i in range(n)]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_legacy_sessions_exclude_agentd_ones(env):
    _, root, make = env
    _write_session(root / "claude" / "-w" / "raw-1.jsonl", "fix the build")
    _write_session(root / "claude" / "-w" / "mine-1.jsonl", "sandboxed")
    _write_session(root / "t" / "claude-code" / "-ws" / "mine-1.jsonl", "sandboxed")  # agentd's store

    async def scenario(server, local, peer, other, anon):
        _, listing = await peer.call("GET", "/v1/legacy/claude-code")
        assert [(s["id"], s["title"], s["cwd"]) for s in listing["data"]] == [("raw-1", "fix the build", "/w")]
        status, _ = await peer.call("GET", "/v1/legacy/claude-code/mine-1")
        assert status == 404, "agentd's own sessions aren't legacy"
        status, _ = await peer.call("GET", "/v1/legacy/claude-code/..%2Fetc")
        assert status == 404
        _, page = await peer.call("GET", "/v1/legacy/claude-code/raw-1?limit=2")
        assert len(page["data"]) == 2 and page["next_cursor"]
        _, page = await peer.call("GET", "/v1/legacy/claude-code/raw-1?limit=2&order=desc")
        assert [r.get("i") for r in page["data"]] == [2, 1]

    run(make, scenario)


def test_page_jsonl_both_directions(tmp_path):
    path = tmp_path / "t.jsonl"
    long = "x" * 200_000  # longer than a read chunk
    rows = [{"i": i, "pad": long if i == 3 else ""} for i in range(10)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")

    got, cursor = [], None
    while True:
        page, cursor = page_jsonl(path, cursor=cursor, limit=3)
        got += page
        if cursor is None:
            break
    assert [r.get("i") for r in got[:10]] == list(range(10)) and got[10] == {"raw": "not json"}

    got, cursor = [], None
    while True:
        page, cursor = page_jsonl(path, cursor=cursor, limit=4, order="desc")
        got += page
        if cursor is None:
            break
    assert got[0] == {"raw": "not json"} and [r["i"] for r in got[1:]] == list(range(9, -1, -1))
    with pytest.raises(ValueError):
        page_jsonl(path, cursor="abc")
    assert page_list([1, 2, 3], limit=2) == ([1, 2], "2")
    assert page_list([1, 2, 3], limit=2, order="desc") == ([3, 2], "1")
    assert page_list([1, 2, 3], limit=2, order="desc", cursor="1") == ([1], None)


# --------------------------------------------------------------------------- #
# Idle sandboxes
# --------------------------------------------------------------------------- #

def test_idle_sandboxes_stop(env):
    _, _, make = env

    async def scenario(server, local, peer, other, anon):
        _, r = await peer.call("POST", "/v1/responses", json={"input": "a"})
        entry = next(iter(server.pool.entries.values()))
        await server.pool.reap_once()
        assert entry.running, "not idle long enough yet"
        entry.last_used -= 601
        await server.pool.reap_once()
        assert not entry.running and entry.executor.stops == 1
        _, s = await peer.call("GET", f"/v1/sessions/{r['agentd']['session']}")
        assert s["sandbox"] == "stopped"
        status, _ = await peer.call("POST", "/v1/responses", json={"input": "b", "session_id": s["id"]})
        assert status == 200 and entry.running, "the next turn brings it back"

    run(make, scenario)


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #

def test_cron_and_intervals():
    utc = ZoneInfo("UTC")
    t0 = datetime(2026, 10, 1, 8, 30, tzinfo=utc).timestamp()  # a Thursday

    def nxt(expr, after=t0, tz=utc):
        return datetime.fromtimestamp(Cron(expr).next_after(after, tz), tz).strftime("%a %Y-%m-%d %H:%M")

    assert nxt("0 9 * * 1-5") == "Thu 2026-10-01 09:00"
    assert nxt("*/15 * * * *") == "Thu 2026-10-01 08:45"
    assert nxt("0 9 * * 0") == "Sun 2026-10-04 09:00"
    assert nxt("0 9 * * 7") == "Sun 2026-10-04 09:00"
    assert nxt("0 0 1 * *") == "Sun 2026-11-01 00:00"
    assert nxt("0 12 13 * 5") == "Fri 2026-10-02 12:00", "day-of-month OR day-of-week"
    assert nxt("30 8 29 2 *") == "Mon 2027-03-01 08:30" or nxt("30 8 29 2 *").startswith("Tue 2028-02-29")
    ny = ZoneInfo("America/New_York")
    assert nxt("0 9 * * *", tz=ny) == "Thu 2026-10-01 09:00"
    for bad in ("* * *", "61 * * * *", "*/0 * * * *"):
        with pytest.raises(ValueError):
            Cron(bad)
    assert parse_interval("90s") == 90 and parse_interval("2h") == 7200 and parse_interval("0 9 * * *") is None


def test_feb_29_cron():
    utc = ZoneInfo("UTC")
    t = Cron("30 8 29 2 *").next_after(datetime(2026, 10, 1, tzinfo=utc).timestamp(), utc)
    assert datetime.fromtimestamp(t, utc).strftime("%Y-%m-%d %H:%M") == "2028-02-29 08:30"


def test_schedules_run_wait_persist_and_catch_up(env):
    harness, _, make = env

    async def scenario(server, local, peer, other, anon):
        sched = server.schedules
        _, r = await peer.call("POST", "/v1/responses", json={"input": "setup"})
        sid = r["agentd"]["session"]
        status, body = await other.call("POST", "/v1/schedules", json={"input": "x", "every": "1h", "session_id": sid})
        assert status == 403, "only drivers can schedule turns in a session"
        status, body = await peer.call("POST", "/v1/schedules", json={"input": "x", "every": "5s"})
        assert status == 400 and "at least" in body["error"]["message"]
        status, body = await peer.call("POST", "/v1/schedules", json={"input": "x", "every": "bogus cron"})
        assert status == 400
        status, s = await peer.call("POST", "/v1/schedules", json={"input": "ping", "every": "1h", "session_id": sid})
        assert status == 200 and s["owner"] == PEER and s["next_run"] > time.time() + 3500

        # Due, but the session is busy: the run waits.
        harness.gate = asyncio.Event()
        _, bg = await peer.call("POST", "/v1/responses", json={"input": "busy", "session_id": sid, "background": True})
        due = s["next_run"]
        await sched.tick(now=due + 1)
        assert sched.get(s["id"]).runs == []
        harness.gate.set()
        while sid in server.running:
            await asyncio.sleep(0.01)
        await sched.tick(now=due + 2)
        sch = sched.get(s["id"])
        assert len(sch.runs) == 1 and sch.runs[0]["session_id"] == sid
        assert harness.calls[-1]["input_data"] == "ping" and sch.next_run == due + 3600
        run_turn = sched.running[s["id"]]
        await run_turn.done.wait()

        # A one-shot schedule targeting a new session per run.
        status, once = await peer.call("POST", "/v1/schedules", json={"input": "once", "at": time.time() + 60,
                                                                       "workspace": "nightly", "harness": "codex"})
        assert status == 200
        await sched.tick(now=time.time() + 61)
        o = sched.get(once["id"])
        assert not o.active and o.next_run is None and o.runs[0]["session_id"] != sid
        assert harness.calls[-1]["harness_name"] == "codex" and harness.calls[-1]["cwd"].name == "nightly"
        await sched.running[once["id"]].done.wait()

        status, _ = await other.call("DELETE", f"/v1/schedules/{once['id']}")
        assert status == 403
        status, _ = await peer.call("DELETE", f"/v1/schedules/{once['id']}")
        assert status == 200
        _, listing = await other.call("GET", "/v1/schedules")
        assert [x["id"] for x in listing["data"]] == [s["id"]]
        return s["id"], due

    sid_sched, due = run(make, scenario)

    # Down for ~3.5 hours past the next run: one catch-up run, the rest counted as missed.
    server = make()
    from agentd.serve.schedules import Scheduler

    sched = Scheduler(server)
    s = sched.get(sid_sched)
    assert s.next_run == due + 3600, "schedules persist"
    sched.catch_up(now=due + 3600 * 4.5)
    assert s.missed == 3 and s.next_run == due + 3600 * 4, "the latest missed run is due now"


# --------------------------------------------------------------------------- #
# Live: a real turn in a sandbox through the socket
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not os.environ.get("AGENTD_LIVE"), reason="set AGENTD_LIVE=1 for live sandbox tests")
def test_live_claude_code_turn_over_the_socket():
    from agentd.sandbox.base import DEFAULT_HOME

    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    root = _short_tmp()
    ws_root = Path(tempfile.mkdtemp(prefix="serve-ws-", dir=DEFAULT_HOME / "tmp"))

    async def main():
        cfg = ServeConfig(dir=root / "s", workspace_roots=[ws_root], idle_timeout=600)
        server = Server(cfg)
        await server.start()
        try:
            peer = Client(cfg.peers_socket, PEER)
            events = await peer.events("POST", "/v1/responses", json={
                "input": "Run `uname -s` and remember the word OSPREY-7. Reply with the uname output only.",
                "harness": "claude-code", "model": "claude-sonnet-5", "stream": True}, timeout=aiohttp.ClientTimeout(300))
            final = events[-1]["response"]
            assert final["status"] == "completed", final
            assert "Linux" in final["output"][-1]["content"][0]["text"]
            assert any(e.get("item", {}).get("type") == "code_interpreter_call" for e in events), "tool calls stream"
            sid = final["agentd"]["session"]
            # Idle stop, then the next turn resumes the native session in a fresh sandbox.
            entry = next(iter(server.pool.entries.values()))
            entry.last_used -= 601
            await server.pool.reap_once()
            assert not entry.running
            status, r2 = await peer.call("POST", "/v1/responses", json={"input": "What was the word? Just the word.",
                                                                          "session_id": sid})
            assert status == 200 and "OSPREY-7" in r2["output"][-1]["content"][0]["text"]
            assert r2["agentd"]["session_id"] == final["agentd"]["session_id"], "native session resumed"
        finally:
            await server.stop()

    asyncio.run(main())
