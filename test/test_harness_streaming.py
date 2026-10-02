"""
Harness tool calls in streams (PTC-style code_interpreter_call events), and
abandoning a stream stopping the harness. With AGENTD_LIVE=1, an abandoned
Claude Code stream must leave no process behind in the sandbox.
"""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("AGENTD_LOG_DIR", tempfile.mkdtemp(prefix="agentd-test-logs-"))

from agentd.harness import chat as hc  # noqa: E402
from agentd.harness import responses as hr  # noqa: E402
from agentd.harness.chat import HarnessConversations  # noqa: E402
from agentd.harness.events import HarnessEvent as E  # noqa: E402

SCRIPT = [
    E("text", text="Let me check."),
    E("tool_use", name="Bash", data={"command": "ls"}, id="t1"),
    E("tool_result", data="a\nb", id="t1"),
    E("tool_use", name="Read", data={"file_path": "x.txt"}, id="t2"),
    E("tool_result", data=[{"type": "text", "text": "content"}], is_error=True, id="t2"),
    E("tool_result", name="codex.error", data="Reconnecting... 1/5", is_error=True),  # no id: not a tool call
    E("text", text="Done."),
    E("result", text="Done.", session_id="s1"),
]


class ScriptedHarness:
    def __init__(self, script, delay=0.0):
        self.script, self.delay, self.closed = script, delay, False

    async def run(self, prompt, *, cwd, model, resume, append_system_prompt):
        try:
            for event in self.script:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield event
        finally:
            self.closed = True


@pytest.fixture
def patched(monkeypatch, tmp_path):
    holder = {}

    def install(harness):
        holder["h"] = harness
        conv = HarnessConversations()
        for mod in (hc, hr):
            monkeypatch.setattr(mod, "_state", lambda client_obj, executor, name: (harness, conv))

        async def no_skills(*a, **k):
            return ""
        for mod in (hc, hr):
            monkeypatch.setattr(mod, "_prepare_skills", no_skills)
        return harness
    return install


class _Client:
    pass


def _kw(**extra):
    base = dict(client_obj=_Client(), harness_name="claude-code", model=None, mcp_servers=None,
                cwd=Path("/tmp"), executor=None, server_cache={}, bridge_cache={}, skills_override=None)
    base.update(extra)
    return base


async def _collect(agen):
    return [e async for e in agen]


def test_responses_stream_items_follow_the_turn(patched):
    patched(ScriptedHarness(SCRIPT))
    events = asyncio.run(_collect(hr.stream_response(**_kw(input_data="go", kwargs={}))))
    types = [e.type for e in events]
    assert types == [
        "response.created", "response.in_progress",
        "response.output_item.added", "response.content_part.added", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done", "response.output_item.done",
        "response.output_item.added", "response.output_item.done",      # Bash
        "response.output_item.added", "response.output_item.done",      # Read
        "response.output_item.added", "response.content_part.added", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done", "response.output_item.done",
        "response.completed",
    ]
    final = events[-1].response
    assert [i.type for i in final.output] == ["message", "code_interpreter_call", "code_interpreter_call", "message"]
    bash, read = final.output[1], final.output[2]
    assert (bash.code, bash.outputs[0].logs, bash.status) == ("Bash\nls", "a\nb", "completed")
    assert read.code == 'Read\n{"file_path": "x.txt"}' and read.outputs[0].logs == "content" and read.status == "failed"
    assert final.output_text == "Let me check.\n\nDone."
    added = {e.output_index for e in events if e.type == "response.output_item.added"}
    assert added == {0, 1, 2, 3}


def test_chat_stream_interleaves_execution_events_and_display_events_renders_them(patched):
    from agentd.ptc import CodeExecution, TextDelta, display_events_async

    patched(ScriptedHarness(SCRIPT))
    chunks = asyncio.run(_collect(hc.stream_completion(**_kw(messages=[{"role": "user", "content": "go"}]))))
    executions = [c for c in chunks if getattr(c, "type", None) == "response.output_item.done"]
    assert [(x.item.code, x.item.status) for x in executions] == [
        ("Bash\nls", "completed"), ('Read\n{"file_path": "x.txt"}', "failed")]

    patched(ScriptedHarness(SCRIPT))
    stream_events = asyncio.run(_collect(hr.stream_response(**_kw(input_data="go", kwargs={}))))

    async def render():
        async def gen():
            for e in stream_events:
                yield e
        return [e async for e in display_events_async(gen())]

    rendered = asyncio.run(render())
    assert [type(e).__name__ for e in rendered if isinstance(e, (TextDelta, CodeExecution))].count("CodeExecution") == 2
    assert "".join(e.text for e in rendered if isinstance(e, TextDelta)) == "Let me check.\n\nDone."


