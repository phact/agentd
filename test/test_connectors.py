"""
Connectors (agentd.connectors) with the native Google Calendar connector,
against a fake Calendar API and a stand-in for p2claw Connect: consent through
an approval that resolves on the provider's callback, reads free once
connected, writes approved per call with their arguments verbatim (and a
pending approval reused when the call comes back), destructive calls never
"always", a revoked grant asking to reconnect, disconnect, the audit log, the
skills. With a p2claw checkout, the same consent through the real
p2claw-agent-client against a fake agent socket.
"""
import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web

from agentd.connectors import Connector, ConnectorError, Connectors, Native, P2clawConnect
from agentd.connectors import google_calendar as gc
from agentd.egress.approvals import Approvals

SDK = Path.home() / "Documents" / "p2claw" / "libs" / "agent-client" / "python"
SCOPES = ["calendar.app.created", "calendar.events.freebusy"]


class FakeFlow:
    def __init__(self, client, provider, scopes):
        self.client, self.provider, self.scopes = client, provider, scopes
        self.granted = scopes
        self.flow_id = f"f_{len(client.flows)}"
        self.authorize_url = f"https://accounts.example/consent?flow={self.flow_id}"
        self.done = threading.Event()

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise TimeoutError("still waiting for the user")
        self.client.grants[f"gr_{self.flow_id}"] = {"scopes": self.granted}
        return {"access_token": "tok-1", "expires_in": 3600, "grant_id": f"gr_{self.flow_id}",
                "scope": " ".join(self.granted)}


class FakeConnect:
    """What P2clawConnect needs from p2claw's AgentClient."""

    def __init__(self):
        self.flows, self.grants, self.revoked, self.dead = [], {}, [], False

    def oauth_grants_connect(self, provider, scopes, store=False):
        assert store, "agentd lets the p2claw agent keep the grant"
        flow = FakeFlow(self, provider, scopes)
        self.flows.append(flow)
        return flow

    def oauth_grants_token(self, grant_id):
        if self.dead:
            err = RuntimeError("invalid_grant")
            err.status = 410
            raise err
        return {"access_token": "tok-2", "expires_in": 3600}

    def oauth_grants_revoke(self, grant_id):
        self.revoked.append(grant_id)
        return {"provider_revoked": True}


