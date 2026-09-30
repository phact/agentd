"""Route patched ``chat.completions.create(harness=...)`` calls to a harness.

The caller's ``messages`` list is the harness-neutral transcript. Each call:

  * splits it into system text, prior history, and the new user prompt;
  * if the history is exactly a conversation this client already ran on the
    same harness (the recorded messages plus that run's reply), resumes that
    harness's native session, keeping tool-level fidelity;
  * otherwise (first call, edited history, or a different harness than the
    one that produced it) starts a fresh native session with the history
    rendered into the prompt, so switching harness keeps the conversation;
  * with ``session_id=`` (an id agentd returned as ``agentd.session_id``)
    resumes exactly that native session, even in a new sandbox or process;
  * returns the harness's final reply as an OpenAI chat completion (or
    streams its text), and logs every event to the agentd ConversationLog.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from agentd.harness import get_harness

_MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable")
# Between separate assistant text blocks of one turn when streaming them.
TEXT_SEPARATOR = "\n\n"


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def normalize(messages: list[dict]) -> list[tuple[str, str]]:
    return [(m.get("role", ""), _text(m.get("content"))) for m in messages]


def split_messages(messages: list[dict]) -> tuple[str, list[tuple[str, str]], str]:
    """(system text, prior non-system history, new user prompt)."""
    if not messages or messages[-1].get("role") != "user":
        raise ValueError("harness calls need messages ending with a user message")
    system = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer"))
    history = [(r, c) for r, c in normalize(messages[:-1]) if r not in ("system", "developer")]
    return system, history, _text(messages[-1].get("content"))


def render_history(history: list[tuple[str, str]]) -> str:
    lines = [f"[{role}]\n{content}" for role, content in history]
    return (
        "<conversation_history>\nThe conversation so far (it may have been with a different agent):\n\n"
        + "\n\n".join(lines)
        + "\n</conversation_history>\n\n"
    )


@dataclass
class _Run:
    harness: str
    session_id: str
    transcript: list[tuple[str, str]]


@dataclass
class HarnessConversations:
    """Per-client record of which native session produced which history."""

    runs: list[_Run] = field(default_factory=list)

    def match(self, harness: str, prior: list[tuple[str, str]]) -> str | None:
        for run in reversed(self.runs):
            if run.harness == harness and run.transcript == prior:
                return run.session_id
        return None

    def record(self, harness: str, session_id: str, transcript: list[tuple[str, str]]) -> None:
        self.runs.append(_Run(harness, session_id, transcript))


def _is_claude_model(model: str) -> bool:
    bare = model.split("/", 1)[1] if model.startswith("anthropic/") else model
    return bare.startswith("claude") or bare in _MODEL_ALIASES


def _harness_model(harness: str, model: str | None) -> str | None:
    """The model to ask the harness for, or None for the harness's default.

    A conversation may switch harness with the same ``model`` argument; a
    model from the other vendor falls back to the harness's own default.
    """
    if not model:
        return None
    if harness == "claude-code":
        return model.split("/", 1)[1] if model.startswith("anthropic/") and _is_claude_model(model) else (
            model if _is_claude_model(model) else None)
    if harness == "codex":
        if _is_claude_model(model):
            return None
        return model.split("/", 1)[1] if model.startswith("openai/") else model
    return model


async def run_turn(
    *,
    harness_name: str,
    harness: Any,
    conversations: HarnessConversations,
    model: str | None,
    messages: list[dict],
    cwd: Path,
    tool_manifest: str = "",
    streaming: bool = False,
    session_id: str | None = None,
) -> AsyncIterator[Any]:
    """Run one user turn; yields HarnessEvents, the last being ``result``.

    ``session_id`` resumes that native session explicitly (it is what agentd
    returns as ``agentd.session_id``): only the new user message is sent, and
    the session's transcript is first pulled from the CLI's native path if it
    is newer there, so this works in a fresh sandbox or after a restart.
    Otherwise the history is matched against recorded runs, or seeded.

    The run is recorded against the reply the caller receives (the final
    result, or all streamed text blocks joined by :data:`TEXT_SEPARATOR`) so
    that when the caller sends the history back with that reply, the native
    session is resumed. Transcripts are synced to the native path afterwards.
    """
    import asyncio

    from agentd.conversation_logger import create_log

    system, history, prompt = split_messages(messages)
    if session_id:
        resume = session_id
        await asyncio.to_thread(_pull_transcript, harness, harness_name, cwd, session_id)
    else:
        resume = conversations.match(harness_name, normalize(messages[:-1]))
        if resume is None and history:
            prompt = render_history(history) + prompt
    append = "\n\n".join(p for p in (system, tool_manifest) if p) or None

    clog = create_log(f"harness:{harness_name}", model or "")
    if clog:
        for role, content in normalize(messages):
            clog.message(role, content)

    final, streamed, turns = None, [], 0
    async for event in harness.run(
        prompt, cwd=cwd, model=_harness_model(harness_name, model), resume=resume, append_system_prompt=append
    ):
        if event.kind == "text":
            streamed.append(event.text)
        elif event.kind == "tool_use":
            turns += 1
        if clog:
            if event.kind == "text":
                clog.message("assistant", event.text)
            elif event.kind == "tool_use":
                clog.tool_call(harness_name, event.name, event.data)
            elif event.kind == "tool_result":
                clog.tool_result(harness_name, "", event.data)
        if event.kind == "result":
            final = event
        yield event
    if clog:
        clog.end(turns)
    session = getattr(getattr(harness, "executor", None), "session", None)
    if session is not None:
        await asyncio.to_thread(session.sync_transcripts_out, harness_name)
    if final is not None and final.session_id and not final.is_error:
        reply = TEXT_SEPARATOR.join(streamed) if streaming else final.text
        conversations.record(harness_name, final.session_id, normalize(messages) + [("assistant", reply)])


def _pull_transcript(harness: Any, harness_name: str, cwd: Path, session_id: str) -> None:
    """Before resuming by id: bring the session in from the native path if newer.

    Works before the sandbox exists: the store path depends only on the
    workspace (the executor's, once it has booted) and transcripts root."""
    from agentd.harness import transcripts

    executor = getattr(harness, "executor", None)
    if executor is None:
        return
    if executor.session is not None:
        executor.session.pull_transcript(harness_name, session_id)
        return
    if getattr(executor, "sync_transcripts", True):
        workspace = Path(cwd).resolve()
        transcripts.pull_in(
            harness_name,
            transcripts.store_dir(harness_name, workspace, executor.transcripts_dir),
            transcripts.native_dir(harness_name, workspace),
            session_id,
        )


def _state(client_obj: Any, executor: Any, harness_name: str):
    from agentd.sandbox.executor import SandboxExecutor

    if not isinstance(executor, SandboxExecutor):
        raise ValueError(f"harness={harness_name!r} runs inside a sandbox; pass executor=KrunExecutor() or DockerExecutor()")
    if getattr(client_obj, "_harness_objs", None) is None:
        client_obj._harness_objs = {}
        client_obj._harness_conversations = HarnessConversations()
    if harness_name not in client_obj._harness_objs:
        client_obj._harness_objs[harness_name] = get_harness(harness_name)(executor)
    return client_obj._harness_objs[harness_name], client_obj._harness_conversations


async def _prepare_skills(executor, cwd, mcp_servers, server_cache, bridge_cache, skills_override) -> str:
    """Same skills/bridge setup as PTC: MCP tools become skills in the sandbox."""
    from agentd.ptc import set_bridge_env, setup_skills_directory

    skills_dir = skills_override or (cwd / "skills")
    _, bridge_address, _manifest = await setup_skills_directory(
        skills_dir, mcp_servers, server_cache,
        bridge_socket_path=executor.bridge_socket_path, bridge_cache=bridge_cache,
    )
    set_bridge_env(bridge_address)
    return (
        "Tools are available as agentd skills (MCP servers exposed through the `skills` CLI on PATH): "
        "run `skills list`, `skills read <skill>`, and follow each skill's instructions."
    )


async def handle_completion(
    *, client_obj, harness_name, model, messages, mcp_servers, cwd, executor,
    server_cache, bridge_cache, skills_override, session_id=None,
):
    from agentd.llm_dispatch import _make_openai_shaped_response

    harness, conversations = _state(client_obj, executor, harness_name)
    manifest = await _prepare_skills(executor, cwd, mcp_servers, server_cache, bridge_cache, skills_override)
    final = None
    async for event in run_turn(
        harness_name=harness_name, harness=harness, conversations=conversations,
        model=model, messages=messages, cwd=cwd, tool_manifest=manifest, session_id=session_id,
    ):
        if event.kind == "result":
            final = event
    if final is None:
        raise RuntimeError(f"harness {harness_name} ended without a result")
    response = _make_openai_shaped_response(final.text, model or harness_name)
    response["agentd"] = {"harness": harness_name, "session_id": final.session_id, "is_error": final.is_error}
    return response


async def stream_completion(
    *, client_obj, harness_name, model, messages, mcp_servers, cwd, executor,
    server_cache, bridge_cache, skills_override, session_id=None,
):
    """OpenAI-style chunks: assistant text as it arrives, then a stop chunk."""
    from agentd.llm_dispatch import _make_openai_shaped_chunk

    harness, conversations = _state(client_obj, executor, harness_name)
    manifest = await _prepare_skills(executor, cwd, mcp_servers, server_cache, bridge_cache, skills_override)
    first = True
    async for event in run_turn(
        harness_name=harness_name, harness=harness, conversations=conversations,
        model=model, messages=messages, cwd=cwd, tool_manifest=manifest, streaming=True,
        session_id=session_id,
    ):
        if event.kind == "text":
            text = event.text if first else TEXT_SEPARATOR + event.text
            yield _make_openai_shaped_chunk(text, model or harness_name, include_role=first)
            first = False
    yield _make_openai_shaped_chunk(None, model or harness_name, finish_reason="stop", include_role=first)

