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
            assert (await m.call("calendar", "event_delete", {"event_id": eid}))["result"] == {"deleted": eid}
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
            assert (await m.call("calendar", "event_delete", {"event_id": e["id"]}))["result"] == {"deleted": e["id"]}
        assert (await m.call("calendar", "events_list", window))["result"] == []
        from agentd.connectors.google_calendar import _api
        from agentd.connectors import Context

        ctx = Context(m, c)
        await _api(ctx, "DELETE", f"/calendars/{ctx.state['calendar_id']}")  # leave nothing behind
        print(f"deleted the test calendar; the grant stays with the p2claw agent: {m._grant('calendar')['grant_id']} "
              "(p2claw oauth-grants revoke <id> when done)", flush=True)
    asyncio.run(main())
