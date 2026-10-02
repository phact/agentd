"""Smart LLM-call dispatch.

agentd's ``patch_openai_with_ptc`` historically routed every call through
LiteLLM (any vendor as long as you supply the right API key). That leaves
Claude Pro / Max subscribers without an API key out, so ``smart_completion``
/ ``smart_acompletion`` dispatch:

  * ``model="gpt-*"`` (or any non-claude provider) ->   LiteLLM.
  * ``model="claude-*"`` + ``ANTHROPIC_API_KEY`` set -> LiteLLM.
  * ``model="claude-*"`` + no key -> the ``claude`` CLI as a pure model
    transport (``claude -p --output-format stream-json`` with every built-in
    tool disabled), billed to the subscription. With a sandbox executor it
    runs inside the sandbox, whose host proxy adds the credential; otherwise
    it runs the host's logged-in CLI. Its text is returned as an OpenAI
    ``ChatCompletion`` so downstream code is unchanged.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import aclosing
import shutil
import subprocess
from functools import lru_cache
from typing import Any

import litellm


class _DualAccess(dict):
    """A dict that also exposes its keys as attributes.

    LiteLLM returns dict-shaped responses that agentd accesses two ways:
    via attribute (`response.choices[0].message.content`) and via subscript
    (`response['choices'][0]['message']['content']`). Real LiteLLM
    ``ModelResponse`` objects support both. Our hand-built claude CLI
    responses need to do the same.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

_CLAUDE_MODEL_PREFIXES = ("claude-", "anthropic/")


def _run_async(coro):
    """Run an async coroutine from a sync context, even if a loop is active in the calling thread.

    ``asyncio.run`` refuses to start when a loop is already running. That can
    happen in mixed sync/async stacks (e.g. LiteLLM has spun up a loop, or an
    executor was called from inside another loop). To stay robust we drop into
    a dedicated thread that owns its own fresh loop.
    """
    import concurrent.futures

    def _thread_target():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_thread_target).result()


def _is_claude_model(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) for p in _CLAUDE_MODEL_PREFIXES)


@lru_cache(maxsize=1)
def _claude_cli_available() -> bool:
    """True if the ``claude`` CLI binary is on PATH and reports a version."""
    if shutil.which("claude") is None:
        return False
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, timeout=4, text=True
        )
        return out.returncode == 0
    except Exception:
        return False


def _in_sandbox(executor: Any) -> bool:
    from agentd.sandbox.executor import SandboxExecutor

    return isinstance(executor, SandboxExecutor)


def _should_route_to_claude_cli(model: str | None, api_key: str | None, executor: Any = None) -> bool:
    if not _is_claude_model(model):
        return False
    # If the caller already has an Anthropic key, LiteLLM works and is faster.
    if api_key:
        return False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return False
    return _in_sandbox(executor) or _claude_cli_available()


