"""
Tests for harness routing through the Responses API (agentd.harness.responses),
with a fake harness; and, with AGENTD_LIVE=1, Codex / Claude Code in a
sandbox (libkrun or Docker) via the patched client.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("AGENTD_LOG_DIR", tempfile.mkdtemp(prefix="agentd-test-logs-"))

from agentd.harness import responses as hr  # noqa: E402
from agentd.harness.chat import HarnessConversations  # noqa: E402
from agentd.harness.events import HarnessEvent  # noqa: E402


class FakeHarness:
    def __init__(self):
        self.calls, self.n = [], 0

    async def run(self, prompt, *, cwd, model, resume, append_system_prompt):
        self.n += 1
        self.calls.append({"prompt": prompt, "resume": resume, "system": append_system_prompt})
        yield HarnessEvent("text", text="step one")
        yield HarnessEvent("text", text=f"answer {self.n}")
        yield HarnessEvent("result", text=f"answer {self.n}", session_id="thread-1")


class Client:
    pass


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(hr, "RESPONSES_DIR", tmp_path / "responses")
    harness = FakeHarness()
    conv = HarnessConversations()
    monkeypatch.setattr(hr, "_state", lambda client_obj, executor, name: (harness, conv))

    async def no_skills(*a, **k):
        return ""
    monkeypatch.setattr(hr, "_prepare_skills", no_skills)
    return harness


def call(**overrides):
    base = dict(client_obj=overrides.pop("client"), harness_name="codex", model=None, kwargs={},
                mcp_servers=None, cwd=Path("/tmp"), executor=None, server_cache={}, bridge_cache={},
                skills_override=None)
    base.update(overrides)
    return base


def test_input_to_messages():
    msgs = hr.input_to_messages(
        [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
         {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]}],
        instructions="be terse",
    )
    assert msgs == [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "yo"}]
    assert hr.input_to_messages("q") == [{"role": "user", "content": "q"}]
    with pytest.raises(ValueError, match="message items only"):
        hr.input_to_messages([{"type": "function_call_output", "call_id": "x", "output": "1"}])


def test_non_streaming_and_previous_response_id_resumes(fake):
    client = Client()
    r1 = asyncio.run(hr.handle_response(**call(client=client, input_data="first", kwargs={"instructions": "sys"})))
    assert r1.status == "completed" and r1.output_text == "answer 1"
    assert r1.agentd["session_id"] == "thread-1" and r1.instructions == "sys"
    assert fake.calls[0]["resume"] is None and fake.calls[0]["system"] == "sys"

    r2 = asyncio.run(hr.handle_response(**call(client=client, input_data="second",
                                                 kwargs={"previous_response_id": r1.id, "instructions": "sys"})))
    assert fake.calls[1]["resume"] == "thread-1", "continuing a response resumes the native session"
    assert fake.calls[1]["prompt"] == "second" and fake.calls[1]["system"] == "sys"
    assert r2.previous_response_id == r1.id and r2.output_text == "answer 2"

    with pytest.raises(ValueError, match="unknown previous_response_id"):
        asyncio.run(hr.handle_response(**call(client=client, input_data="x", kwargs={"previous_response_id": "nope"})))
    with pytest.raises(ValueError, match="client-side tools"):
        asyncio.run(hr.handle_response(**call(client=client, input_data="x", kwargs={"tools": [{"type": "function"}]})))


class SessionsHarness(FakeHarness):
    """Each new native session gets its own id, like the real CLIs."""

    async def run(self, prompt, *, cwd, model, resume, append_system_prompt):
        self.n += 1
        self.calls.append({"prompt": prompt, "resume": resume, "system": append_system_prompt})
        yield HarnessEvent("result", text=f"answer {self.n}", session_id=resume or f"thread-{self.n}")


def test_changed_instructions_start_a_seeded_session(fake, monkeypatch):
    """The CLIs keep a session's system prompt from its first turn, so a turn
    with other instructions starts a new native session with the history; the
    same ones, or none (they carry over), resume it."""
    harness = SessionsHarness()
    monkeypatch.setattr(hr, "_state", lambda client_obj, executor, name: (harness, HarnessConversations()))
    client = Client()

    def turn(text, previous=None, **kwargs):
        if previous is not None:
            kwargs["previous_response_id"] = previous.id
        return asyncio.run(hr.handle_response(**call(client=client, input_data=text, kwargs=kwargs)))

    r1 = turn("one", instructions="french")
    r2 = turn("two", r1, instructions="french")
    assert harness.calls[1]["resume"] == "thread-1", "same instructions: native resume"
    r3 = turn("three", r2, instructions="caps")
    assert harness.calls[2]["resume"] is None and harness.calls[2]["system"] == "caps"
    assert "<conversation_history>" in harness.calls[2]["prompt"] and "two" in harness.calls[2]["prompt"]
    assert "answer 2" in harness.calls[2]["prompt"], "the new session gets the whole conversation"
    assert r3.agentd["session_id"] == "thread-3"
    turn("four", r3, instructions="caps")
    assert harness.calls[3]["resume"] == "thread-3", "the new session keeps resuming"
    r5 = turn("five", r3)
    assert harness.calls[4]["resume"] == "thread-3" and harness.calls[4]["system"] == "caps", \
        "omitted instructions keep the conversation's"
    turn("six", r5, instructions="")
    assert harness.calls[5]["resume"] is None and harness.calls[5]["system"] is None, "'' clears them"


def test_session_id_with_changed_instructions(fake, monkeypatch):
    from agentd.harness.chat import run_turn

    harness = SessionsHarness()
    conv = HarnessConversations()

    async def turn(messages, session_id=None):
        events = run_turn(harness_name="codex", harness=harness, conversations=conv, messages=messages,
                          cwd=Path("/tmp"), model=None, tool_manifest="", session_id=session_id)
        return [e async for e in events][-1]

    first = asyncio.run(turn([{"role": "system", "content": "a"}, {"role": "user", "content": "hi"}]))
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": first.text}]
    asyncio.run(turn([{"role": "system", "content": "a"}, *history, {"role": "user", "content": "more"}],
                     session_id=first.session_id))
    assert harness.calls[1]["resume"] == first.session_id
    asyncio.run(turn([{"role": "system", "content": "b"}, *history, {"role": "user", "content": "more"}],
                     session_id=first.session_id))
    assert harness.calls[2]["resume"] is None and harness.calls[2]["system"] == "b"
    # Without history to start from, the session is resumed anyway (with a warning).
    asyncio.run(turn([{"role": "system", "content": "b"}, {"role": "user", "content": "more"}],
                     session_id=first.session_id))
    assert harness.calls[3]["resume"] == first.session_id
    # Sessions agentd didn't start are resumed as asked.
    asyncio.run(turn([{"role": "system", "content": "b"}, *history, {"role": "user", "content": "x"}],
                     session_id="external-session"))
    assert harness.calls[4]["resume"] == "external-session"


def test_streaming_events_and_resume_from_streamed_response(fake):
    from agentd.ptc import TextDelta, display_events_async

    client = Client()

    async def collect(**kw):
        return [e async for e in hr.stream_response(**call(client=client, **kw))]

    events = asyncio.run(collect(input_data="go"))
    types = [e.type for e in events]
    assert types == [
        "response.created", "response.in_progress", "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.delta", "response.output_text.done",
        "response.content_part.done", "response.output_item.done", "response.completed",
    ]
    assert [e.sequence_number for e in events] == list(range(len(events)))
    final = events[-1].response
    assert final.output_text == "step one\n\nanswer 1"
    assert "".join(e.delta for e in events if e.type == "response.output_text.delta") == final.output_text

    async def rendered():
        async def gen():
            for e in events:
                yield e
        return [ev.text async for ev in display_events_async(gen()) if isinstance(ev, TextDelta)]
    assert "".join(asyncio.run(rendered())) == final.output_text, "agentd's display_events must render it"

    asyncio.run(collect(input_data="more", kwargs={"previous_response_id": final.id}))
    assert fake.calls[1]["resume"] == "thread-1"


def _live_backends():
    from agentd.sandbox.executor import docker_available, krun_available

    from agentd.sandbox.executor import colima_available

    return [b for b, ok in (("krun", krun_available()), ("krun-colima", colima_available()),
                            ("docker", docker_available())) if ok]


@pytest.fixture(params=["krun", "krun-colima", "docker"])
def live_executor(request):
    """Executor factory per backend for live tests (workspaces under ~/.agentd/tmp)."""
    from agentd.sandbox.executor import DockerExecutor, KrunExecutor

    if request.param not in _live_backends():
        pytest.skip(f"{request.param} sandbox not set up")
    if request.param == "krun-colima":
        return lambda **kw: KrunExecutor(colima=True, **kw)
    return KrunExecutor if request.param == "krun" else DockerExecutor


def live_tmp():
    from agentd.sandbox.base import DEFAULT_HOME

    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp")


AGENTS_ROOTFS = Path.home() / ".agentd" / "rootfs" / "agents"
live = pytest.mark.skipif(
    os.environ.get("AGENTD_LIVE") != "1" or not (Path.home() / ".codex" / "auth.json").exists(),
    reason="set AGENTD_LIVE=1 with Codex logged in on the host (makes real model calls)",
)


@live
def test_live_responses_api_codex_stream_then_claude_code(live_executor):
    from openai import OpenAI

    from agentd.ptc import patch_openai_with_ptc

    with live_tmp() as tmp:
        ws = Path(tmp, "ws")
        ws.mkdir()
        with live_executor(transcripts_dir=Path(tmp, "t")) as ex:
            client = patch_openai_with_ptc(OpenAI(api_key="unused"), cwd=ws, executor=ex, harness="codex")
            events = list(client.responses.create(
                input="Run `uname -sr` in the shell and remember the code word EGRET-3. Reply with the uname output.",
                instructions="Be brief.", stream=True,
            ))
            done = events[-1]
            assert done.type == "response.completed", [e.type for e in events][-3:]
            assert "Linux" in done.response.output_text

            r2 = client.responses.create(input="What was the code word? Just the word.",
                                         previous_response_id=done.response.id)
            assert r2.agentd["session_id"] == done.response.agentd["session_id"], "native Codex resume"
            assert "EGRET-3" in r2.output_text

            r3 = client.responses.create(model="claude-sonnet-5", harness="claude-code",
                                         input="Repeat the code word once more, just the word.",
                                         previous_response_id=r2.id)
            assert "EGRET-3" in r3.output_text
