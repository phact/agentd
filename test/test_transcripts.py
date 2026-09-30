"""
Transcript stores and syncing to the CLIs' native paths (agentd.harness.transcripts),
explicit session_id resume, and persisted previous_response_id. With AGENTD_LIVE=1,
resuming Claude Code / Codex sessions by id in a fresh sandbox.
"""
import asyncio
import os
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("AGENTD_LOG_DIR", tempfile.mkdtemp(prefix="agentd-test-logs-"))

from agentd.harness import transcripts as tr  # noqa: E402
from agentd.harness.chat import HarnessConversations, run_turn  # noqa: E402
from agentd.harness.events import HarnessEvent  # noqa: E402


def _write(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_layout_paths():
    ws = "/Users/me/my.proj"
    assert tr.store_dir("codex", ws, Path("/r")) == Path("/r/codex/-Users-me-my-proj")
    assert tr.sandbox_dir("claude-code", ws) == "/home/agent/.claude/projects/-Users-me-my-proj"
    assert tr.sandbox_dir("codex", ws) == "/home/agent/.codex/sessions"


def test_sync_out_and_pull_in_both_layouts(tmp_path):
    store, native = tmp_path / "store", tmp_path / "native"
    now = time.time()
    # Claude Code: <id>.jsonl plus a per-session subdir; Codex: dated rollout files.
    _write(store / "s1.jsonl", "cc v1", now - 100)
    _write(store / "s1" / "subagents" / "a.jsonl", "sub", now - 100)
    _write(store / "2026" / "10" / "01" / "rollout-2026-10-01T00-00-00-t1.jsonl", "codex v1", now - 100)

    copied = tr.sync_out(store, native)
    assert {p.relative_to(native).as_posix() for p in copied} == {
        "s1.jsonl", "s1/subagents/a.jsonl", "2026/10/01/rollout-2026-10-01T00-00-00-t1.jsonl"}
    assert tr.sync_out(store, native) == [], "unchanged files are not copied again"

    # The session continues in the sandbox: newer store file goes out.
    _write(store / "s1.jsonl", "cc v2", now - 50)
    assert [p.name for p in tr.sync_out(store, native)] == ["s1.jsonl"]
    assert (native / "s1.jsonl").read_text() == "cc v2"

    # Continued on the host instead: pull brings it in; sync_out won't clobber it.
    _write(native / "s1.jsonl", "cc v3 (host)", now)
    assert tr.sync_out(store, native) == [], "a newer native file is never overwritten"
    assert [p.name for p in tr.pull_in("claude-code", store, native, "s1")] == ["s1.jsonl"]
    assert (store / "s1.jsonl").read_text() == "cc v3 (host)"
    assert tr.pull_in("claude-code", store, native, "s1") == []

    # Codex sessions are found by thread id anywhere in the date tree.
    assert [p.name for p in tr.session_files("codex", native, "t1")] == ["rollout-2026-10-01T00-00-00-t1.jsonl"]
    fresh = tmp_path / "fresh-store"
    assert len(tr.pull_in("codex", fresh, native, "t1")) == 1
    assert (fresh / "2026/10/01/rollout-2026-10-01T00-00-00-t1.jsonl").read_text() == "codex v1"


class FakeHarness:
    def __init__(self):
        self.calls = []

    async def run(self, prompt, *, cwd, model, resume, append_system_prompt):
        self.calls.append({"prompt": prompt, "resume": resume})
        yield HarnessEvent("text", text="ok")
        yield HarnessEvent("result", text="ok", session_id=resume or "new-session")


def test_explicit_session_id_resumes_with_only_the_new_message():
    h = FakeHarness()
    msgs = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "continue"}]

    async def go():
        return [e async for e in run_turn(harness_name="codex", harness=h, conversations=HarnessConversations(),
                                          model=None, messages=msgs, cwd=Path("/tmp"), session_id="thread-9")]

    events = asyncio.run(go())
    assert h.calls == [{"prompt": "continue", "resume": "thread-9"}], "no seeded history when resuming by id"
    assert events[-1].session_id == "thread-9"


