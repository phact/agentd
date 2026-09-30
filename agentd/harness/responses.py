"""Route patched ``responses.create(harness=...)`` calls to a harness.

Same turn logic as :mod:`agentd.harness.chat` (native resume when the history
matches a recorded run, otherwise seeded history), exposed as the Responses
API:

  * ``input`` (string or message items) plus ``instructions`` become the
    conversation; ``previous_response_id`` continues an earlier response
    without resending history. Response records are kept on disk
    (``~/.agentd/responses``), so this works after a restart too; on the
    same harness it resumes the native session by id, in a fresh sandbox.
  * ``session_id=`` (an ``agentd.session_id`` from an earlier response)
    resumes that native session directly.
  * The output is one assistant ``message`` item. The harness's own tool
    activity (shell, file edits, skills) is not exposed as output items; it
    goes to the agentd ConversationLog. Client-side ``tools`` are rejected:
    give harnesses tools through ``mcp_servers`` (they appear as skills).
  * ``stream=True`` yields the standard event sequence (created, in_progress,
    output_item/content_part added, output_text deltas and done, part/item
    done, completed or failed).
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from agentd.harness.chat import TEXT_SEPARATOR, _prepare_skills, _state, _text, run_turn
from agentd.sandbox.base import DEFAULT_HOME

_MAX_STORED = 1000
RESPONSES_DIR = DEFAULT_HOME / "responses"


def input_to_messages(input_data: Any, instructions: str | None = None) -> list[dict]:
    """Responses ``input``/``instructions`` as chat-style messages (text only)."""
    messages: list[dict] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
        return messages
    for item in input_data or []:
        item = item if isinstance(item, dict) else item.model_dump()
        kind = item.get("type", "message")
        if kind != "message" or "role" not in item:
            raise ValueError(f"harness Responses input supports message items only; got type={kind!r}")
        messages.append({"role": item["role"], "content": _text(item.get("content"))})
    return messages


def _stored(client_obj) -> "OrderedDict[str, dict]":
    if getattr(client_obj, "_harness_responses", None) is None:
        client_obj._harness_responses = OrderedDict()
    return client_obj._harness_responses


def _load(client_obj, response_id: str) -> dict | None:
    """A response record: {"harness", "session_id", "messages"} (memory, then disk)."""
    record = _stored(client_obj).get(response_id)
    if record is not None:
        return record
    path = RESPONSES_DIR / f"{response_id}.json"
    if "/" in response_id or not path.is_file():
        return None
    return json.loads(path.read_text())


def _conversation(client_obj, harness_name, input_data, instructions, previous_response_id):
    """(messages, native session to resume or None)."""
    new = input_to_messages(input_data, instructions if not previous_response_id else None)
    if not previous_response_id:
        return new, None
    prior = _load(client_obj, previous_response_id)
    if prior is None:
        raise ValueError(f"unknown previous_response_id {previous_response_id!r}")
    messages = prior["messages"]
    if instructions:
        new.insert(0, {"role": "system", "content": instructions})
        messages = [m for m in messages if m["role"] != "system"]
    resume = prior.get("session_id") if prior.get("harness") == harness_name else None
    return messages + new, resume


def _remember(client_obj, response_id: str, harness_name: str, session_id: str | None,
              messages: list[dict], reply: str) -> None:
    record = {"harness": harness_name, "session_id": session_id,
              "messages": messages + [{"role": "assistant", "content": reply}]}
    store = _stored(client_obj)
    store[response_id] = record
    while len(store) > _MAX_STORED:
        store.popitem(last=False)
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RESPONSES_DIR / f".{response_id}.tmp"
    tmp.write_text(json.dumps(record))
    os.replace(tmp, RESPONSES_DIR / f"{response_id}.json")


def _new_id(prefix: str) -> str:
    return f"{prefix}_agentd_{os.urandom(12).hex()}"


def _message(item_id: str, text: str, status: str):
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    content = [ResponseOutputText(type="output_text", text=text, annotations=[])] if status == "completed" or text else []
    return ResponseOutputMessage(id=item_id, type="message", role="assistant", status=status, content=content)


def _response(response_id, *, model, status, output, instructions, previous_response_id, error=None, agentd=None):
    from openai.types.responses import Response

    return Response.model_validate({
        "id": response_id,
        "object": "response",
        "created_at": time.time(),
        "model": model,
        "status": status,
        "output": [o.model_dump() for o in output],
        "error": error,
        "instructions": instructions,
        "previous_response_id": previous_response_id,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "agentd": agentd or {},
    })


def _check_kwargs(kwargs: dict) -> None:
    if kwargs.get("tools"):
        raise ValueError("harnesses do not take client-side tools; pass mcp_servers= (they appear as skills)")


async def handle_response(
    *, client_obj, harness_name, model, input_data, kwargs, mcp_servers, cwd, executor,
    server_cache, bridge_cache, skills_override,
):
    _check_kwargs(kwargs)
    instructions, previous = kwargs.get("instructions"), kwargs.get("previous_response_id")
    messages, resume = _conversation(client_obj, harness_name, input_data, instructions, previous)
    harness, conversations = _state(client_obj, executor, harness_name)
    manifest = await _prepare_skills(executor, cwd, mcp_servers, server_cache, bridge_cache, skills_override)

    final = None
    async for event in run_turn(
        harness_name=harness_name, harness=harness, conversations=conversations,
        model=model, messages=messages, cwd=cwd, tool_manifest=manifest,
        session_id=kwargs.get("session_id") or resume,
    ):
        if event.kind == "result":
            final = event
    if final is None:
        raise RuntimeError(f"harness {harness_name} ended without a result")

    response_id = _new_id("resp")
    failed = final.is_error
    if not failed:
        _remember(client_obj, response_id, harness_name, final.session_id, messages, final.text)
    return _response(
        response_id, model=model or harness_name, status="failed" if failed else "completed",
        output=[_message(_new_id("msg"), final.text, "completed")],
        instructions=instructions, previous_response_id=previous,
        error={"code": "server_error", "message": final.text} if failed else None,
        agentd={"harness": harness_name, "session_id": final.session_id, "is_error": failed},
    )


async def stream_response(
    *, client_obj, harness_name, model, input_data, kwargs, mcp_servers, cwd, executor,
    server_cache, bridge_cache, skills_override,
):
    from openai.types import responses as R

    _check_kwargs(kwargs)
    instructions, previous = kwargs.get("instructions"), kwargs.get("previous_response_id")
    messages, resume = _conversation(client_obj, harness_name, input_data, instructions, previous)
    harness, conversations = _state(client_obj, executor, harness_name)
    manifest = await _prepare_skills(executor, cwd, mcp_servers, server_cache, bridge_cache, skills_override)

    response_id, item_id = _new_id("resp"), _new_id("msg")
    model_name = model or harness_name
    seq = 0

    def nxt() -> int:
        nonlocal seq
        seq += 1
        return seq - 1

    def snapshot(status, output, error=None, agentd=None):
        return _response(response_id, model=model_name, status=status, output=output, instructions=instructions,
                         previous_response_id=previous, error=error, agentd=agentd)

    start = snapshot("in_progress", [])
    yield R.ResponseCreatedEvent(type="response.created", response=start, sequence_number=nxt())
    yield R.ResponseInProgressEvent(type="response.in_progress", response=start, sequence_number=nxt())
    yield R.ResponseOutputItemAddedEvent(type="response.output_item.added", output_index=0,
                                         item=_message(item_id, "", "in_progress"), sequence_number=nxt())
    yield R.ResponseContentPartAddedEvent(
        type="response.content_part.added", item_id=item_id, output_index=0, content_index=0,
        part=R.ResponseOutputText(type="output_text", text="", annotations=[]), sequence_number=nxt())

    texts: list[str] = []
    final = None
    async for event in run_turn(
        harness_name=harness_name, harness=harness, conversations=conversations,
        model=model, messages=messages, cwd=cwd, tool_manifest=manifest, streaming=True,
        session_id=kwargs.get("session_id") or resume,
    ):
        if event.kind == "text":
            delta = event.text if not texts else TEXT_SEPARATOR + event.text
            texts.append(event.text)
            yield R.ResponseTextDeltaEvent(type="response.output_text.delta", item_id=item_id, output_index=0,
                                           content_index=0, delta=delta, logprobs=[], sequence_number=nxt())
        elif event.kind == "result":
            final = event

    text = TEXT_SEPARATOR.join(texts)
    failed = final is None or final.is_error
    if final is not None and not text and final.text:
        text = final.text  # e.g. an error message with no streamed text
    part = R.ResponseOutputText(type="output_text", text=text, annotations=[])
    message = _message(item_id, text, "completed")
    yield R.ResponseTextDoneEvent(type="response.output_text.done", item_id=item_id, output_index=0,
                                  content_index=0, text=text, logprobs=[], sequence_number=nxt())
    yield R.ResponseContentPartDoneEvent(type="response.content_part.done", item_id=item_id, output_index=0,
                                         content_index=0, part=part, sequence_number=nxt())
    yield R.ResponseOutputItemDoneEvent(type="response.output_item.done", output_index=0, item=message,
                                        sequence_number=nxt())
    agentd = {"harness": harness_name, "session_id": final and final.session_id, "is_error": failed}
    if failed:
        error = {"code": "server_error", "message": (final and final.text) or "harness ended without a result"}
        yield R.ResponseFailedEvent(type="response.failed", sequence_number=nxt(),
                                    response=snapshot("failed", [message], error=error, agentd=agentd))
    else:
        _remember(client_obj, response_id, harness_name, final.session_id, messages, text)
        yield R.ResponseCompletedEvent(type="response.completed", sequence_number=nxt(),
                                       response=snapshot("completed", [message], agentd=agentd))
