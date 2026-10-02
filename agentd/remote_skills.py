"""``fleet_*`` tools: other boxes' agent sessions as skills (see agentd.remote).

:func:`register` adds them to agentd's ``@tool`` registry; use
:func:`agentd.remote.enable_fleet_skills` rather than calling it directly.
"""
from __future__ import annotations

from typing import Any

from agentd.remote import fleet, output_text
from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY, tool


def _drivable(box: str):
    b = fleet().box(box)
    if not b.drive:
        raise PermissionError(f"box {box!r} is read-only for this agent (set \"drive\": true in fleet.json)")
    return b


async def fleet_boxes() -> list:
    """List the configured boxes, whether each is reachable, and whether this agent may drive sessions there."""
    out = []
    for name, b in fleet().boxes.items():
        try:
            info = await b.info()
            ready = info.get("ready") or {}
            out.append({"box": name, "reachable": True, "drive": b.drive,
                        "harnesses": [h for h in info.get("harnesses", []) if ready.get(h, True)]})
        except Exception as e:  # noqa: BLE001
            out.append({"box": name, "reachable": False, "drive": b.drive, "error": str(e)})
    return out


async def fleet_harnesses(box: str) -> list:
    """The agent harnesses on a box: whether each is ready (and why not), its default model and its models.

    box: box name from fleet_boxes
    """
    return [{"harness": h["name"], "ready": h["ready"], "reasons": h["reasons"], "default_model": h["default_model"],
             "models": [m["id"] for m in h["models"]]} for h in await fleet().box(box).harnesses()]


async def fleet_sessions(box: str, legacy: bool = False) -> list:
    """List agent sessions on a box, newest first.

    box: box name from fleet_boxes
    legacy: list raw Claude Code sessions run outside agentd instead
    """
    b = fleet().box(box)
    if legacy:
        return [{k: s.get(k) for k in ("id", "title", "cwd", "modified")} for s in await b.legacy_sessions()]
    return [{k: s.get(k) for k in ("id", "title", "harness", "workspace", "owner", "last_activity", "sandbox",
                                    "running_response_id")} for s in await b.sessions()]


async def fleet_read(box: str, session: str, query: str = "", limit: int = 40) -> dict:
    """Read a session's conversation (agentd sessions start with "ses_"; other ids are legacy Claude Code sessions).

    box: box name
    session: session id
    query: only return entries containing this text (case-insensitive)
    limit: how many recent entries to return
    """
    b = fleet().box(box)
    if session.startswith("ses_"):
        page = await b.transcript(session, limit=500 if query else limit, order="desc")
        entries = [{"role": m.get("role"), "text": m.get("content")} for m in page["data"]]
    else:
        page = await b.legacy_transcript(session, limit=500 if query else limit, order="desc")
        entries = []
        for rec in page["data"]:
            msg = rec.get("message") or {}
            content = msg.get("content")
            text = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text")
            if text:
                entries.append({"role": msg.get("role") or rec.get("type"), "text": text})
    if query:
        entries = [e for e in entries if query.lower() in str(e["text"]).lower()]
    entries = list(reversed(entries[:limit]))
    return {"entries": entries, "more": page.get("next_cursor") is not None}


async def fleet_start(box: str, prompt: str, workspace: str = "", harness: str = "", background: bool = False) -> dict:
    """Start a new sandboxed agent session on a box with a first prompt.

    box: box name (must be drivable)
    prompt: the task
    workspace: workspace directory name on that box (default: a fresh one)
    harness: claude-code or codex (default: the box's default)
    background: return at once with a response id instead of waiting for the reply
    """
    r = await _drivable(box).start(prompt, workspace=workspace or None, harness=harness or None,
                                   background=background)
    return _summary(r)


async def fleet_send(box: str, session: str, prompt: str, background: bool = False) -> dict:
    """Send a turn to a session on a box and return the reply (or, in the background, a response id to check).

    box: box name (must be drivable)
    session: agentd session id ("ses_...")
    prompt: the message
    background: return at once; check later with fleet_result
    """
    return _summary(await _drivable(box).send(session, prompt, background=background))


async def fleet_result(box: str, response_id: str) -> dict:
    """Status and reply of a turn started earlier (e.g. in the background or by a schedule).

    box: box name
    response_id: the turn's response id
    """
    return _summary(await fleet().box(box).response(response_id))


async def fleet_cancel(box: str, session: str) -> dict:
    """Stop the turn running in a session on a box.

    box: box name (must be drivable)
    session: agentd session id
    """
    b = _drivable(box)
    running = (await b.session(session)).get("running_response_id")
    if not running:
        return {"cancelled": False, "reason": "no turn is running"}
    return {"cancelled": True, **_summary(await b.cancel(running))}


async def fleet_schedule(box: str, prompt: str, every: str = "", at: str = "", session: str = "",
                         timezone: str = "", workspace: str = "") -> dict:
    """Schedule turns on a box: every interval ("30m", "2h") or cron ("0 9 * * 1-5"), or once at an RFC 3339 time.

    box: box name (must be drivable)
    prompt: what to send each time
    every: interval or 5-field cron expression
    at: one-time RFC 3339 timestamp with offset
    session: send to this session (default: a new session per run)
    timezone: IANA time zone for cron (default: the box's)
    workspace: workspace for new sessions
    """
    s = await _drivable(box).schedule(prompt, every=every or None, at=at or None, session_id=session or None,
                                      timezone=timezone or None, workspace=workspace or None)
    return {k: s.get(k) for k in ("id", "next_run", "every", "at", "session_id", "timezone")}


async def fleet_unschedule(box: str, schedule_id: str) -> dict:
    """Remove a schedule on a box.

    box: box name (must be drivable)
    schedule_id: the schedule's id ("sch_...")
    """
    s = await _drivable(box).unschedule(schedule_id)
    return {"removed": s.get("id")}


def _summary(r: dict[str, Any]) -> dict[str, Any]:
    agentd = r.get("agentd") or {}
    out = {"response_id": r.get("id"), "status": r.get("status"), "session": agentd.get("session")}
    text = output_text(r)
    if text:
        out["reply"] = text
    if r.get("error"):
        out["error"] = r["error"].get("message")
    return out


TOOLS = (fleet_boxes, fleet_harnesses, fleet_sessions, fleet_read, fleet_start, fleet_send, fleet_result, fleet_cancel,
         fleet_schedule, fleet_unschedule)


def register() -> None:
    for func in TOOLS:
        tool(func)


def unregister() -> None:
    for func in TOOLS:
        FUNCTION_REGISTRY.pop(func.__name__, None)
        SCHEMA_REGISTRY.pop(func.__name__, None)