def test_previous_response_id_survives_a_new_client(monkeypatch, tmp_path):
    from agentd.harness import responses as hr

    monkeypatch.setattr(hr, "RESPONSES_DIR", tmp_path / "responses")
    h = FakeHarness()
    monkeypatch.setattr(hr, "_state", lambda client_obj, executor, name: (h, HarnessConversations()))

    async def no_skills(*a, **k):
        return ""
    monkeypatch.setattr(hr, "_prepare_skills", no_skills)

    class Client:
        pass

    def call(client, harness, **kw):
        return asyncio.run(hr.handle_response(
            client_obj=client, harness_name=harness, model=None, kwargs=kw.pop("kwargs", {}), mcp_servers=None,
            cwd=Path("/tmp"), executor=None, server_cache={}, bridge_cache={}, skills_override=None, **kw))

    r1 = call(Client(), "codex", input_data="remember HERON")
    # A different process / client object: the record comes from disk.
    call(Client(), "codex", input_data="what was it?", kwargs={"previous_response_id": r1.id})
    assert h.calls[1] == {"prompt": "what was it?", "resume": "new-session"}, "same harness: native resume by id"
    call(Client(), "claude-code", input_data="and now?", kwargs={"previous_response_id": r1.id})
    assert h.calls[2]["resume"] is None and "remember HERON" in h.calls[2]["prompt"], "other harness: seeded"


# --------------------------------------------------------------------------- #
# Live: resume by session id in a fresh sandbox
# --------------------------------------------------------------------------- #

def _live_backends():
    from agentd.sandbox.executor import docker_available, krun_available

    return [b for b, ok in (("krun", krun_available()), ("docker", docker_available())) if ok]


@pytest.fixture(params=["krun", "docker"])
def live_executor(request):
    from agentd.sandbox.executor import DockerExecutor, KrunExecutor

    if os.environ.get("AGENTD_LIVE") != "1":
        pytest.skip("set AGENTD_LIVE=1 (makes real model calls)")
    if request.param not in _live_backends():
        pytest.skip(f"{request.param} sandbox not set up")
    return KrunExecutor if request.param == "krun" else DockerExecutor


@pytest.mark.parametrize("harness,model", [("claude-code", "claude-sonnet-5"), ("codex", None)])
def test_live_resume_by_session_id_in_a_fresh_sandbox(live_executor, harness, model, monkeypatch):
    if harness == "codex" and not (Path.home() / ".codex" / "auth.json").exists():
        pytest.skip("Codex not logged in on the host")
    from openai import OpenAI

    from agentd.sandbox.base import DEFAULT_HOME
    from agentd.ptc import patch_openai_with_ptc

    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp") as tmp:
        tmp = Path(tmp)
        ws, stores, native_root = tmp / "ws", tmp / "stores", tmp / "native"
        ws.mkdir()
        # Keep the test off your real ~/.claude and ~/.codex.
        monkeypatch.setattr(tr, "native_dir", lambda h, w: native_root / h)  # (overrides conftest)

        def turn(text, **kw):
            with live_executor(transcripts_dir=stores) as ex:  # a new sandbox every time
                client = patch_openai_with_ptc(OpenAI(api_key="unused"), cwd=ws, executor=ex, harness=harness)
                return client.chat.completions.create(model=model, messages=[{"role": "user", "content": text}], **kw)

        r1 = turn("Remember the code word OSPREY-5. Reply OK.")
        sid = r1.agentd["session_id"]
        assert sid and not r1.agentd["is_error"], r1.choices[0].message.content
        synced = tr.session_files(harness, native_root / harness, sid)
        assert synced, "the transcript must land at the native path"

        r2 = turn("What was the code word? Reply with just it.", session_id=sid)
        assert r2.agentd["session_id"] == sid
        assert "OSPREY-5" in r2.choices[0].message.content
