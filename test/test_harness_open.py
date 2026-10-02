"""
The OpenCode and omp harnesses: event parsing (shapes captured from the real
CLIs), their configs, model routing and transcripts; and, with AGENTD_LIVE=1
plus AGENTD_TEST_UPSTREAM / AGENTD_TEST_UPSTREAM_MODEL (an OpenAI-compatible
server), real turns in each sandbox backend: a tool call, resume by id in a
fresh sandbox, and switching harness mid-conversation.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from agentd.harness import routes, transcripts
from agentd.harness.omp import OmpEvents, OmpHarness
from agentd.harness.opencode import OFFLINE_ENV, OpenCodeEvents, OpenCodeHarness
from agentd.model_proxy import BearerCredentials, CodexChatGPTCredentials, ModelUpstream


def _lines(*events):
    return [json.dumps(e).encode() for e in events]


# --------------------------------------------------------------------------- #
# Event parsing
# --------------------------------------------------------------------------- #

def test_omp_events():
    p = OmpEvents()
    out = []
    for line in _lines(
        {"type": "session", "id": "omp-1", "cwd": "/w"},
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"}, {"type": "toolCall", "id": "c1", "name": "bash"}]}},
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "echo ok"}},
        {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
         "result": {"content": [{"type": "text", "text": "ok\n"}]}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
        {"type": "agent_end", "messages": []},
    ) + [b"not json"]:
        out += p.feed(line)
    out += p.finish(0)
    assert [(e.kind, e.name) for e in out] == [("tool_use", "bash"), ("tool_result", "bash"), ("text", ""), ("result", "")]
    assert out[0].data == {"command": "echo ok"} and out[0].id == out[1].id == "c1"
    assert out[1].data == [{"type": "text", "text": "ok\n"}]
    assert out[-1].text == "ok" and out[-1].session_id == "omp-1" and not out[-1].is_error

    p = OmpEvents("omp-2")
    p.feed(_lines({"type": "message_end", "message": {"role": "assistant", "content": [],
                                                      "stopReason": "error", "errorMessage": "401 bad key"}})[0])
    p.feed(_lines({"type": "agent_end"})[0])
    r = p.finish(0)[0]
    assert r.is_error and r.text == "401 bad key" and r.session_id == "omp-2"
    r = OmpEvents().finish(1)[0]
    assert r.is_error and "exited 1" in r.text


def test_opencode_events():
    p = OpenCodeEvents()
    out = []
    for line in _lines(
        {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
        {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "bash", "callID": "c1",
                                                            "state": {"status": "completed", "input": {"command": "echo ok"},
                                                                      "output": "ok\n"}}},
        {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "read", "callID": "c2",
                                                            "state": {"status": "error", "input": {"filePath": "/x"},
                                                                      "error": "no such file"}}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "tool-calls"}},
        {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "ok"}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "stop"}},
    ):
        out += p.feed(line)
    out += p.finish(0)
    assert [(e.kind, e.name) for e in out] == [("tool_use", "bash"), ("tool_result", "bash"), ("tool_use", "read"),
                                               ("tool_result", "read"), ("text", ""), ("result", "")]
    assert out[1].data == "ok\n" and not out[1].is_error and out[3].is_error and out[3].data == "no such file"
    assert out[-1].text == "ok" and out[-1].session_id == "ses_1"

    p = OpenCodeEvents()
    p.feed(_lines({"type": "error", "sessionID": "ses_2",
                   "error": {"name": "APIError", "data": {"message": "model not found"}}})[0])
    r = p.finish(0)[0]
    assert r.is_error and r.text == "model not found" and r.session_id == "ses_2"
    p = OpenCodeEvents()
    p.feed(_lines({"type": "error", "error": {"name": "APIError", "data": {"message": "Error", "statusCode": 429}}})[0])
    assert p.finish(0)[0].text == "APIError 429: Error"


# --------------------------------------------------------------------------- #
# Configs and routing
# --------------------------------------------------------------------------- #

CHAT = routes.ModelRoute("openai-chat", "http://127.0.0.1:8090", "http://127.0.0.1:8090/v1", "Qwen/Qwen3.8-27B")
CLAUDE = routes.ModelRoute("anthropic", "http://127.0.0.1:8080", "http://127.0.0.1:8080/v1", "claude-sonnet-5")


def test_omp_models_file_and_argv():
    h = OmpHarness(executor=None, config={"memory": {"backend": "off"}})
    chat = json.loads(h.models_file(CHAT))["providers"]["agentd"]
    assert chat["baseUrl"] == "http://127.0.0.1:8090/v1" and chat["api"] == "openai-completions"
    assert chat["apiKey"] == routes.PLACEHOLDER_KEY and chat["models"][0]["id"] == "Qwen/Qwen3.8-27B"
    claude = json.loads(h.models_file(CLAUDE))["providers"]["agentd"]
    assert claude["baseUrl"] == "http://127.0.0.1:8080" and claude["api"] == "anthropic-messages"
    argv = h.argv(CHAT, resume="s1", append_system_prompt="be brief", config_path="/c.yml")
    assert argv[:4] == ["omp", "-p", "--mode", "json"] and "--model" in argv
    assert argv[argv.index("--model") + 1] == "agentd/Qwen/Qwen3.8-27B"
    assert argv[argv.index("--resume") + 1] == "s1" and argv[argv.index("--config") + 1] == "/c.yml"
    assert "yolo" in argv and "--append-system-prompt" in argv


def test_opencode_config_and_argv():
    h = OpenCodeHarness(executor=None, config={"permission": {"webfetch": "deny"}, "experimental": {"x": 1}})
    cfg = h.opencode_config(CHAT, "/tmp/i.md")
    prov = cfg["provider"]["agentd"]
    assert prov["npm"] == "@ai-sdk/openai-compatible" and prov["options"]["baseURL"] == "http://127.0.0.1:8090/v1"
    assert cfg["model"] == "agentd/Qwen/Qwen3.8-27B" and cfg["instructions"] == ["/tmp/i.md"]
    assert cfg["permission"] == {"edit": "allow", "bash": "allow", "webfetch": "deny", "external_directory": "allow"}
    assert cfg["experimental"] == {"x": 1} and cfg["autoupdate"] is False
    claude = h.opencode_config(CLAUDE, None)["provider"]["agentd"]
    assert claude["npm"] == "@ai-sdk/anthropic" and claude["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert h.argv("ses_1") == ["opencode", "run", "--format", "json", "--auto", "--session", "ses_1"]
    assert OFFLINE_ENV["OPENCODE_DISABLE_AUTOUPDATE"] == "1"


class _Session:
    def __init__(self, creds):
        self.openai_credentials = creds

    async def model_upstream(self, upstream):
        return "http://127.0.0.1:8090/v1"


class _Executor:
    async def run(self, coro):
        return await coro


def test_routes():
    async def main():
        ex = _Executor()
        chatgpt, key = _Session(CodexChatGPTCredentials()), _Session(BearerCredentials("k"))
        up = ModelUpstream("http://10.0.2.58:8001/v1", api="chat")
        r = await routes.resolve(ex, chatgpt, "Qwen/Q", up, "omp")
        assert (r.api, r.origin, r.base_url, r.model) == ("openai-chat", "http://127.0.0.1:8090",
                                                         "http://127.0.0.1:8090/v1", "Qwen/Q")
        r = await routes.resolve(ex, chatgpt, "m", ModelUpstream("http://x/v1", api="responses"), "omp")
        assert r.api == "openai-responses"
        r = await routes.resolve(ex, chatgpt, "anthropic/claude-sonnet-5", None, "omp")
        assert (r.api, r.model, r.origin) == ("anthropic", "claude-sonnet-5", "http://127.0.0.1:8080")
        r = await routes.resolve(ex, key, "gpt-5.5", None, "opencode")
        assert (r.api, r.base_url) == ("openai-responses", "http://127.0.0.1:8081/v1")
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            await routes.resolve(ex, chatgpt, "gpt-5.5", None, "opencode")
        with pytest.raises(ValueError, match="model="):
            await routes.resolve(ex, chatgpt, None, up, "omp")
    asyncio.run(main())


def test_transcript_paths(tmp_path):
    assert transcripts.omp_dirname("/Users/rosey/.agentd/tmp/ws") == "--Users-rosey-.agentd-tmp-ws--"
    assert transcripts.sandbox_dir("omp", "/w/x") == "/home/agent/.omp/agent/sessions/--w-x--"
    assert transcripts.sandbox_dir("opencode", "/w/x") == "/home/agent/.agentd/opencode-sessions"
    store = tmp_path / "store"
    store.mkdir()
    (store / "2026-10-02T12-00-00Z_abc.jsonl").write_text("{}\n")
    (store / ".2026-10-02T12-00-00Z_abc.jsonl.lock.os").write_text("")
    (store / "ses_1.json").write_text("{}")
    assert [p.name for p in transcripts.session_files("omp", store, "abc")] == ["2026-10-02T12-00-00Z_abc.jsonl"]
    assert [p.name for p in transcripts.session_files("opencode", store, "ses_1")] == ["ses_1.json"]
    copied = transcripts.sync_out(store, tmp_path / "native")
    assert sorted(p.name for p in copied) == ["2026-10-02T12-00-00Z_abc.jsonl", "ses_1.json"], "lock files stay"
    assert transcripts.pull_in("opencode", store, None, "ses_1") == []


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #

UPSTREAM = os.environ.get("AGENTD_TEST_UPSTREAM")
UPSTREAM_MODEL = os.environ.get("AGENTD_TEST_UPSTREAM_MODEL")
live = pytest.mark.skipif(not (os.environ.get("AGENTD_LIVE") and UPSTREAM and UPSTREAM_MODEL),
                          reason="set AGENTD_LIVE=1, AGENTD_TEST_UPSTREAM and AGENTD_TEST_UPSTREAM_MODEL")


def _executor(backend, **kw):
    from agentd.sandbox.executor import (DockerExecutor, KrunExecutor, colima_available, docker_available,
                                         krun_available)

    ok = {"krun": krun_available, "krun-colima": colima_available, "docker": docker_available}[backend]()
    if not ok:
        pytest.skip(f"{backend} is not set up")
    if backend == "docker":
        return DockerExecutor(**kw)
    return KrunExecutor(colima=True, **kw) if backend == "krun-colima" else KrunExecutor(**kw)


@live
@pytest.mark.parametrize("backend", ["krun", "krun-colima", "docker"])
@pytest.mark.parametrize("harness", ["opencode", "omp"])
def test_live_turn_resume_and_switch(harness, backend):
    from openai import OpenAI

    from agentd.ptc import patch_openai_with_ptc
    from agentd.sandbox.base import DEFAULT_HOME

    up = ModelUpstream(UPSTREAM, api="chat", name="test")
    opts = {h: {"upstream": up, "model": UPSTREAM_MODEL} for h in ("opencode", "omp")}
    other = "omp" if harness == "opencode" else "opencode"
    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp") as tmp:
        ws = Path(tmp, "ws")
        ws.mkdir()
        kw = {"transcripts_dir": Path(tmp, "t")}
        with _executor(backend, **kw) as ex:
            client = patch_openai_with_ptc(OpenAI(api_key="unused"), cwd=ws, executor=ex, harness=harness,
                                           harness_options=opts)
            events = list(client.responses.create(
                input="Run `uname -s` in the shell and remember the code word IBIS-6. Reply with the uname output only.",
                stream=True))
            done = events[-1].response
            assert done.status == "completed" and "Linux" in done.output_text, done.output_text
            assert any(getattr(e, "type", "") == "response.output_item.done"
                       and getattr(e.item, "type", "") == "code_interpreter_call" for e in events), "tool calls stream"
            sid = done.agentd["session_id"]
            r = client.responses.create(input="Repeat the code word, just the word.", previous_response_id=done.id,
                                        harness=other)
            assert "IBIS-6" in r.output_text, "switching harness keeps the conversation"
        with _executor(backend, **kw) as ex:  # a fresh sandbox
            client = patch_openai_with_ptc(OpenAI(api_key="unused"), cwd=ws, executor=ex, harness=harness,
                                           harness_options=opts)
            r = client.responses.create(input="What was the code word? Just the word.", session_id=sid)
            assert "IBIS-6" in r.output_text and r.agentd["session_id"] == sid, "native resume in a fresh sandbox"
        files = list(Path(tmp, "t", harness).rglob("*"))
        assert any(sid in p.name for p in files), f"the session is in the transcript store: {files}"
