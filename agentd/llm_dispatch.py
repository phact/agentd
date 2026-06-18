"""Smart LLM-call dispatch.

agentd's ``patch_openai_with_ptc`` historically routed every call through
LiteLLM (any vendor as long as you supply the right API key). That works
for most cases, but it doesn't take advantage of an *already-authenticated*
Claude Code CLI session — the same one the user runs locally to use their
Claude Pro / Max subscription. So Claude subscribers without an API key
get errors instead of subscription-billed answers.

This module wraps LiteLLM behind ``smart_completion`` / ``smart_acompletion``
that transparently dispatch:

  * ``model="gpt-*"`` (or any non-claude provider) →   LiteLLM.
  * ``model="claude-*"`` + ``ANTHROPIC_API_KEY`` set → LiteLLM (existing path).
  * ``model="claude-*"`` + no key but ``claude`` CLI logged in →
    ``claude_agent_sdk.query(...)`` for subscription-billed access.
    The streaming messages from the SDK are flattened into a single OpenAI
    ``ChatCompletion``-shaped object so downstream code is unchanged.

Streaming (``stream=True``) is **not** supported through the claude-sdk
route in this v1 — it still goes through LiteLLM. If the model is claude-*
and there's no API key, ``smart_completion(stream=True)`` raises with a
clear message asking the user to set ``ANTHROPIC_API_KEY``.
"""
from __future__ import annotations

import asyncio
import os
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
    ``ModelResponse`` objects support both. Our hand-built claude-sdk
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


def _should_route_to_claude_sdk(model: str | None, api_key: str | None) -> bool:
    if not _is_claude_model(model):
        return False
    # If the caller already has an Anthropic key, LiteLLM works and is faster.
    if api_key:
        return False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return False
    return _claude_cli_available()


def _flatten_messages_to_prompt(messages: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Split an OpenAI chat-style messages list into (system_prompt, user_prompt).

    The claude-agent-sdk takes a single prompt string + a separate system
    prompt; multi-turn conversation history is encoded inline into the user
    prompt with simple ``Role: text`` lines. For PTC-style usage this is
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
        id="chatcmpl-claude-sdk",
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
        id="chatcmpl-claude-sdk-stream",
        object="chat.completion.chunk",
        model=model,
        choices=[choice],
    )