async def fake_calendar(state):
    """Enough of the Calendar API: an app-created calendar's events, and freeBusy."""
    def auth(request):
        if request.headers.get("Authorization") not in ("Bearer tok-1", "Bearer tok-2"):
            raise web.HTTPUnauthorized(text=json.dumps({"error": {"message": "bad token"}}))

    async def create_calendar(request):
        auth(request)
        body = await request.json()
        state["calendars"][f"cal{len(state['calendars'])}"] = {"summary": body["summary"], "events": {}}
        return web.json_response({"id": f"cal{len(state['calendars']) - 1}", "summary": body["summary"]})

    async def events(request):
        auth(request)
        cal = state["calendars"].get(request.match_info["cal"])
        if cal is None:
            return web.json_response({"error": {"message": "Not Found"}}, status=404)
        if request.method == "POST":
            body = await request.json()
            eid = f"ev{len(cal['events'])}"
            cal["events"][eid] = {"id": eid, **body}
            return web.json_response(cal["events"][eid])
        return web.json_response({"items": list(cal["events"].values())})

    async def event(request):
        auth(request)
        cal = state["calendars"][request.match_info["cal"]]
        eid = request.match_info["eid"]
        if request.method == "PATCH":
            cal["events"][eid].update(await request.json())
            return web.json_response(cal["events"][eid])
        if request.method == "DELETE":
            del cal["events"][eid]
            return web.Response(status=204)
        return web.json_response(cal["events"][eid])

    async def freebusy(request):
        auth(request)
        body = await request.json()
        out = {}
        for item in body["items"]:
            if item["id"] == "stranger@example.com":
                out[item["id"]] = {"busy": [], "errors": [{"reason": "notFound"}]}
            else:
                out[item["id"]] = {"busy": state["busy"].get(item["id"], [])}
        return web.json_response({"calendars": out})

    app = web.Application()
    app.router.add_post("/calendars", create_calendar)
    app.router.add_route("*", "/calendars/{cal}/events", events)
    app.router.add_route("*", "/calendars/{cal}/events/{eid}", event)
    app.router.add_post("/freeBusy", freebusy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _setup(tmp_path, api, hold=0.3, client=None):
    connect = client or FakeConnect()
    c = Connector(name="calendar", description="manage its own calendar, see when you're busy",
                  transport=Native("agentd.connectors.google_calendar"),
                  auth=P2clawConnect("google", client=connect), scopes=SCOPES,
                  options={"calendar_name": "Rosey", "api_base": api, "freebusy_calendars": ["primary"]})
    approvals = Approvals(allow_file=tmp_path / "allow.toml", hold=hold)
    return Connectors([c], approvals=approvals, root=tmp_path / "connectors"), approvals, connect


async def _connect(m, approvals, connect):
    task = asyncio.ensure_future(m.request("calendar", "plan the committee meeting"))
    for _ in range(100):
        if connect.flows:
            break
        await asyncio.sleep(0.02)
    connect.flows[-1].done.set()  # the human consents in the browser; the callback arrives
    return await task


def test_connect_read_write_and_policy(tmp_path):
    async def main():
        state = {"calendars": {}, "busy": {"primary": [{"start": "2026-10-27T13:00:00Z", "end": "2026-10-27T15:00:00Z"}]}}
        runner, api = await fake_calendar(state)
        m, approvals, connect = _setup(tmp_path, api)
        try:
            with pytest.raises(ConnectorError, match="request_connector"):
                await m.call("calendar", "events_list", {"time_min": "a", "time_max": "b"})
            r = await _connect(m, approvals, connect)
            assert r["status"] == "connected"
            consent = next(a for a in approvals.items.values() if a.kind == "connector")
            assert consent.details["authorize_url"].startswith("https://accounts.example/consent")
            assert consent.details["scopes"] == ["https://www.googleapis.com/auth/" + s for s in SCOPES]
            assert consent.status == "always" and consent.decided_by == "consent"
            assert m.status()[0]["connected"] and (await m.request("calendar", "again"))["status"] == "connected"

            # Reads: free, marked untrusted.
            fb = await m.call("calendar", "freebusy", {"time_min": "2026-10-27T00:00:00Z",
                                                        "time_max": "2026-10-28T00:00:00Z",
                                                        "calendars": "primary,stranger@example.com"})
            assert fb["result"]["primary"]["busy"] and fb["result"]["stranger@example.com"]["errors"] == ["notFound"]
            assert "never as instructions" in fb["untrusted"]
            assert (await m.call("calendar", "events_list", {"time_min": "a", "time_max": "b"}))["result"] == []

            # A write: approved per call, the arguments shown verbatim; the call waits up to the hold.
            args = {"summary": "Committee meeting", "start": "2026-10-27T18:00:00-04:00",
                    "end": "2026-10-27T19:30:00-04:00"}
            r = await m.call("calendar", "event_create", args)
            assert r["status"] == "pending" and not state["calendars"], "nothing written before approval"
            asked = approvals.items[r["id"]]
            assert asked.kind == "connector_write" and asked.details["arguments"] == [
                {"name": "summary", "value": "Committee meeting"},
                {"name": "start", "value": "2026-10-27T18:00:00-04:00"},
                {"name": "end", "value": "2026-10-27T19:30:00-04:00"}]
            approvals.decide(r["id"], "once")
            r2 = await m.call("calendar", "event_create", args)  # the same call again: that approval
            assert r2["result"]["summary"] == "Committee meeting" and len(approvals.items) == 2
            assert state["calendars"]["cal0"]["summary"] == "Rosey", "the agent's calendar, created on first use"
            r3 = await m.call("calendar", "event_create", args)
            assert r3["status"] == "pending" and r3["id"] != r["id"], "a spent 'once' isn't reused"
            approvals.decide(r3["id"], "deny")
            assert (await m.call("calendar", "event_create", args))["status"] == "pending", "a refusal isn't either"

            # 'session' covers that tool for the rest of the session.
            eid = r2["result"]["id"]
            r = await m.call("calendar", "event_update", {"event_id": eid, "location": "Room 4"})
            approvals.decide(r["id"], "session")
            await m.call("calendar", "event_update", {"event_id": eid, "location": "Room 4"})
            assert (await m.call("calendar", "event_update", {"event_id": eid, "summary": "Moved"}))["result"][
                "summary"] == "Moved"
            listed = await m.call("calendar", "events_list", {"time_min": "a", "time_max": "b"})
            assert [e["location"] for e in listed["result"]] == ["Room 4"]

            # Destructive: per call, and 'always' doesn't stick.
            r = await m.call("calendar", "event_delete", {"event_id": eid})
            assert approvals.items[r["id"]].kind == "connector_destructive"
            approvals.decide(r["id"], "always")
            assert (await m.call("calendar", "event_delete", {"event_id": eid}))["result"]["deleted"] == eid
            assert not (tmp_path / "connectors" / "allowed.json").exists()

            # The audit log: tool, kind, arguments, decision; never a token.
            audit = (tmp_path / "connectors" / "audit.jsonl").read_text()
            assert '"tool": "event_create"' in audit and "tok-1" not in audit and "tok-2" not in audit

            # The provider stopped honouring the grant: connect again.
            connect.dead = True
            m._tokens.clear()
            with pytest.raises(ConnectorError, match="connect it again"):
                await m.call("calendar", "events_list", {"time_min": "a", "time_max": "b"})
            assert not m.status()[0]["connected"]
        finally:
            await runner.cleanup()
    asyncio.run(main())


def test_always_writes_persist_and_disconnect(tmp_path):
    async def main():
        state = {"calendars": {}, "busy": {}}
        runner, api = await fake_calendar(state)
        m, approvals, connect = _setup(tmp_path, api)
        try:
            await _connect(m, approvals, connect)
            args = {"summary": "Gym", "start": "2026-10-28", "end": "2026-10-29"}
            approvals.decide((await m.call("calendar", "event_create", args))["id"], "always")
            await m.call("calendar", "event_create", args)
            again, _, _ = _setup(tmp_path, api, client=connect)  # a later agentd
            r = await again.call("calendar", "event_create", {**args, "summary": "Gym 2"})
            assert r["result"]["summary"] == "Gym 2" and r["result"]["start"] == "2026-10-28"
            assert (await m.disconnect("calendar"))["provider_revoked"] and connect.revoked
            assert not m.status()[0]["connected"]
        finally:
            await runner.cleanup()
    asyncio.run(main())


def test_consent_needs_the_callback(tmp_path):
    async def main():
        m, approvals, connect = _setup(tmp_path, "http://unused", hold=0.2)
        r = await m.request("calendar", "x")
        assert r["status"] == "pending" and r["authorize_url"].startswith("https://accounts.example")
        approvals.decide(r["id"], "once")  # tapping approve in the approver isn't consenting at Google
        r2 = await m.request("calendar", "x")
        assert r2["status"] == "pending" and "consent" in r2["message"] and not m.status()[0]["connected"]
        assert r2["id"] == r["id"] and len(connect.flows) == 1, "the same consent link, not a new flow"
        connect.flows[-1].done.set()
        for _ in range(100):
            if m.status()[0]["connected"]:
                break
            await asyncio.sleep(0.05)
        assert m.status()[0]["connected"]
    asyncio.run(main())


def test_unticked_scope(tmp_path):
    async def main():
        state = {"calendars": {}, "busy": {}}
        runner, api = await fake_calendar(state)
        m, approvals, connect = _setup(tmp_path, api)
        try:
            task = asyncio.ensure_future(m.request("calendar", "x"))
            for _ in range(100):
                if connect.flows:
                    break
                await asyncio.sleep(0.02)
            connect.flows[-1].granted = ["https://www.googleapis.com/auth/calendar.app.created"]  # freebusy unticked
            connect.flows[-1].done.set()
            await task
            assert m.status()[0]["missing"] == ["https://www.googleapis.com/auth/calendar.events.freebusy"]
            with pytest.raises(ConnectorError, match="wasn't granted"):
                await m.call("calendar", "freebusy", {"time_min": "a", "time_max": "b"})
            assert (await m.call("calendar", "events_list", {"time_min": "a", "time_max": "b"}))["result"] == []
            # Asking again starts a consent for what's missing.
            task = asyncio.ensure_future(m.request("calendar", "need availability too"))
            for _ in range(100):
                if len(connect.flows) == 2:
                    break
                await asyncio.sleep(0.02)
            connect.flows[-1].done.set()
            assert (await task)["status"] == "connected" and m.status()[0]["missing"] == []
            await m.call("calendar", "freebusy", {"time_min": "a", "time_max": "b", "calendars": "primary"})
        finally:
            await runner.cleanup()
    asyncio.run(main())


def test_recurrence_text_checks_and_split_rule():
    d = gc.describe_recurrence
    assert d("RRULE:FREQ=WEEKLY;BYDAY=SU;COUNT=10") == "weekly on Sundays, 10 times"
    assert d("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == "weekly on weekdays"
    assert d("RRULE:FREQ=MONTHLY;BYDAY=2TU;UNTIL=20261231") == "monthly on the second Tuesday, until Dec 31, 2026"
    assert d("RRULE:FREQ=MONTHLY;BYDAY=-1FR") == "monthly on the last Friday"
    assert d("RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=SA,SU") == "every other week on Saturdays and Sundays"
    assert d("RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1") == "yearly on March 1"
    assert d("RRULE:FREQ=DAILY;COUNT=3\nEXDATE;TZID=America/New_York:20261102T180000") == \
        "daily, 3 times, with some dates skipped"
    assert d("none") == "doesn't repeat"
    with pytest.raises(gc.CalendarError, match="RFC 5545"):
        gc._rules("every sunday")
    # Checked before anyone is asked to approve.
    with pytest.raises(gc.CalendarError, match="time_zone"):
        gc.check("event_create", {"start": "2026-10-11T18:00:00", "recurrence": "RRULE:FREQ=DAILY"})
    gc.check("event_create", {"start": "2026-10-11", "recurrence": "RRULE:FREQ=DAILY"})  # all day: no zone needed
    with pytest.raises(gc.CalendarError, match="series"):
        gc.check("event_update", {"scope": "this", "recurrence": "RRULE:FREQ=DAILY"})
    with pytest.raises(gc.CalendarError, match="scope"):
        gc.check("event_delete", {"scope": "everything"})
    assert gc.each_time("event_update", {"scope": "series"}) and gc.each_time("event_update", {"scope": "following"})
    assert not gc.each_time("event_update", {"scope": "this"})
    # Ending a series just before an occurrence: COUNT dropped, UNTIL in UTC (timed) or the day before (all day).
    parts = {"FREQ": "WEEKLY", "BYDAY": "SU", "COUNT": "10"}
    assert gc._ended_before(parts, {"dateTime": "2026-11-01T18:00:00-05:00"}) == \
        "RRULE:FREQ=WEEKLY;BYDAY=SU;UNTIL=20261101T225959Z"
    assert gc._ended_before(parts, {"date": "2026-11-01"}) == "RRULE:FREQ=WEEKLY;BYDAY=SU;UNTIL=20261031"


def test_one_occurrence_needs_an_occurrence(tmp_path, monkeypatch):
    """scope this/following with a series id is refused (before any approval) unless original_start names
    the date; the card and the action resolve the same target."""
    events = {
        "S": {"id": "S", "summary": "Committee", "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=SU;COUNT=6"],
              "start": {"dateTime": "2026-10-11T18:00:00-04:00", "timeZone": "America/New_York"}},
        "S_2": {"id": "S_2", "summary": "Committee", "recurringEventId": "S",
                "originalStartTime": {"dateTime": "2026-10-18T18:00:00-04:00"},
                "start": {"dateTime": "2026-10-18T18:00:00-04:00"}},
        "ONE": {"id": "ONE", "summary": "Dentist", "start": {"dateTime": "2026-10-20T09:00:00-04:00"}},
    }
    calls = []

    async def own(ctx, create):
        return "cal"

    async def api(ctx, method, path, *, params=None, body=None):
        calls.append((method, path, params))
        if method == "GET" and path.endswith("/instances"):
            return {"items": [events["S_2"]] if gc._same_time(params["originalStart"], "2026-10-18T22:00:00Z") else []}
        if method == "GET":
            return events[path.rsplit("/", 1)[1]]
        return {}
    monkeypatch.setattr(gc, "_own", own)
    monkeypatch.setattr(gc, "_api", api)

    async def main():
        with pytest.raises(gc.CalendarError, match="series id"):
            await gc._resolve(None, "S", "this")
        with pytest.raises(gc.CalendarError, match="series id"):
            await gc._resolve(None, "S", "following")
        series, occ = await gc._resolve(None, "S", "this", "2026-10-18T18:00:00-04:00")
        assert occ["id"] == "S_2" and series["id"] == "S"
        assert (await gc._resolve(None, "S_2", "this"))[1]["id"] == "S_2"
        with pytest.raises(gc.CalendarError, match="not 2026-10-25"):
            await gc._resolve(None, "S_2", "this", "2026-10-25T18:00:00-04:00")
        with pytest.raises(gc.CalendarError, match="no occurrence"):
            await gc._resolve(None, "S", "this", "2026-10-19T18:00:00-04:00")
        assert (await gc._resolve(None, "S", "series"))[1] is None
        assert (await gc._resolve(None, "ONE", "this")) == (events["ONE"], None), "a single event is just itself"

        # Through the manager: refused before an approval exists; with original_start, the card names that
        # date and the delete hits that occurrence, not the series.
        m, approvals, _ = _setup(tmp_path, "http://unused")
        m._write("grants.json", {"calendar": {"grant_id": "g", "provider": "google",
                                              "scopes": ["https://www.googleapis.com/auth/" + s for s in SCOPES]}})
        m._tokens["calendar"] = ("tok", time.time() + 3600)
        with pytest.raises(gc.CalendarError, match="series id"):
            await m.call("calendar", "event_delete", {"event_id": "S", "scope": "this"})
        with pytest.raises(gc.CalendarError, match="series id"):
            await m.call("calendar", "event_update", {"event_id": "S", "scope": "this", "summary": "x"})
        assert not approvals.items, "nothing was put in front of the human"
        args = {"event_id": "S", "scope": "this", "original_start": "2026-10-18T18:00:00-04:00"}
        r = await m.call("calendar", "event_delete", args)
        assert approvals.items[r["id"]].details["summary"] == \
            "Cancel 'Committee' on Sun Oct 18, 6:00 PM only (the rest of the series stays)"
        approvals.decide(r["id"], "once")
        await m.call("calendar", "event_delete", args)
        assert ("DELETE", "/calendars/cal/events/S_2", {"sendUpdates": "none"}) in calls, "that occurrence, nobody emailed"
        assert not any(c[0] == "DELETE" and c[1].endswith("/S") for c in calls), "never the series"
    asyncio.run(main())


def test_suggest_time(tmp_path):
    async def main():
        state = {"calendars": {}, "busy": {
            "primary": [{"start": "2026-10-27T13:00:00Z", "end": "2026-10-27T14:00:00Z"}],
            "partner@example.com": [{"start": "2026-10-27T14:30:00Z", "end": "2026-10-27T15:10:00Z"}]}}
        runner, api = await fake_calendar(state)
        m, approvals, connect = _setup(tmp_path, api)
        try:
            await _connect(m, approvals, connect)
            r = await m.call("calendar", "suggest_time", {
                "duration_minutes": 30, "window_start": "2026-10-27T12:40:00Z", "window_end": "2026-10-27T16:00:00Z",
                "calendars": "primary,partner@example.com", "count": 3})
            # 12:45 runs into the 13:00 block; 14:00-14:30 fits before the partner's 14:30; then 15:15
            # (after 15:10, on the quarter hour); slots don't overlap, and 15:45 would end after 16:00.
            assert [s["start"] for s in r["result"]] == ["2026-10-27T14:00:00+00:00", "2026-10-27T15:15:00+00:00"]
        finally:
            await runner.cleanup()
    asyncio.run(main())


def test_skills(tmp_path):
    from agentd.connectors import enable_connector_skills
    from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY

    m, approvals, _ = _setup(tmp_path, "http://unused")
    names = enable_connector_skills(m)
    try:
        assert {"request_connector", "connectors_status", "calendar_freebusy", "calendar_event_create"} <= set(names)
        create = SCHEMA_REGISTRY["calendar_event_create"]["function"]
        assert "ctx" not in create["parameters"]["properties"] and create["parameters"]["required"] == [
            "summary", "start", "end"]
        assert "approve each call" in create["description"]
        assert "approve" not in SCHEMA_REGISTRY["calendar_freebusy"]["function"]["description"].split("\n")[0]
        props = SCHEMA_REGISTRY["calendar_suggest_time"]["function"]["parameters"]["properties"]
        assert props["duration_minutes"]["type"] == "integer"
        assert FUNCTION_REGISTRY["connectors_status"]()[0]["name"] == "calendar"
    finally:
        for n in names:
            FUNCTION_REGISTRY.pop(n, None)
            SCHEMA_REGISTRY.pop(n, None)


@pytest.mark.skipif(not SDK.is_dir(), reason="needs a p2claw checkout (p2claw-agent-client from source)")
def test_consent_through_the_real_agent_client(tmp_path):
    """p2claw-agent-client against a fake agent socket: PKCE, the nonce in the callback's state, exchange with store."""
    sys.path.insert(0, str(SDK))
    from p2claw_agent_client import AgentClient

    sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="p2c-")) / "agent.sock"
    seen = {}

    async def main():
        async def start(request):
            body = await request.json()
            seen["start"] = body
            return web.json_response({"flow_id": "f_1", "authorize_url": "https://accounts.google.com/o/oauth2/auth?x"})

        async def wait(request):
            if not seen.get("consented"):
                return web.json_response({"status": "pending"})
            claims = base64.urlsafe_b64encode(json.dumps({"nonce_hash": seen["start"]["nonce_hash"]}).encode())
            return web.json_response({"status": "ready", "code": "c", "state": f"s1.{claims.decode().rstrip('=')}.sig"})

        async def exchange(request):
            body = await request.json()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).decode().rstrip("=")
            assert challenge == seen["start"]["code_challenge"] and body["store"] is True
            return web.json_response({"access_token": "tok-1", "expires_in": 3600, "grant_id": "gr_1"})

        async def token(request):
            return web.json_response({"access_token": "tok-2", "expires_in": 3600})

        app = web.Application()
        app.router.add_post("/v1/oauth-grants/flows", start)
        app.router.add_get("/v1/oauth-grants/flows/{id}", wait)
        app.router.add_post("/v1/oauth-grants/flows/{id}/exchange", exchange)
        app.router.add_get("/v1/oauth-grants/{id}/token", token)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.UnixSite(runner, str(sock)).start()
        try:
            client = AgentClient(socket_path=str(sock))
            m, approvals, _ = _setup(tmp_path, "http://unused", client=client, hold=10)
            task = asyncio.ensure_future(m.request("calendar", "x"))
            await asyncio.sleep(0.5)
            assert seen["start"]["provider"] == "google" and seen["start"]["scopes"][0].endswith("calendar.app.created")
            seen["consented"] = True
            assert (await task)["status"] == "connected"
            assert await m.token("calendar") == "tok-1"
            m._tokens.clear()
            assert await m.token("calendar") == "tok-2"
        finally:
            await runner.cleanup()
    asyncio.run(main())