def _flatten_messages_to_prompt(messages: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Split an OpenAI chat-style messages list into (system_prompt, user_prompt).

    ``claude -p`` takes a single prompt (stdin) + a separate system prompt;
    multi-turn conversation history is encoded inline into the user prompt
    with simple ``Role: text`` lines. For PTC-style usage this is
    sufficient — the conversation tends to be one system + one user message.
    """
    system_parts: list[str] = []
    convo_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, list):
            # OpenAI's structured content; pull text chunks.
            text_chunks = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            content = "\n".join(text_chunks)
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        if role == "system":
            system_parts.append(content)
        else:
            convo_parts.append(f"{role.capitalize()}: {content}" if len(messages) > 2 else content)
    system_prompt = "\n\n".join(p for p in system_parts if p) or None
    user_prompt = "\n\n".join(convo_parts).strip()
    return system_prompt, user_prompt


def _make_openai_shaped_response(content: str, model: str) -> _DualAccess:
    """Build a ChatCompletion-shaped object the rest of agentd can read.

    Only the fields agentd's ``_extract_content`` and the surrounding code
    actually touch are populated: ``choices[0].message.content``,
    ``choices[0].finish_reason``, ``model``, ``usage`` (zeroed).
    """
    message = _DualAccess(
        role="assistant",
        content=content,
        tool_calls=None,
        function_call=None,
    )
    choice = _DualAccess(
        index=0,
        message=message,
        finish_reason="stop",
    )
    usage = _DualAccess(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    return _DualAccess(
        id="chatcmpl-claude-cli",
        object="chat.completion",
        model=model,
        choices=[choice],
        usage=usage,
    )


def _make_openai_shaped_chunk(
    content_delta: str | None,
    model: str,
    finish_reason: str | None = None,
    include_role: bool = False,
) -> _DualAccess:
    """Build a ChatCompletionChunk-shaped object for streaming."""
    delta = _DualAccess(
        role="assistant" if include_role else None,
        content=content_delta,
        tool_calls=None,
        function_call=None,
    )
    choice = _DualAccess(
        index=0,
        delta=delta,
        finish_reason=finish_reason,
    )
    return _DualAccess(
        id="chatcmpl-claude-cli-stream",
        object="chat.completion.chunk",
        model=model,
        choices=[choice],
    )


def _transport_argv(model: str, system_prompt: str | None, kwargs: dict[str, Any]) -> list[str]:
    """``claude -p`` arguments for the PTC model transport.

    PTC parses code fences out of the text and runs them through its own
    executor, so every built-in Claude Code tool is disabled (``--tools ""``),
    no MCP servers load, and no user/project settings, CLAUDE.md or skills
    leak into the prompt. ``claude_tools`` / ``setting_sources`` /
    ``allowed_tools`` / ``disallowed_tools`` override explicitly.
    """
    from agentd.harness.claude_cli import claude_argv

    extra: list[str] = []
    if "allowed_tools" in kwargs:
        extra += ["--allowedTools", ",".join(kwargs["allowed_tools"])]
    if "disallowed_tools" in kwargs:
        extra += ["--disallowedTools", ",".join(kwargs["disallowed_tools"])]
    bare = model.split("/", 1)[1] if model.startswith("anthropic/") else model
    return claude_argv(
        model=bare,
        system_prompt=system_prompt,
        tools=list(kwargs.get("claude_tools", [])),
        setting_sources=list(kwargs.get("setting_sources", [])),
        strict_mcp_config=True,
        extra_args=extra,
    )


async def _claude_cli_events(model: str, messages: list[dict[str, Any]], **kwargs):
    """Run one transport turn; yields HarnessEvents (text, ..., result)."""
    from agentd.harness import claude_cli

    system_prompt, user_prompt = _flatten_messages_to_prompt(messages)
    argv = _transport_argv(model, system_prompt, kwargs)
    executor, cwd = kwargs.get("executor"), kwargs.get("cwd")
    if _in_sandbox(executor):
        session = await executor.ensure_session(cwd or ".")
        events = claude_cli.run_in_sandbox(executor, argv, user_prompt, cwd=str(session.workspace))
    else:
        events = claude_cli.run_on_host(argv, user_prompt, cwd=cwd)
    async with aclosing(events):  # abandoning the call stops the CLI
        async for event in events:
            if event.kind == "result" and event.is_error:
                raise RuntimeError(f"claude CLI transport failed: {event.text}")
            yield event


async def _call_claude_cli(model: str, messages: list[dict[str, Any]], **kwargs) -> _DualAccess:
    """Run a single non-streaming turn, return an OpenAI-shaped response."""
    async with aclosing(_claude_cli_events(model, messages, **kwargs)) as events:
        parts = [e.text async for e in events if e.kind == "text"]
    content = "\n".join(parts).strip() or "(no response)"
    return _make_openai_shaped_response(content, model)


async def _claude_cli_stream_async(model: str, messages: list[dict[str, Any]], **kwargs):
    """Async generator: yield OpenAI-shaped chunks, one per assistant text block."""
    first = True
    async with aclosing(_claude_cli_events(model, messages, **kwargs)) as events:
        async for event in events:
            if event.kind == "text" and event.text:
                yield _make_openai_shaped_chunk(event.text, model, include_role=first)
                first = False
    yield _make_openai_shaped_chunk(None, model, finish_reason="stop", include_role=first)


def _claude_cli_stream_sync(model: str, messages: list[dict[str, Any]], **kwargs):
    """Sync generator wrapping the async stream (buffered; use the async path
    for incremental streaming)."""
    collected: list[_DualAccess] = []

    async def _collect() -> None:
        async for chunk in _claude_cli_stream_async(model, messages, **kwargs):
            collected.append(chunk)

    _run_async(_collect())
    yield from collected


# Keys consumed by the claude CLI path but not understood by LiteLLM. We strip
# them before forwarding so LiteLLM doesn't surface "unexpected keyword" errors.
_AGENTD_ONLY_KWARGS = ("cwd", "allowed_tools", "disallowed_tools", "executor", "claude_tools", "setting_sources")


def _strip_for_litellm(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _AGENTD_ONLY_KWARGS}


def smart_completion(*, model: str, messages: list[dict[str, Any]], api_key: str | None = None, stream: bool = False, **kwargs) -> Any:
    """Drop-in sync replacement for ``litellm.completion`` with claude CLI fallback."""
    if stream:
        if _should_route_to_claude_cli(model, api_key, kwargs.get("executor")):
            return _claude_cli_stream_sync(model, messages, **kwargs)
        return litellm.completion(model=model, messages=messages, api_key=api_key, stream=True, **_strip_for_litellm(kwargs))

    if _should_route_to_claude_cli(model, api_key, kwargs.get("executor")):
        return _run_async(_call_claude_cli(model, messages, **kwargs))

    return litellm.completion(model=model, messages=messages, api_key=api_key, **_strip_for_litellm(kwargs))


async def smart_acompletion(*, model: str, messages: list[dict[str, Any]], api_key: str | None = None, stream: bool = False, **kwargs) -> Any:
    """Drop-in async replacement for ``litellm.acompletion`` with claude CLI fallback."""
    if stream:
        if _should_route_to_claude_cli(model, api_key, kwargs.get("executor")):
            return _claude_cli_stream_async(model, messages, **kwargs)
        return await litellm.acompletion(model=model, messages=messages, api_key=api_key, stream=True, **_strip_for_litellm(kwargs))

    if _should_route_to_claude_cli(model, api_key, kwargs.get("executor")):
        return await _call_claude_cli(model, messages, **kwargs)

    return await litellm.acompletion(model=model, messages=messages, api_key=api_key, **_strip_for_litellm(kwargs))


__all__ = ["smart_completion", "smart_acompletion"]
