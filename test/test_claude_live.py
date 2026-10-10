"""
Claude Code's persistent sessions (agentd.harness.claude_live), live: the real
``claude`` CLI in a real sandbox making real model calls. One process per
conversation, unprompted turns after background tasks, interrupting instead of
killing, idle close and resume, set_model.
"""
import asyncio
from pathlib import Path

from agentd.harness.claude_code import ClaudeCodeHarness
from agentd.harness.claude_live import LiveTurn
from test.live import live, live_tmp

pytestmark = live

MODEL = "claude-haiku-4-5"
BACKGROUND = ("Use the Bash tool with run_in_background set to true to run `sleep {secs}; echo FALCON-9`. "
              "Don't wait for it: reply just STARTED. When it finishes, reply with its output.")
SLOW = "Use the Bash tool (not in the background) to run `sleep 120`, then reply DONE."


def _sandboxed(live_executor, test, **harness_kw):
    with live_tmp() as tmp:
        ws = Path(tmp, "ws")
        ws.mkdir()
        with live_executor(transcripts_dir=Path(tmp, "transcripts"), env={"AGENTD_EXTRA": "OSPREY-3"}) as ex:
            harness = ClaudeCodeHarness(ex, **harness_kw)

            async def main():
                try:
                    await asyncio.wait_for(test(harness, str(ws)), 300)
                finally:
                    await harness.close()
            asyncio.run(main())


async def _turn(harness, cwd, prompt, **kw):
    return [e async for e in harness.run(prompt, cwd=cwd, model=kw.pop("model", MODEL), **kw)]


def _texts(events):
    return " ".join(e.text for e in events if e.kind in ("text", "result"))


async def _first(got: list[LiveTurn], secs=90) -> LiveTurn:
    for _ in range(secs * 10):
        if got:
            return got[0]
        await asyncio.sleep(0.1)
    raise AssertionError("no unprompted turn")


def test_turns_share_one_process_with_the_sandbox_env(live_executor):
    async def test(harness, cwd):
        first = await _turn(harness, cwd, "Use Bash to run `echo $PTC_SKILLS_DIR $AGENTD_EXTRA "
                                          "$CLAUDE_CODE_DISABLE_ARTIFACT` and reply with its output.")
        sid = first[-1].session_id
        assert first[-1].kind == "result" and not first[-1].is_error
        out = " ".join(str(e.data) for e in first if e.kind == "tool_result")
        assert "skills OSPREY-3 1" in out, "the executor's env and base env, and the CLI's own"
        proc = harness._live[sid]
        assert "--input-format" in proc.argv and "--resume" not in proc.argv and "--settings" in proc.argv
        second = await _turn(harness, cwd, "Reply with just the word PONG.", resume=sid)
        assert "PONG" in _texts(second) and second[-1].session_id == sid
        assert harness._live[sid] is proc and proc.alive, "the second turn went to the same process"
    _sandboxed(live_executor, test)


def test_unprompted_turn_after_a_background_task(live_executor):
    async def test(harness, cwd):
        got: list[LiveTurn] = []
        harness.on_unprompted = got.append
        events = await _turn(harness, cwd, BACKGROUND.format(secs=10))
        sid = events[-1].session_id
        assert events[-1].kind == "result" and not events[-1].is_error
        proc = harness._live[sid]
        assert proc.background, "background_tasks_changed lists the running task"
        turn = await _first(got)
        unprompted = [e async for e in turn.events()]
        assert unprompted[0].kind == "task" and unprompted[0].data["status"] == "completed"
        assert "FALCON-9" in _texts(unprompted)
        assert unprompted[-1].kind == "result" and turn.session_id == sid
        assert not proc.background and not turn.prompted
        # The conversation goes on in the same process.
        after = await _turn(harness, cwd, "What did the background job print? Just that.", resume=sid)
        assert "FALCON-9" in _texts(after) and harness._live[sid] is proc
    _sandboxed(live_executor, test)