@pytest.mark.skipif(not __import__("os").environ.get("AGENTD_CONNECT_LIVE") or not SDK.is_dir(),
                    reason="set AGENTD_CONNECT_LIVE=1 (real Google through the running p2claw agent; run with -s "
                           "and open the printed link to consent)")
def test_live_google_calendar(tmp_path):
    """Consent through p2claw Connect, then: free/busy on the human's calendar, and an event created and
    deleted on the agent's own test calendar, which is deleted at the end (it never touches the human's
    calendars). AGENTD_CONNECT_GRANT=<grant id> (and AGENTD_CONNECT_CALENDAR=<calendar id>) reuse an
    earlier run's grant (and calendar) instead of consenting again."""
    import datetime as dt
    import os

    sys.path.insert(0, str(SDK))
    from p2claw_agent_client import AgentClient

    c = Connector(name="calendar", description="agentd's live test", transport=Native("agentd.connectors.google_calendar"),
                  auth=P2clawConnect("google", client=AgentClient()), scopes=SCOPES,
                  options={"calendar_name": "agentd test"})
    approvals = Approvals(allow_file=tmp_path / "allow.toml", hold=600)  # time to consent
    m = Connectors([c], approvals=approvals, root=tmp_path / "connectors")
    if os.environ.get("AGENTD_CONNECT_GRANT"):
        m._write("grants.json", {"calendar": {"grant_id": os.environ["AGENTD_CONNECT_GRANT"], "provider": "google",
                                              "scopes": SCOPES}})
    if os.environ.get("AGENTD_CONNECT_CALENDAR"):
        m._write("state.json", {"calendar": {"calendar_id": os.environ["AGENTD_CONNECT_CALENDAR"]}})

    async def main():
        if not m.status()[0]["connected"]:
            task = asyncio.ensure_future(m.request("calendar", "agentd's live test"))
            for _ in range(100):
                consent = next((a for a in approvals.items.values() if a.kind == "connector"), None)
                if consent:
                    break
                await asyncio.sleep(0.1)
            print(f"\n\nOpen this to consent (10 minutes):\n{consent.details['authorize_url']}\n", flush=True)
            assert (await task)["status"] == "connected"
        approvals.hold = 1  # from here the test answers its own approvals
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        fb = await m.call("calendar", "freebusy", {"time_min": now.isoformat(),
                                                    "time_max": (now + dt.timedelta(days=7)).isoformat()})
        assert "primary" in fb["result"]
        print(f"busy blocks on primary this week: {len(fb['result']['primary']['busy'])}", flush=True)
        m._session_tools.add(("calendar", "event_create"))
        made = await m.call("calendar", "event_create", {"summary": "agentd live test",
                                                          "start": (now + dt.timedelta(days=1)).isoformat(),
                                                          "end": (now + dt.timedelta(days=1, hours=1)).isoformat()})
        window = {"time_min": now.isoformat(), "time_max": (now + dt.timedelta(days=3)).isoformat()}
        listed = (await m.call("calendar", "events_list", window))["result"]
        assert made["result"]["id"] in [e["id"] for e in listed]
        for e in listed:  # this run's event, and any an interrupted run left
            r = await m.call("calendar", "event_delete", {"event_id": e["id"]})
            approvals.decide(r["id"], "once")
            assert (await m.call("calendar", "event_delete", {"event_id": e["id"]}))["result"]["deleted"] == e["id"]
        assert (await m.call("calendar", "events_list", window))["result"] == []
        from agentd.connectors.google_calendar import _api
        from agentd.connectors import Context

        ctx = Context(m, c)
        await _api(ctx, "DELETE", f"/calendars/{ctx.state['calendar_id']}")  # leave nothing behind
        print(f"deleted the test calendar; the grant stays with the p2claw agent: {m._grant('calendar')['grant_id']} "
              "(p2claw oauth-grants revoke <id> when done)", flush=True)
    asyncio.run(main())