def test_unfinished_tool_is_reported_incomplete(patched):
    patched(ScriptedHarness([E("tool_use", name="Bash", data={"command": "sleep 9"}, id="t1"),
                             E("result", text="", session_id="s1")]))
    events = asyncio.run(_collect(hr.stream_response(**_kw(input_data="go", kwargs={}))))
    item = events[-1].response.output[0]
    assert item.type == "code_interpreter_call" and item.status == "incomplete"


def test_abandoning_an_async_stream_closes_the_harness(patched):
    h = patched(ScriptedHarness(SCRIPT, delay=0.05))

    async def go():
        stream = hr.stream_response(**_kw(input_data="go", kwargs={}))
        async for event in stream:
            if event.type == "response.output_item.added":
                break
        await stream.aclose()
    asyncio.run(go())
    assert h.closed


def test_abandoning_a_sync_stream_cancels_the_background_run(patched):
    from agentd.ptc import _sync_generator_wrapper

    h = patched(ScriptedHarness(SCRIPT * 50, delay=0.05))
    t = time.monotonic()
    stream = _sync_generator_wrapper(hr.stream_response(**_kw(input_data="go", kwargs={})))
    for event in stream:
        if event.type == "response.output_item.added":
            break
    stream.close()
    assert h.closed, "the harness must be closed, not left running in the background"
    assert time.monotonic() - t < 5


# --------------------------------------------------------------------------- #
# Live: an abandoned stream leaves nothing running in the sandbox
# --------------------------------------------------------------------------- #

@pytest.fixture(params=["krun", "krun-colima", "docker"])
def live_executor(request):
    from agentd.sandbox.executor import DockerExecutor, KrunExecutor, docker_available, krun_available

    if os.environ.get("AGENTD_LIVE") != "1":
        pytest.skip("set AGENTD_LIVE=1 (makes real model calls)")
    from agentd.sandbox.executor import colima_available

    if not {"krun": krun_available, "krun-colima": colima_available, "docker": docker_available}[request.param]():
        pytest.skip(f"{request.param} sandbox not set up")
    if request.param == "krun-colima":
        return lambda **kw: KrunExecutor(colima=True, **kw)
    return KrunExecutor if request.param == "krun" else DockerExecutor


def test_live_abandoned_stream_kills_claude_and_its_tools(live_executor):
    from openai import OpenAI

    from agentd.ptc import patch_openai_with_ptc
    from agentd.sandbox.base import DEFAULT_HOME

    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp") as ws, live_executor() as ex:
        client = patch_openai_with_ptc(OpenAI(api_key="unused"), cwd=Path(ws), executor=ex, harness="claude-code")
        stream = client.responses.create(
            model="claude-sonnet-5", stream=True,
            input="Run exactly this with your Bash tool and wait for it: `sleep 120 && echo late > late.txt`")
        saw_tool = None
        for event in stream:
            if event.type == "response.output_item.added" and event.item.type == "code_interpreter_call":
                saw_tool = event.item.code
                break
        stream.close()
        assert saw_tool and "sleep 120" in saw_tool, saw_tool
        time.sleep(1.5)
        ps, _ = asyncio.run(ex.run(ex.session.sandbox.exec(["ps", "-eo", "pid,args"], user="root")))
        procs = [line.strip() for line in ps.decode().splitlines()[1:]]
        # Match on the process name, not a shell command line that merely mentions it.
        left = [p for p in procs if p.split(None, 1)[-1].split()[0].rsplit("/", 1)[-1] in ("sleep", "claude")]
        assert left == [], f"processes left in the sandbox: {left}\nall: {procs}"
        assert not Path(ws, "late.txt").exists()
