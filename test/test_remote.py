"""
agentd.remote and the fleet skills against a real ``agentd serve`` (fake
harness): over the local socket, and over a stand-in p2claw agent driven by
the real p2claw-agent-client SDK (from a p2claw checkout, if present).
"""
import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from agentd.remote import Box, Fleet, LocalTransport, P2clawTransport, RemoteError, output_text, parse_sse
from test.test_serve import FakeHarness, FakePool, _short_tmp, no_model_lookups  # noqa: F401  (autouse fixture)

from agentd.harness import responses as harness_responses
from agentd.serve import Server, ServeConfig

SDK = Path.home() / "Documents" / "p2claw" / "libs" / "agent-client" / "python"
PEER_ID = "peer-zzz"


def test_parse_sse():
    events, rest = parse_sse(b'event: a\ndata: {"x": 1}\n\nevent: b\ndata: {"y"')
    assert events == [{"x": 1}] and rest == b'event: b\ndata: {"y"'


async def _fake_p2claw_agent(sock: Path, peers_sock: Path):
    """Forwards /v1/proxy/<peer>/agentd/<path> to agentd's peers socket, adding X-P2claw-Peer."""
    async def proxy(request: web.Request) -> web.StreamResponse:
        _, _, _, peer, app, *rest = request.path.split("/")
        assert app == "agentd"
        path = "/" + "/".join(rest) + (f"?{request.query_string}" if request.query_string else "")
        headers = {"X-P2claw-Peer": PEER_ID, "content-type": request.headers.get("content-type", "application/json")}
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(peers_sock))) as http:
            async with http.request(request.method, "http://agentd" + path, data=await request.read(),
                                    headers=headers) as up:
                out = web.StreamResponse(status=up.status, headers={"content-type": up.headers.get("content-type", "")})
                await out.prepare(request)
                async for chunk in up.content.iter_any():
                    await out.write(chunk)
                await out.write_eof()
                return out

    app = web.Application()
    app.router.add_route("*", "/v1/proxy/{tail:.*}", proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, str(sock)).start()
    return runner


@pytest.fixture
def served(monkeypatch):
    harness = FakeHarness()
    monkeypatch.setattr(harness_responses, "stream_response", harness)
    root = _short_tmp()
    cfg = ServeConfig(dir=root / "s", workspace_roots=[root / "ws"], claude_projects=root / "claude",
                      transcripts_root=root / "t", box_name="far")
    return harness, root, cfg


def _boxes(root: Path, cfg: ServeConfig) -> dict:
    boxes = {"here": Box("here", LocalTransport(cfg.serve_socket), drive=True)}
    if SDK.is_dir():
        sys.path.insert(0, str(SDK))
        from p2claw_agent_client import AgentClient

        client = AgentClient(socket_path=str(root / "p2claw.sock"))
        boxes["far"] = Box("far", P2clawTransport("far-box", client=client, timeout=30), drive=True)
        boxes["peek"] = Box("peek", P2clawTransport("far-box", client=client, timeout=30), drive=False)
    return boxes