@pytest.mark.skipif(not __import__("os").environ.get("AGENTD_CONNECT_LIVE") or not SDK.is_dir()
                    or not __import__("os").environ.get("AGENTD_CONNECT_GRANT"),
                    reason="set AGENTD_CONNECT_LIVE=1 and AGENTD_CONNECT_GRANT=<a grant id> (real Google)")
def test_live_google_calendar_recurrence(tmp_path):
    """Repeating events on a throwaway calendar: create, change one date, this and following, the whole
    series' pattern, cancel one date, delete; approvals carry plain-language summaries and series changes
    are asked every time. The calendar is deleted at the end."""
    import datetime as dt
    import os

    sys.path.insert(0, str(SDK))
    from p2claw_agent_client import AgentClient

    from agentd.connectors import Context

    c = Connector(name="calendar", description="agentd's live test", transport=Native("agentd.connectors.google_calendar"),
                  auth=P2clawConnect("google", client=AgentClient()), scopes=SCOPES,
                  options={"calendar_name": "agentd recurrence test", "time_zone": "America/New_York"})
    approvals = Approvals(allow_file=tmp_path / "allow.toml", hold=0.5)
    m = Connectors([c], approvals=approvals, root=tmp_path / "connectors")
    m._write("grants.json", {"calendar": {"grant_id": os.environ["AGENTD_CONNECT_GRANT"], "provider": "google",
                                          "scopes": ["https://www.googleapis.com/auth/" + s for s in SCOPES]}})
    summaries = []

    async def approved(tool, args, decision="once"):
        r = await m.call("calendar", tool, args)
        if "result" in r:
            return r["result"]
        summaries.append(approvals.items[r["id"]].details.get("summary"))
        approvals.decide(r["id"], decision)
        return (await m.call("calendar", tool, args))["result"]

    async def main():
        today = dt.date.today()
        sunday = today + dt.timedelta(days=(6 - today.weekday()) % 7 or 7)
        first = f"{sunday.isoformat()}T18:00:00"
        try:
            series = await approved("event_create", {
                "summary": "Committee", "start": first, "end": f"{sunday.isoformat()}T19:30:00",
                "time_zone": "America/New_York", "recurrence": "RRULE:FREQ=WEEKLY;BYDAY=SU;COUNT=6"})
            assert series["repeats"] == "weekly on Sundays, 6 times"
            assert summaries[-1].startswith("New repeating event 'Committee': weekly on Sundays, 6 times, starting")
            with pytest.raises(Exception, match="time_zone"):
                await m.call("calendar", "event_create", {"summary": "x", "start": first, "end": first,
                                                           "recurrence": "RRULE:FREQ=DAILY;COUNT=2"})
            window = {"time_min": f"{today.isoformat()}T00:00:00Z",
                      "time_max": f"{(sunday + dt.timedelta(weeks=10)).isoformat()}T00:00:00Z"}
            occ = (await m.call("calendar", "events_list", window))["result"]
            assert len(occ) == 6 and all(o["series_id"] == series["id"] for o in occ)

            # One date: rename the second Sunday only.
            await approved("event_update", {"event_id": occ[1]["id"], "scope": "this", "summary": "Committee (short)"},
                           decision="session")
            assert summaries[-1].startswith("Just ") and "renamed to 'Committee (short)'" in summaries[-1]
            occ = (await m.call("calendar", "events_list", window))["result"]
            assert [o["summary"] for o in occ].count("Committee (short)") == 1

            # A series id where one date is meant: refused before anyone is asked; with original_start,
            # exactly that date (Google finds the occurrence by its original start).
            asked = len(approvals.items)
            with pytest.raises(Exception, match="series id"):
                await m.call("calendar", "event_delete", {"event_id": series["id"], "scope": "this"})
            assert len(approvals.items) == asked
            await approved("event_update", {"event_id": series["id"], "scope": "this", "summary": "Committee (third)",
                                            "original_start": occ[2]["original_start"]})
            # (no new card: one-date updates were approved for the session above)
            occ = (await m.call("calendar", "events_list", window))["result"]
            assert [o["summary"] for o in occ] == ["Committee", "Committee (short)", "Committee (third)", "Committee",
                                                   "Committee", "Committee"]

            # This and following: from the 4th on, an hour later.
            fourth = occ[3]
            later = _iso_hour(fourth["start"], 19)
            split = await approved("event_update", {"event_id": fourth["id"], "scope": "following",
                                                    "start": later, "end": _iso_hour(fourth["end"], 20, 30),
                                                    "time_zone": "America/New_York"}, decision="session")
            assert summaries[-1].startswith("From ") and "Committee" in summaries[-1]
            occ = (await m.call("calendar", "events_list", window))["result"]
            assert len(occ) == 6, "3 before the split, 3 after (the count carried over)"
            assert [o["start"][11:13] for o in occ] == ["18", "18", "18", "19", "19", "19"]
            new_series = split["new_series"]["id"]

            # A session approval of event_update doesn't cover whole-series changes: asked again.
            r = await m.call("calendar", "event_update", {"event_id": new_series, "scope": "series",
                                                           "recurrence": "RRULE:FREQ=WEEKLY;BYDAY=SA;UNTIL=20271231"})
            assert r["status"] == "pending"
            assert approvals.items[r["id"]].details["summary"].endswith(
                "weekly on Sundays, 3 times → weekly on Saturdays, until Dec 31, 2027"), approvals.items[r["id"]].details
            approvals.decide(r["id"], "once")
            await m.call("calendar", "event_update", {"event_id": new_series, "scope": "series",
                                                       "recurrence": "RRULE:FREQ=WEEKLY;BYDAY=SA;UNTIL=20271231"})

            # Cancel one date of the first series, then delete the second series entirely.
            occ = (await m.call("calendar", "events_list", window))["result"]
            first_series = [o for o in occ if o["series_id"] == series["id"]]
            await approved("event_delete", {"event_id": first_series[0]["id"], "scope": "this"})
            assert summaries[-1].startswith("Cancel 'Committee' on ") and summaries[-1].endswith("(the rest of the series stays)")
            await approved("event_delete", {"event_id": new_series, "scope": "series"})
            assert summaries[-1].startswith("Delete the whole series 'Committee' (weekly on Saturdays")
            left = (await m.call("calendar", "events_list", window))["result"]
            assert len(left) == 2 and all(o["series_id"] == series["id"] for o in left)
            # End what's left after its first remaining date.
            await approved("event_delete", {"event_id": left[1]["id"], "scope": "following"})
            assert len((await m.call("calendar", "events_list", window))["result"]) == 1
        finally:
            from agentd.connectors.google_calendar import _api

            ctx = Context(m, c)
            if ctx.state.get("calendar_id"):
                await _api(ctx, "DELETE", f"/calendars/{ctx.state['calendar_id']}")
        print("\nsummaries:\n  " + "\n  ".join(s for s in summaries if s), flush=True)
    asyncio.run(main())