def test_new_question_interrupts_and_background_tasks_survive(live_executor):
    async def test(harness, cwd):
        got: list[LiveTurn] = []
        harness.on_unprompted = got.append
        first = await _turn(harness, cwd, BACKGROUND.format(secs=40))
        sid = first[-1].session_id
        proc = harness._live[sid]

        # A turn stuck in a slow tool, then a new question: the old turn is interrupted.
        slow_events = []

        async def slow():
            async for e in harness.run(SLOW, cwd=cwd, model=MODEL, resume=sid):
                slow_events.append(e)
        slow_task = asyncio.ensure_future(slow())
        for _ in range(600):
            if any(e.kind == "tool_use" for e in slow_events):
                break
            await asyncio.sleep(0.1)
        new = await _turn(harness, cwd, "Reply with just the word PONG.", resume=sid)
        await asyncio.wait_for(slow_task, 30)
        assert slow_events[-1].kind == "result" and slow_events[-1].is_error, "interrupted"
        assert "PONG" in _texts(new)

        # Cancelled (the caller went away): interrupted, the process stays.
        cancelled = asyncio.ensure_future(_turn(harness, cwd, SLOW, resume=sid))
        await asyncio.sleep(8)
        cancelled.cancel()
        again = await _turn(harness, cwd, "Reply with just the word AGAIN.", resume=sid)
        assert "AGAIN" in _texts(again)

        # The background task started before all that still finished and spoke up.
        turn = await _first(got)
        assert "FALCON-9" in _texts([e async for e in turn.events()])
        assert harness._live[sid] is proc and proc.alive
    _sandboxed(live_executor, test)


def test_idle_close_then_resume_and_model_change(live_executor):
    async def test(harness, cwd):
        first = await _turn(harness, cwd, "Remember the code word HERON-7. Reply OK.")
        sid = first[-1].session_id
        proc = harness._live[sid]
        # A model change goes to the live process.
        second = await _turn(harness, cwd, "Reply with just the word PONG.", model="claude-sonnet-5", resume=sid)
        assert "PONG" in _texts(second) and harness._live[sid] is proc and proc.model == "claude-sonnet-5"
        for _ in range(300):
            if not proc.alive:
                break
            await asyncio.sleep(0.1)
        assert not proc.alive and sid not in harness._live, "closed when idle"
        third = await _turn(harness, cwd, "What was the code word? Just the word.", resume=sid)
        assert "HERON-7" in _texts(third) and third[-1].session_id == sid
        resumed = harness._live[sid]
        assert resumed is not proc and resumed.argv[resumed.argv.index("--resume") + 1] == sid
    _sandboxed(live_executor, test, idle_minutes=0.1)


def test_not_closed_while_a_background_task_runs(live_executor):
    async def test(harness, cwd):
        got: list[LiveTurn] = []
        harness.on_unprompted = got.append
        first = await _turn(harness, cwd, BACKGROUND.format(secs=15))
        proc = harness._live[first[-1].session_id]
        for _ in range(900):
            if got:
                break
            assert proc.alive, "kept while the background task runs"
            await asyncio.sleep(0.1)
        await got[0].wait()
        for _ in range(300):
            if not proc.alive:
                break
            await asyncio.sleep(0.1)
        assert not proc.alive
    _sandboxed(live_executor, test, idle_minutes=0.05)  # 3 s, shorter than the task


def test_one_shot_mode_still_works(live_executor):
    async def test(harness, cwd):
        events = await _turn(harness, cwd, "Reply with just the word PONG.")
        assert "PONG" in _texts(events) and not events[-1].is_error
        assert not harness._live
    _sandboxed(live_executor, test, persistent=False)


def test_turns_from_short_lived_loops(live_executor):
    """A sync client runs each call on a fresh loop: the process and its unprompted
    turns live on the executor's."""
    with live_tmp() as tmp:
        cwd = str(Path(tmp, "ws"))
        Path(cwd).mkdir()
        with live_executor(transcripts_dir=Path(tmp, "transcripts")) as ex:
            harness = ClaudeCodeHarness(ex)
            got: list[LiveTurn] = []
            harness.on_unprompted = got.append
            try:
                first = asyncio.run(_turn(harness, cwd, BACKGROUND.format(secs=10)))
                sid = first[-1].session_id
                proc = harness._live[sid]
                second = asyncio.run(_turn(harness, cwd, "Reply with just the word PONG.", resume=sid))
                assert "PONG" in _texts(second) and harness._live[sid] is proc

                async def unprompted():
                    return [e async for e in (await _first(got)).events()]
                assert "FALCON-9" in _texts(asyncio.run(unprompted())), "delivered with the caller's loop gone"
                third = asyncio.run(_turn(harness, cwd, "Reply with just the word AGAIN.", resume=sid))
                assert "AGAIN" in _texts(third) and harness._live[sid] is proc
            finally:
                asyncio.run(harness.close())
