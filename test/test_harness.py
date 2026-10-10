"""
Tests for agentd harness routing (agentd.harness.chat) and, with
AGENTD_LIVE=1, Claude Code running inside a sandbox (libkrun or Docker) via the
patched OpenAI client.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

# Keep agentd's JSONL conversation logs out of the repo during tests.
os.environ.setdefault("AGENTD_LOG_DIR", tempfile.mkdtemp(prefix="agentd-test-logs-"))

from agentd.harness import claude_project_dirname  # noqa: E402
from agentd.harness.chat import HarnessConversations, render_history, run_turn, split_messages  # noqa: E402
from agentd.harness.events import HarnessEvent  # noqa: E402
from test.live import live, live_tmp  # noqa: E402


class FakeHarness:
    """Records how it was called; replies with a fixed answer and session id."""

    def __init__(self, reply="ok", session="s1"):
        self.reply, self.session, self.calls = reply, session, []

    async def run(self, prompt, *, cwd, model, resume, append_system_prompt):
        self.calls.append({"prompt": prompt, "resume": resume, "system": append_system_prompt})
        yield HarnessEvent("text", text="thinking... ")
        yield HarnessEvent("text", text=self.reply)
        yield HarnessEvent("result", text=self.reply, session_id=self.session)


def turn(harness, conv, messages, name="claude-code", streaming=False):
    async def go():
        return [e async for e in run_turn(
            harness_name=name, harness=harness, conversations=conv, model=None,
            messages=messages, cwd=Path("/tmp"), streaming=streaming,
        )]
    return asyncio.run(go())


def test_split_and_render():
    msgs = [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}, {"role": "user", "content": "again"}]
    system, history, prompt = split_messages(msgs)
    assert (system, history, prompt) == ("be terse", [("user", "hi"), ("assistant", "hello")], "again")
    assert "[assistant]\nhello" in render_history(history)
    with pytest.raises(ValueError):
        split_messages([{"role": "assistant", "content": "x"}])


def test_resumes_native_session_when_history_matches():
    h, conv = FakeHarness(reply="PELICAN"), HarnessConversations()
    m1 = [{"role": "user", "content": "remember PELICAN"}]
    turn(h, conv, m1)
    assert h.calls[0]["resume"] is None and h.calls[0]["prompt"] == "remember PELICAN"
    m2 = m1 + [{"role": "assistant", "content": "PELICAN"}, {"role": "user", "content": "word?"}]
    turn(h, conv, m2)
    assert h.calls[1]["resume"] == "s1"
    assert h.calls[1]["prompt"] == "word?", "a resumed session gets only the new message"


def test_edited_history_or_other_harness_is_seeded_not_resumed():
    h, conv = FakeHarness(reply="PELICAN"), HarnessConversations()
    m1 = [{"role": "user", "content": "remember PELICAN"}]
    turn(h, conv, m1)
    edited = m1 + [{"role": "assistant", "content": "something else"}, {"role": "user", "content": "word?"}]
    turn(h, conv, edited)
    assert h.calls[1]["resume"] is None and "<conversation_history>" in h.calls[1]["prompt"]
    # Same history as the recorded run, but a different harness: seed it.
    other = FakeHarness()
    matching = m1 + [{"role": "assistant", "content": "PELICAN"}, {"role": "user", "content": "word?"}]
    turn(other, conv, matching, name="codex")
    assert other.calls[0]["resume"] is None and "remember PELICAN" in other.calls[0]["prompt"]


def test_streaming_records_the_streamed_text():
    h, conv = FakeHarness(reply="done"), HarnessConversations()
    m1 = [{"role": "user", "content": "go"}]
    turn(h, conv, m1, streaming=True)
    m2 = m1 + [{"role": "assistant", "content": "thinking... \n\ndone"}, {"role": "user", "content": "next"}]
    turn(h, conv, m2, streaming=True)
    assert h.calls[1]["resume"] == "s1"


def test_codex_item_events_map_to_neutral_events():
    from agentd.harness.codex import _item_events

    ev = _item_events({"type": "command_execution", "command": "ls", "aggregated_output": "x\n", "exit_code": 2})
    assert [(e.kind, e.name, e.is_error) for e in ev] == [("tool_use", "shell", False), ("tool_result", "shell", True)]
    ev = _item_events({"type": "mcp_tool_call", "server": "gh", "tool": "search", "arguments": {"q": 1},
                       "result": {"ok": 1}, "error": None})
    assert ev[0].name == "gh.search" and ev[1].data == {"ok": 1} and not ev[1].is_error
    assert _item_events({"type": "agent_message", "text": "hi"})[0].text == "hi"
    assert _item_events({"type": "reasoning", "text": "..."}) == []


def test_model_falls_back_to_harness_default_across_vendors():
    from agentd.harness.chat import _harness_model

    assert _harness_model("claude-code", "claude-sonnet-5") == "claude-sonnet-5"
    assert _harness_model("claude-code", "anthropic/claude-sonnet-5") == "claude-sonnet-5"
    assert _harness_model("claude-code", "gpt-6.1") is None
    assert _harness_model("codex", "gpt-6.1") == "gpt-6.1"
    assert _harness_model("codex", "openai/gpt-6.1") == "gpt-6.1"
    assert _harness_model("codex", "claude-sonnet-5") is None
    assert _harness_model("codex", None) is None


def test_claude_stream_json_parsing():
    from agentd.harness.claude_cli import parse_line

    assert parse_line(b"not json") == [] and parse_line(b'{"type": "system", "subtype": "init"}') == []
    ev = parse_line(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}))
    assert [(e.kind, e.text, e.name, e.data) for e in ev] == [
        ("text", "hi", "", None), ("tool_use", "", "Bash", {"command": "ls"})]
    ev = parse_line(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "out", "is_error": True}]}}))
    assert ev[0].kind == "tool_result" and ev[0].data == "out" and ev[0].is_error
    ev = parse_line(json.dumps({"type": "result", "subtype": "success", "result": "done", "session_id": "s", "is_error": False}))
    assert (ev[0].kind, ev[0].text, ev[0].session_id, ev[0].is_error) == ("result", "done", "s", False)


def test_claude_project_dirname():
    assert claude_project_dirname("/Users/me/my.work_space") == "-Users-me-my-work-space"


@live
def test_live_claude_code_harness_resume_and_switch(live_executor):
    from openai import OpenAI

    from agentd.ptc import patch_openai_with_ptc

    with live_tmp() as tmp:
        ws = Path(tmp, "my.work_space")  # '.' and '_' exercise the project-dir encoding
        ws.mkdir()
        transcripts = Path(tmp, "transcripts")
        with live_executor(transcripts_dir=transcripts) as ex:
            client = patch_openai_with_ptc(OpenAI(api_key="unused"), cwd=ws, executor=ex, harness="claude-code")
            model = "claude-sonnet-5"

            msgs = [{"role": "user", "content":
                     "Use Bash to run `uname -sr > kernel.txt` and `echo PELICAN-42 > /tmp/codeword`. "
                     "Remember the code word PELICAN-42. Reply OK."}]
            r1 = client.chat.completions.create(model=model, messages=msgs)
            sid = r1.agentd["session_id"]
            assert Path(ws, "kernel.txt").read_text().startswith("Linux"), "Bash must run in the microVM"
            from agentd.harness.transcripts import store_dir
            assert (store_dir("claude-code", ws.resolve(), transcripts) / f"{sid}.jsonl").exists()

            # Same harness, same history + reply: the native session resumes.
            msgs += [{"role": "assistant", "content": r1.choices[0].message.content},
                     {"role": "user", "content": "What was the code word? Reply with just it."}]
            r2 = client.chat.completions.create(model=model, messages=msgs)
            assert r2.agentd["session_id"] == sid
            assert "PELICAN-42" in r2.choices[0].message.content

            # Switch to PTC for a turn: same conversation, same VM (/tmp persists).
            msgs += [{"role": "assistant", "content": r2.choices[0].message.content},
                     {"role": "user", "content": "Using bash, print the contents of /tmp/codeword."}]
            r3 = client.chat.completions.create(model=model, messages=msgs, harness="ptc")
            assert "PELICAN-42" in r3.choices[0].message.content

            # And back to Claude Code: a fresh native session, seeded with the history.
            msgs += [{"role": "assistant", "content": r3.choices[0].message.content},
                     {"role": "user", "content": "Repeat the code word once more, just the word."}]
            r4 = client.chat.completions.create(model=model, messages=msgs)
            assert r4.agentd["session_id"] != sid
            assert "PELICAN-42" in r4.choices[0].message.content


codex_live = pytest.mark.skipif(
    os.environ.get("AGENTD_LIVE") != "1" or not (Path.home() / ".codex" / "auth.json").exists(),
    reason="set AGENTD_LIVE=1 with Codex logged in on the host (makes real model calls)",
)


@codex_live
def test_live_codex_harness_resume_and_switch_with_claude_code(live_executor):
    import json

    from openai import OpenAI

    from agentd.ptc import patch_openai_with_ptc

    host_token = json.loads((Path.home() / ".codex" / "auth.json").read_text())["tokens"]["access_token"]
    with live_tmp() as tmp:
        ws = Path(tmp, "ws")
        ws.mkdir()
        transcripts = Path(tmp, "transcripts")
        with live_executor(transcripts_dir=transcripts) as ex:
            client = patch_openai_with_ptc(OpenAI(api_key="unused"), cwd=ws, executor=ex, harness="codex")

            msgs = [{"role": "user", "content":
                     "Run `uname -sr > kernel.txt` in the shell. Remember the code word HERON-7. Reply OK."}]
            r1 = client.chat.completions.create(model=None, messages=msgs)
            sid = r1.agentd["session_id"]
            assert not r1.agentd["is_error"], r1.choices[0].message.content
            assert Path(ws, "kernel.txt").read_text().startswith("Linux"), "Codex must run in the microVM"
            assert list((transcripts / "codex").rglob(f"rollout-*{sid}.jsonl")), "rollout must persist on the host"

            # The sandbox holds only placeholder credentials.
            out, _ = asyncio.run(ex.run(ex.session.sandbox.exec(["/bin/sh", "-c", "cat ~/.codex/auth.json"])))
            assert host_token not in out.decode() and "agentd-sandbox-placeholder" in out.decode()

            # Native Codex resume.
            msgs += [{"role": "assistant", "content": r1.choices[0].message.content},
                     {"role": "user", "content": "What was the code word? Reply with just it."}]
            r2 = client.chat.completions.create(model=None, messages=msgs)
            assert r2.agentd["session_id"] == sid and "HERON-7" in r2.choices[0].message.content

            # Switch to Claude Code mid-conversation: history carries over, same VM.
            msgs += [{"role": "assistant", "content": r2.choices[0].message.content},
                     {"role": "user", "content": "Read kernel.txt and tell me the code word from earlier, "
                                                 "as `<kernel line> | <code word>`."}]
            r3 = client.chat.completions.create(model="claude-sonnet-5", messages=msgs, harness="claude-code")
            text = r3.choices[0].message.content
            assert "HERON-7" in text and "Linux" in text



@live
def test_live_claude_code_persistent_session(live_executor):
    """One process across turns; a background job's completion comes back as an
    unprompted turn (Responses stream); a cancelled turn doesn't end the process."""
    import asyncio

    from openai import AsyncOpenAI

    from agentd.ptc import patch_openai_with_ptc

    async def main(ex, ws):
        unprompted = []

        async def on_unprompted(stream):
            unprompted.append([e async for e in stream])
        client = patch_openai_with_ptc(AsyncOpenAI(api_key="unused"), cwd=ws, executor=ex, harness="claude-code",
                                       on_unprompted=on_unprompted)
        model = "claude-sonnet-5"
        r1 = await client.responses.create(model=model, input=(
            "Use the Bash tool with run_in_background set to true to run `sleep 15; echo FALCON-9`. "
            "Then reply just STARTED. When it finishes, reply with its output."))
        harness = client._harness_objs["claude-code"]
        proc = harness._live[r1.agentd["session_id"]]
        r2 = await client.responses.create(model=model, previous_response_id=r1.id,
                                           input="Reply with just the word PONG.")
        assert "PONG" in r2.output_text and r2.agentd["session_id"] == r1.agentd["session_id"]

        # Cancel a turn mid-way: interrupted, the process (and the background job) stays.
        async def long_turn():
            stream = await client.responses.create(model=model, previous_response_id=r2.id, stream=True,
                                                   input="Use Bash to run `sleep 60`, then reply DONE.")
            async for _ in stream:
                pass
        task = asyncio.ensure_future(long_turn())
        await asyncio.sleep(8)
        task.cancel()
        r3 = await client.responses.create(model=model, previous_response_id=r2.id,
                                           input="Reply with just the word AGAIN.")
        assert "AGAIN" in r3.output_text and proc.alive

        for _ in range(120):
            if unprompted:
                break
            await asyncio.sleep(0.5)
        done = unprompted[0][-1]
        assert done.type == "response.completed" and done.response.agentd["unprompted"]
        assert "FALCON-9" in done.response.output_text
        # The conversation goes on from the unprompted turn.
        r4 = await client.responses.create(model=model, previous_response_id=done.response.id,
                                           input="What did the background job print? Just that.")
        assert "FALCON-9" in r4.output_text and r4.agentd["session_id"] == r1.agentd["session_id"]
        await harness.close()

    with live_tmp() as tmp:
        ws = Path(tmp, "ws")
        ws.mkdir()
        with live_executor(transcripts_dir=Path(tmp, "transcripts")) as ex:
            asyncio.run(main(ex, ws))