def _iso_hour(value: str, hour: int, minute: int = 0) -> str:
    """The same date (in its own offset) at another local time, without an offset (the time_zone applies)."""
    import datetime as dt

    d = dt.datetime.fromisoformat(value)
    return d.replace(hour=hour, minute=minute, tzinfo=None).isoformat()


@pytest.mark.skipif(not __import__("os").environ.get("AGENTD_CONNECT_LIVE") or not SDK.is_dir()
                    or not __import__("os").environ.get("AGENTD_CONNECT_GRANT"),
                    reason="set AGENTD_CONNECT_LIVE=1 and AGENTD_CONNECT_GRANT=<a grant id> (real Google)")
def test_live_google_calendar_fields(tmp_path):
    """Alerts, guests (nobody emailed), Meet, color, busy/free, visibility, status, properties, source, quick add,
    search, a series' occurrences, the calendar's own settings; approval cards say all of it. The throwaway
    calendar is deleted at the end."""
    import datetime as dt
    import os

    sys.path.insert(0, str(SDK))
    from p2claw_agent_client import AgentClient

    from agentd.connectors import Context

    c = Connector(name="calendar", description="agentd's live test", transport=Native("agentd.connectors.google_calendar"),
                  auth=P2clawConnect("google", client=AgentClient()), scopes=SCOPES,
                  options={"calendar_name": "agentd fields test", "time_zone": "America/New_York"})
    approvals = Approvals(allow_file=tmp_path / "allow.toml", hold=0.5)
    m = Connectors([c], approvals=approvals, root=tmp_path / "connectors")
    m._write("grants.json", {"calendar": {"grant_id": os.environ["AGENTD_CONNECT_GRANT"], "provider": "google",
                                          "scopes": ["https://www.googleapis.com/auth/" + s for s in SCOPES]}})
    cards = []

    async def approved(tool, args):
        r = await m.call("calendar", tool, args)
        if "result" in r:
            return r["result"]
        cards.append(approvals.items[r["id"]].details.get("summary"))
        approvals.decide(r["id"], "once")
        return (await m.call("calendar", tool, args))["result"]

    async def main():
        day = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        try:
            e = await approved("event_create", {
                "summary": "Board prep", "start": f"{day}T10:00:00", "end": f"{day}T11:00:00",
                "time_zone": "America/New_York", "reminders": "popup:10m,email:1d",
                "attendees": "agentd-guest@example.com, ?agentd-optional@example.com", "meet": "add",
                "color": "tomato", "show_as": "free", "visibility": "private",
                "properties": '{"task": "t-42"}', "source_url": "https://example.com/doc", "source_title": "Agenda"})
            assert cards[-1] == ("New event 'Board prep' on " + cards[-1].split(" on ", 1)[1].split(";")[0] +
                                 "; alerts: 10 min before (notification), 1 day before (email); guests: "
                                 "agentd-guest@example.com, agentd-optional@example.com (optional); no emails sent; "
                                 "adds a Google Meet link; color tomato; shows as free; private; tags: task=t-42; "
                                 "link: Agenda"), cards[-1]
            got = (await m.call("calendar", "event_get", {"event_id": e["id"]}))["result"]
            assert got["reminders"] == {"use_default": False, "overrides": [{"method": "popup", "minutes": 10},
                                                                            {"method": "email", "minutes": 1440}]}
            assert got["alerts"] == "10 min before (notification), 1 day before (email)"
            guests = {g["email"]: g for g in got["attendees"]}
            assert set(guests) == {"agentd-guest@example.com", "agentd-optional@example.com"}
            assert guests["agentd-optional@example.com"]["optional"] and guests["agentd-guest@example.com"]["response"]
            assert got["meet"].startswith("https://meet.google.com/") and got["conference"]["entry_points"], got
            assert got["color"] == "tomato" and got["show_as"] == "free" and got["visibility"] == "private"
            assert got["properties"] == {"task": "t-42"} and got["source"]["title"] == "Agenda"
            assert got["guests_can_invite_others"] is True and got["guests_can_modify"] is False, "defaults filled in"
            assert got["time_zone"] == "America/New_York" and not got["all_day"] and got["organizer"] and got["created"]

            # Change guests with + and -, drop Meet and the alerts, mark tentative.
            up = await approved("event_update", {"event_id": e["id"], "attendees": "+agentd-third@example.com,-agentd-optional@example.com",
                                                 "meet": "remove", "reminders": "none", "status": "tentative"})
            assert "adds agentd-third@example.com" in cards[-1] and "removes agentd-optional@example.com" in cards[-1]
            assert {g["email"] for g in up["attendees"]} == {"agentd-guest@example.com", "agentd-third@example.com"}
            assert "meet" not in up and up["reminders"] == {"use_default": False, "overrides": []}
            assert up["alerts"] == "no alerts" and up["status"] == "tentative"

            window = {"time_min": f"{day}T00:00:00-04:00", "time_max": f"{day}T23:59:00-04:00"}
            assert [x["id"] for x in (await m.call("calendar", "events_list", {**window, "query": "Board"}))["result"]] == [e["id"]]
            assert [x["id"] for x in (await m.call("calendar", "events_list", {**window, "property": "task=t-42"}))["result"]] == [e["id"]]
            assert (await m.call("calendar", "events_list", {**window, "property": "task=nope"}))["result"] == []

            quick = await approved("event_quick_add", {"text": f"Lunch with Sam {day} 1pm"})
            assert quick["summary"].startswith("Lunch with Sam") and cards[-1].startswith("New event from the text")

            weekly = await approved("event_create", {"summary": "Standup", "start": f"{day}T09:00:00",
                                                     "end": f"{day}T09:15:00", "time_zone": "America/New_York",
                                                     "recurrence": "RRULE:FREQ=DAILY;COUNT=3"})
            assert len((await m.call("calendar", "event_instances", {"series_id": weekly["id"]}))["result"]) == 3

            info = (await m.call("calendar", "calendar_info", {}))["result"]
            assert info["exists"] and info["name"] == "agentd fields test"
            renamed = await approved("calendar_update", {"name": "agentd fields test (renamed)"})
            assert renamed["name"] == "agentd fields test (renamed)" and "renamed to" in cards[-1]

            with pytest.raises(Exception, match="at most 5"):
                await m.call("calendar", "event_create", {"summary": "x", "start": day, "end": day,
                                                           "reminders": "popup:1m,popup:2m,popup:3m,popup:4m,popup:5m,popup:6m"})
            with pytest.raises(Exception, match="color is one of"):
                await m.call("calendar", "event_create", {"summary": "x", "start": day, "end": day, "color": "red"})
            # Anything Google would email people about is approved every time.
            from agentd.connectors import google_calendar as g

            assert g.each_time("event_update", {"scope": "this", "notify_guests": True})
        finally:
            from agentd.connectors.google_calendar import _api

            ctx = Context(m, c)
            if ctx.state.get("calendar_id"):
                await _api(ctx, "DELETE", f"/calendars/{ctx.state['calendar_id']}")
        print("\ncards:\n  " + "\n  ".join(x for x in cards if x), flush=True)
    asyncio.run(main())
