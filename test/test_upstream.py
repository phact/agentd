"""
Custom OpenAI-compatible upstreams for the Codex harness: Codex config flags,
ModelUpstream, the host proxy's Responses -> chat completions translation
(against an in-process chat server), and, with AGENTD_LIVE=1, Codex in a
sandbox using such an upstream with web search and multi-agent off.
"""
import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from agentd.harness.codex import config_args
from agentd.model_proxy import BearerCredentials, CredentialError, ModelProxy, ModelUpstream, NoCredentials


def test_config_args():
    assert config_args({"web_search": "disabled", "features": {"multi_agent": False}, "n": 3,
                        "tools": ["a", "b"], "empty": {}, "q": 'say "hi"'}) == [
        "-c", 'web_search="disabled"', "-c", "features.multi_agent=false", "-c", "n=3",
        "-c", 'tools=["a", "b"]', "-c", "empty={}", "-c", 'q="say \\"hi\\""']
    with pytest.raises(TypeError):
        config_args({"x": object()})


def test_model_upstream(monkeypatch):
    u = ModelUpstream("http://10.0.2.58:8080/v1/", api="chat")
    assert (u.origin, u.path) == ("http://10.0.2.58:8080", "/v1")
    assert isinstance(u.credentials(), NoCredentials)
    assert isinstance(ModelUpstream("https://x/v1", api_key="k").credentials(), BearerCredentials)
    monkeypatch.delenv("AGENTD_TEST_KEY", raising=False)
    with pytest.raises(CredentialError, match="AGENTD_TEST_KEY"):
        ModelUpstream("https://x/v1", api_key_env="AGENTD_TEST_KEY").credentials()
    monkeypatch.setenv("AGENTD_TEST_KEY", "k")
    assert isinstance(ModelUpstream("https://x/v1", api_key_env="AGENTD_TEST_KEY").credentials(), BearerCredentials)
    with pytest.raises(ValueError):
        ModelUpstream("http://x/v1", api="completions")
    with pytest.raises(ValueError):
        ModelUpstream("10.0.2.58:8080/v1")
    assert ModelUpstream("http://x/v1") == ModelUpstream("http://x/v1"), "hashable: one proxy per upstream"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _chat_server(seen):
    """A chat-completions-only server: a tool call first, then text using the tool's output."""
    async def chat(request):
        body = await request.json()
        seen.append({"auth": request.headers.get("authorization"), "body": body})
        tool = next((m for m in body["messages"] if m["role"] == "tool"), None)
        if tool is None:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                   "function": {"name": "exec_command", "arguments": json.dumps({"cmd": "echo ok"})}}]}
            finish = "tool_calls"
        else:
            msg, finish = {"role": "assistant", "content": f"tool said {tool['content']}"}, "stop"
        return web.json_response({"id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                                  "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                  "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def test_proxy_translates_responses_to_chat():
    async def main():
        seen = []
        server, port = await _chat_server(seen)
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "p.sock"
        proxy = ModelProxy(sock, BearerCredentials("host-key"), f"http://127.0.0.1:{port}", ("/v1/",),
                           responses_via_chat=f"http://127.0.0.1:{port}/v1")
        await proxy.start()
        try:
            tools = [{"type": "function", "name": "exec_command", "description": "run",
                      "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(sock))) as http:
                # Streaming, as Codex asks: created first, then each item, then completed.
                body = {"model": "m", "instructions": "be brief", "stream": True, "tools": tools,
                        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]}
                async with http.post("http://x/v1/responses", json=body,
                                     headers={"authorization": "Bearer sandbox-placeholder"}) as r:
                    assert r.status == 200
                    text = await r.text()
                events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
                assert [e["type"] for e in events] == ["response.created", "response.output_item.added",
                                                       "response.output_item.done", "response.completed"]
                call = events[2]["item"]
                assert call["type"] == "function_call" and call["name"] == "exec_command"
                assert json.loads(call["arguments"]) == {"cmd": "echo ok"}
                assert [e["sequence_number"] for e in events] == [0, 1, 2, 3]
                assert seen[0]["auth"] == "Bearer host-key", "the host's key replaces the sandbox's"
                assert seen[0]["body"]["messages"][0] == {"role": "system", "content": "be brief"}

                # The tool's output goes back; non-streaming this time.
                body = dict(body, stream=False, input=body["input"] + [
                    {"type": "function_call", "call_id": call["call_id"], "name": "exec_command",
                     "arguments": call["arguments"]},
                    {"type": "function_call_output", "call_id": call["call_id"], "output": "ok"}])
                async with http.post("http://x/v1/responses", json=body) as r:
                    data = await r.json()
                assert [i["type"] for i in data["output"]] == ["message"]
                assert data["output"][0]["content"][0]["text"] == "tool said ok"
                assert any(m["role"] == "tool" for m in seen[1]["body"]["messages"])

                # Other paths pass through; paths outside the base are refused.
                async with http.get("http://x/other") as r:
                    assert r.status == 403
        finally:
            await proxy.stop()
            await server.cleanup()

    asyncio.run(main())


def test_translated_upstream_errors_become_response_failed():
    async def main():
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "p.sock"
        port = _free_port()  # nothing listens here
        proxy = ModelProxy(sock, NoCredentials(), f"http://127.0.0.1:{port}", ("/v1/",),
                           responses_via_chat=f"http://127.0.0.1:{port}/v1")
        await proxy.start()
        try:
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(sock))) as http:
                async with http.post("http://x/v1/responses", json={"model": "m", "input": "hi", "stream": True}) as r:
                    text = await r.text()
            events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
            assert [e["type"] for e in events] == ["response.created", "response.failed"]
            assert events[1]["response"]["error"]["message"]
        finally:
            await proxy.stop()

    asyncio.run(main())


@pytest.mark.skipif(not os.environ.get("AGENTD_LIVE"), reason="set AGENTD_LIVE=1 for live sandbox tests")
def test_live_codex_on_a_chat_upstream():
    """Codex in a sandbox on a chat-only upstream: runs a command through it, with web search and multi-agent off."""
    from openai import OpenAI

    from agentd.ptc import patch_openai_with_ptc
    from agentd.sandbox.executor import krun_available, default_executor

    seen = []
    loop = asyncio.new_event_loop()
    server, port = loop.run_until_complete(_chat_server(seen))
    import threading
    threading.Thread(target=loop.run_forever, daemon=True).start()
    try:
        from agentd.sandbox.base import DEFAULT_HOME

        (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp") as tmp, default_executor() as ex:
            client = patch_openai_with_ptc(
                OpenAI(api_key="unused"), cwd=tmp, executor=ex, harness="codex",
                harness_options={"codex": {
                    "upstream": ModelUpstream(f"http://127.0.0.1:{port}/v1", api="chat", name="test"),
                    "model": "test-model",
                    "config": {"web_search": "disabled", "features": {"multi_agent": False}}}})
            r = client.responses.create(input="Run the command.")
            assert r.output_text.startswith("tool said"), r.output_text
            assert "ok" in r.output_text, "the command ran in the sandbox and its output went back"
        names = {t["function"]["name"] for t in seen[0]["body"]["tools"]}
        assert "exec_command" in names
        assert not names & {"web_search", "multi_agent_v1", "spawn_agent"}, names
        assert seen[0]["body"]["model"] == "test-model"
    finally:
        loop.call_soon_threadsafe(loop.stop)