def _build_claude_options(messages: list[dict[str, Any]], kwargs: dict[str, Any]):
    """Translate OpenAI-style messages + extras into a ClaudeAgentOptions object.

    When an ``executor`` is supplied, we attach a ``PreToolUse`` hook that
    intercepts the SDK's native ``Bash`` calls and routes them through that
    executor. Claude's bash commands run in agentd's configured sandbox
    (Docker, sandbox-runtime, …) instead of escaping to the host. Write /
    Edit / NotebookEdit are denied outright by default since PTC has no
    transformation for them. Callers can override either list explicitly.
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    system_prompt, user_prompt = _flatten_messages_to_prompt(messages)
    options_kwargs: dict[str, Any] = {}
    if system_prompt:
        options_kwargs["system_prompt"] = system_prompt
    cwd = kwargs.get("cwd")
    if cwd is not None:
        options_kwargs["cwd"] = str(cwd)
    # SDK isolation: when an OpenAI-surface call gets auto-routed through
    # claude-agent-sdk, we don't want the user's local Claude Code config
    # (~/.claude/settings.json, project-level CLAUDE.md, installed skills,
    # …) to leak into the agent's prompt — they pollute the model's view of
    # what tools/skills are available. Caller can opt back in by passing
    # ``setting_sources`` or ``skills`` explicitly.
    if "setting_sources" in kwargs:
        options_kwargs["setting_sources"] = kwargs["setting_sources"]
    else:
        options_kwargs["setting_sources"] = []
    if "skills" in kwargs:
        options_kwargs["skills"] = kwargs["skills"]
    else:
        options_kwargs["skills"] = []

    executor = kwargs.get("executor")
    if executor is not None:
        bash_hook = _make_bash_to_executor_hook(executor, cwd)
        options_kwargs["hooks"] = {
            "PreToolUse": [HookMatcher(matcher="Bash", hooks=[bash_hook])]
        }
        # Bash is allowed because the hook re-routes it through executor.
        default_disallowed = ["Write", "Edit", "NotebookEdit"]
    else:
        # No executor wired in → block native Bash/Write/Edit entirely so
        # PTC's text/code-fence pattern is the only execution path.
        default_disallowed = ["Bash", "Write", "Edit", "NotebookEdit"]

    if "disallowed_tools" in kwargs:
        options_kwargs["disallowed_tools"] = list(kwargs["disallowed_tools"])
    else:
        options_kwargs["disallowed_tools"] = default_disallowed
    if "allowed_tools" in kwargs:
        options_kwargs["allowed_tools"] = list(kwargs["allowed_tools"])
    return user_prompt, ClaudeAgentOptions(**options_kwargs)


def _extract_text_pieces(message: Any) -> list[str]:
    """Pull every TextBlock-shaped string out of a single SDK message."""
    pieces: list[str] = []
    content = getattr(message, "content", None)
    if content is None:
        txt = getattr(message, "text", None)
        if txt:
            pieces.append(txt)
        return pieces
    if isinstance(content, str):
        if content:
            pieces.append(content)
        return pieces
    if isinstance(content, list):
        for block in content:
            t = getattr(block, "text", None)
            if t:
                pieces.append(t)
    return pieces


async def _call_claude_sdk(model: str, messages: list[dict[str, Any]], **kwargs) -> _DualAccess:
    """Run a single non-streaming Claude SDK turn, return an OpenAI-shaped response."""
    try:
        from claude_agent_sdk import query
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "Routing to the Claude Agent SDK requires `claude-agent-sdk` to be "
            "installed (pip install claude-agent-sdk)."
        ) from e

    user_prompt, options = _build_claude_options(messages, kwargs)
    parts: list[str] = []
    async for message in query(prompt=user_prompt, options=options):
        parts.extend(_extract_text_pieces(message))
    content = "\n".join(parts).strip() or "(no response)"
    return _make_openai_shaped_response(content, model)


async def _claude_sdk_stream_async(model: str, messages: list[dict[str, Any]], **kwargs):
    """Async generator: yield OpenAI-shaped chunks from a claude-sdk query.

    Iterates the SDK's stream in real time. Each TextBlock becomes a chunk
    with ``delta.content`` set; on completion a final chunk with
    ``finish_reason="stop"`` is emitted. Tool-use blocks are skipped — PTC
    parses code fences out of the text content, not native tool calls.
    """
    try:
        from claude_agent_sdk import query
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "Routing to the Claude Agent SDK requires `claude-agent-sdk`."
        ) from e

    user_prompt, options = _build_claude_options(messages, kwargs)
    first = True
    async for message in query(prompt=user_prompt, options=options):
        for piece in _extract_text_pieces(message):
            yield _make_openai_shaped_chunk(piece, model, include_role=first)
            first = False
    yield _make_openai_shaped_chunk(None, model, finish_reason="stop", include_role=first)


def _claude_sdk_stream_sync(model: str, messages: list[dict[str, Any]], **kwargs):
    """Sync generator wrapping the async stream.

    The SDK is async-native; we buffer the async generator into a list and
    yield it synchronously. Total wall-clock latency is the same as the
    non-streaming path — chunks arrive at the end, not as they're produced
    — but the iteration contract matches what ``litellm.completion(stream=True)``
    would return, so callers don't need to change.

    Use the async path (``smart_acompletion``) if you want true incremental
    streaming through the claude-sdk route.
    """
    collected: list[_DualAccess] = []

    async def _collect() -> None:
        async for chunk in _claude_sdk_stream_async(model, messages, **kwargs):
            collected.append(chunk)

    _run_async(_collect())
    yield from collected


# Keys consumed by the claude-sdk path but not understood by LiteLLM. We strip
# them before forwarding so LiteLLM doesn't surface "unexpected keyword" errors.
_AGENTD_ONLY_KWARGS = ("cwd", "allowed_tools", "disallowed_tools", "executor")


def _make_bash_to_executor_hook(executor: Any, cwd: Any):
    """PreToolUse hook that re-runs Bash commands through ``executor``.

    The SDK's native Bash tool would otherwise execute on the host without
    any of agentd's sandbox configuration (no Docker isolation, no cwd
    scope, etc.). Instead we:

      1. Run the requested command through ``executor`` (which respects
         whatever agentd config the user wired up).
      2. Stash the captured output in a temp file on the host.
      3. Mutate the SDK's tool input so its Bash call becomes
         ``cat /tmp/agentd_xxx; exit <code>``. The SDK's Bash still runs,
         but it just prints the executor's output and exits with its
         exit code, so the model sees a normal tool result.

    Outputs flow through ``stdout`` only — executors already merge stderr
    into stdout, so the model sees the same combined stream it would have
    seen from the executor directly.
    """
    import tempfile
    from pathlib import Path as _Path
    import shlex

    cwd_path = _Path(cwd) if cwd else _Path(".")

    async def _hook(input_data, tool_use_id, context):
        if input_data.get("tool_name") != "Bash":
            return {}
        cmd = input_data.get("tool_input", {}).get("command", "")
        if hasattr(executor, "execute_bash_async"):
            output, code = await executor.execute_bash_async(cmd, cwd_path)
        else:
            output, code = executor.execute_bash(cmd, cwd_path)
        # Stash the captured output where the SDK's Bash can read it back.
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="agentd_", suffix=".txt", delete=False
        ) as f:
            f.write(output)
            tmp_path = f.name
        new_cmd = f"cat {shlex.quote(tmp_path)}; exit {int(code)}"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {"command": new_cmd},
            }
        }

    return _hook


def _strip_for_litellm(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _AGENTD_ONLY_KWARGS}


def smart_completion(*, model: str, messages: list[dict[str, Any]], api_key: str | None = None, stream: bool = False, **kwargs) -> Any:
    """Drop-in sync replacement for ``litellm.completion`` with claude-sdk fallback."""
    if stream:
        if _should_route_to_claude_sdk(model, api_key):
            return _claude_sdk_stream_sync(model, messages, **kwargs)
        return litellm.completion(model=model, messages=messages, api_key=api_key, stream=True, **_strip_for_litellm(kwargs))

    if _should_route_to_claude_sdk(model, api_key):
        return _run_async(_call_claude_sdk(model, messages, **kwargs))

    return litellm.completion(model=model, messages=messages, api_key=api_key, **_strip_for_litellm(kwargs))


async def smart_acompletion(*, model: str, messages: list[dict[str, Any]], api_key: str | None = None, stream: bool = False, **kwargs) -> Any:
    """Drop-in async replacement for ``litellm.acompletion`` with claude-sdk fallback."""
    if stream:
        if _should_route_to_claude_sdk(model, api_key):
            return _claude_sdk_stream_async(model, messages, **kwargs)
        return await litellm.acompletion(model=model, messages=messages, api_key=api_key, stream=True, **_strip_for_litellm(kwargs))

    if _should_route_to_claude_sdk(model, api_key):
        return await _call_claude_sdk(model, messages, **kwargs)

    return await litellm.acompletion(model=model, messages=messages, api_key=api_key, **_strip_for_litellm(kwargs))


__all__ = ["smart_completion", "smart_acompletion"]