def test_box_api_over_local_and_p2claw(served):
    harness, root, cfg = served

    async def main():
        server = Server(cfg, pool=FakePool(cfg))
        await server.start()
        agent = await _fake_p2claw_agent(root / "p2claw.sock", cfg.peers_socket)
        try:
            boxes = _boxes(root, cfg)
            for name, box in boxes.items():
                if not box.drive:
                    continue
                info = await box.info()
                assert info["box"] == "far" and info["you"] == ("local" if name == "here" else PEER_ID)
                r = await box.start("hello", harness="codex")
                assert output_text(r).startswith("hi #") and r["agentd"]["session"].startswith("ses_")
                sid = r["agentd"]["session"]
                events = [e async for e in box.stream("more", session_id=sid)]
                assert events[-1]["type"] == "response.completed"
                assert any(s["id"] == sid for s in await box.sessions())

                harness.gate = asyncio.Event()
                bg = await box.send(sid, "long", background=True)
                assert bg["status"] == "in_progress"
                reader = asyncio.ensure_future(_collect(box.attach(bg["id"], starting_after=0)))
                await asyncio.sleep(0.2)
                harness.gate.set()
                assert [e["sequence_number"] for e in await reader] == [1, 2, 3]
                assert (await box.response(bg["id"]))["status"] == "completed"
                harness.gate = None

                s = await box.schedule("ping", every="1h", session_id=sid)
                assert s["id"].startswith("sch_") and [x["id"] for x in await box.schedules()] == [s["id"]]
                await box.unschedule(s["id"])
                with pytest.raises(RemoteError) as e:
                    await box.send("ses_nope", "x")
                assert e.value.status == 404
            if "far" in boxes:
                # Sessions started over p2claw belong to that peer.
                owners = {s["owner"] for s in await boxes["here"].sessions()}
                assert owners == {"local", PEER_ID}
        finally:
            await agent.cleanup()
            await server.stop()

    asyncio.run(main())


def test_fleet_skills(served, tmp_path):
    harness, root, cfg = served
    from agentd import remote
    from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY

    async def main():
        server = Server(cfg, pool=FakePool(cfg))
        await server.start()
        agent = await _fake_p2claw_agent(root / "p2claw.sock", cfg.peers_socket)
        try:
            fleet = remote.enable_fleet_skills(Fleet(_boxes(root, cfg)))
            assert {"fleet_boxes", "fleet_start", "fleet_send", "fleet_read", "fleet_schedule"} <= set(FUNCTION_REGISTRY)
            assert SCHEMA_REGISTRY["fleet_sessions"]["function"]["parameters"]["properties"]["legacy"]["type"] == "boolean"
            f = FUNCTION_REGISTRY
            boxes = await f["fleet_boxes"]()
            assert all(b["reachable"] for b in boxes)
            target = "far" if "far" in fleet.boxes else "here"
            started = await f["fleet_start"](box=target, prompt="build it", workspace="proj")
            assert started["status"] == "completed" and started["reply"].startswith("hi #")
            sid = started["session"]
            sent = await f["fleet_send"](box=target, session=sid, prompt="and test it")
            assert sent["reply"].startswith("hi #")
            listed = await f["fleet_sessions"](box=target)
            assert listed[0]["id"] == sid and listed[0]["workspace"].endswith("/proj")
            assert (await f["fleet_cancel"](box=target, session=sid))["cancelled"] is False
            sch = await f["fleet_schedule"](box=target, prompt="check CI", every="0 9 * * 1-5", session=sid,
                                            timezone="America/New_York")
            assert sch["id"].startswith("sch_")
            assert (await f["fleet_unschedule"](box=target, schedule_id=sch["id"]))["removed"] == sch["id"]
            if "peek" in fleet.boxes:
                with pytest.raises(PermissionError, match="read-only"):
                    await f["fleet_send"](box="peek", session=sid, prompt="x")
                assert (await f["fleet_sessions"](box="peek"))[0]["id"] == sid, "read-only boxes can still be read"
        finally:
            remote.disable_fleet_skills()
            await agent.cleanup()
            await server.stop()

    asyncio.run(main())
    assert "fleet_send" not in FUNCTION_REGISTRY


async def _collect(it):
    return [e async for e in it]


def test_fleet_config(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({"boxes": {"local": {"local": True, "drive": True},
                                          "sabik": {"peer": "sabik-alias"}}}))
    fleet = Fleet.from_config(path, p2claw_client=object())
    assert fleet.box("local").drive and not fleet.box("sabik").drive
    assert isinstance(fleet.box("sabik").transport, P2clawTransport) and fleet.box("sabik").transport.peer == "sabik-alias"
    with pytest.raises(KeyError, match="unknown box"):
        fleet.box("vega")
    path.write_text(json.dumps({"boxes": {"bad": {}}}))
    with pytest.raises(ValueError):
        Fleet.from_config(path)
