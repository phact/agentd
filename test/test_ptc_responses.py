"""
PTC through the Responses API shares the harnesses' response store: a fake
model and executor check that previous_response_id replays the conversation
(from PTC or any harness), and that PTC responses can be continued elsewhere.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentd import ptc
from agentd.harness import responses as hr


def _response(rid, text):
    part = SimpleNamespace(type="output_text", text=text)
    return SimpleNamespace(id=rid, output=[SimpleNamespace(type="message", content=[part])],
                           instructions=None, previous_response_id=None)


class FakeModel:
    """Replies from a script; records each call's input and kwargs."""

    def __init__(self, *replies):
        self.replies, self.calls, self.n = list(replies), [], 0

    def __call__(self, _self, *args, model, input, **kwargs):
        self.n += 1
        self.calls.append({"input": [dict(m) for m in input], "kwargs": kwargs})
        return _response(f"resp_{self.n}", self.replies.pop(0))

    def stream(self, _self, *args, model, input, **kwargs):
        self.n += 1
        self.calls.append({"input": [dict(m) for m in input], "kwargs": kwargs})
        text, rid = self.replies.pop(0), f"resp_{self.n}"
        yield SimpleNamespace(type="response.output_text.delta", delta=text)
        yield SimpleNamespace(type="response.completed", response=_response(rid, text))


class FakeExecutor:
    def execute_bash(self, code, cwd):
        return f"ran: {code.strip()}", 0


class Client:
    pass


@pytest.fixture(autouse=True)
def no_skills(monkeypatch, tmp_path):
    monkeypatch.setattr(hr, "RESPONSES_DIR", tmp_path / "responses")

    async def setup(*a, **k):
        return {}, "unused", ""
    monkeypatch.setattr(ptc, "setup_skills_directory", setup)


def ptc_call(client, model, input_data, **kwargs):
    resource = SimpleNamespace(_client=client)
    return asyncio.run(ptc._handle_ptc_responses_call(
        resource, (), "gpt-4o", input_data, None, Path("/tmp"), FakeExecutor(), kwargs,
        False, model, None, {}, None, None))


def ptc_stream(client, model, input_data, **kwargs):
    resource = SimpleNamespace(_client=client)

    async def run():
        gen = await ptc._handle_ptc_responses_streaming(
            resource, (), "gpt-4o", input_data, None, Path("/tmp"), FakeExecutor(), kwargs,
            False, model.stream, None, {}, None, None)
        return [e async for e in gen]
    return asyncio.run(run())


def _roles(messages):
    return [(m["role"], m["content"]) for m in messages]


def test_previous_response_id_replays_the_ptc_conversation():
    client = Client()
    model = FakeModel("Let me check.\n```bash:execute\necho hi\n```", "It said hi.", "Yes: hi.")
    r1 = ptc_call(client, model, "run echo", instructions="be brief")
    assert r1.id == "resp_2" and r1.instructions == "be brief"
    first = model.calls[0]
    assert "previous_response_id" not in first["kwargs"] and "instructions" not in first["kwargs"]
    assert first["input"][0]["role"] == "system" and first["input"][0]["content"].startswith("be brief")

    record = hr._load(client, "resp_2")
    assert record["harness"] == "ptc"
    roles = [r for r, _ in _roles(record["messages"])]
    assert roles == ["system", "user", "assistant", "user", "assistant"], "code rounds are part of the record"
    assert record["messages"][0]["content"] == "be brief", "PTC guidance isn't stored"
    assert "ran: echo hi" in record["messages"][3]["content"]

    r2 = ptc_call(client, model, "what did it say?", previous_response_id=r1.id)
    replay = model.calls[2]
    assert "previous_response_id" not in replay["kwargs"], "the store, not the provider, holds the history"
    contents = [m["content"] for m in replay["input"]]
    assert "run echo" in contents and "It said hi." in contents and contents[-1] == "what did it say?"
    assert replay["input"][0]["content"].startswith("be brief"), "omitted instructions carry over"
    assert r2.previous_response_id == "resp_2"


def test_switching_between_ptc_and_a_harness_keeps_the_history():
    client = Client()
    # A Codex response, continued with PTC: its conversation is replayed.
    hr._remember(client, "resp_codex", "codex", "thread-9",
                 [{"role": "user", "content": "the code word is EGRET-3"}], "noted")
    model = FakeModel("EGRET-3")
    r = ptc_call(client, model, "what was the code word?", previous_response_id="resp_codex")
    assert _roles(model.calls[0]["input"])[1:] == [
        ("user", "the code word is EGRET-3"), ("assistant", "noted"), ("user", "what was the code word?")]

    # And the PTC response continued with a harness: a new session seeded with it all.
    messages, resume = hr._conversation(client, "codex", "and again?", None, r.id)
    assert resume is None, "a PTC response has no native session"
    assert _roles(messages) == [("user", "the code word is EGRET-3"), ("assistant", "noted"),
                                ("user", "what was the code word?"), ("assistant", "EGRET-3"),
                                ("user", "and again?")]


def test_unknown_previous_response_id_goes_to_the_provider():
    client = Client()
    model = FakeModel("ok")
    ptc_call(client, model, "hi", previous_response_id="resp_from_openai", instructions="x")
    assert model.calls[0]["kwargs"]["previous_response_id"] == "resp_from_openai"
    assert model.calls[0]["kwargs"]["instructions"] == "x"
    assert hr._load(client, "resp_1") is None, "a provider-side conversation isn't recorded"


def test_streaming_records_under_every_round():
    client = Client()
    model = FakeModel("```bash:execute\necho hi\n```", "It said hi.")
    events = ptc_stream(client, model, "run echo")
    completed = [e.response.id for e in events if getattr(e, "type", None) == "response.completed"]
    assert completed == ["resp_1", "resp_2"]
    for rid in completed:
        record = hr._load(client, rid)
        assert record["messages"][-1] == {"role": "assistant", "content": "It said hi."}

    model2 = FakeModel("hi")
    ptc_stream(client, model2, "what did it say?", previous_response_id="resp_2")
    assert [m["content"] for m in model2.calls[0]["input"]][-2:] == ["It said hi.", "what did it say?"]
