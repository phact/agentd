"""Google Calendar, as a native connector (docs/connectors.md).

Scopes (both non-sensitive): ``calendar.app.created`` (the agent's own
calendar, which it creates on first use and fully manages; the human overlays
it in Google Calendar) and ``calendar.events.freebusy`` (busy blocks, no titles
or details, on calendars the human can see).

Options: ``calendar_name`` (default "Agent"), ``freebusy_calendars`` (default
``["primary"]``: what ``freebusy`` and ``suggest_time`` check when the agent
names none), ``time_zone`` (for the agent's calendar; default UTC), ``api_base``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

API = "https://www.googleapis.com/calendar/v3"

TOOLS = {
    "events_list": "read",
    "event_get": "read",
    "freebusy": "read",
    "suggest_time": "read",
    "event_create": "write",
    "event_update": "write",
    "event_delete": "destructive",
}


# The scope each tool needs (Google lets the human untick either at consent).
SCOPES = {"freebusy": "calendar.events.freebusy", "suggest_time": "calendar.events.freebusy",
          **{t: "calendar.app.created" for t in ("events_list", "event_get", "event_create", "event_update",
                                                  "event_delete")}}


class CalendarError(RuntimeError):
    pass


async def _api(ctx, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
    base = ctx.options.get("api_base", API)
    for attempt in range(2):
        token = await ctx.token()
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.request(method, base + path, params=params, json=body,
                                   headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401 and attempt == 0:  # the cached access token expired early
            ctx.forget_token()
            continue
        if r.status_code >= 400:
            try:
                message = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = r.text[:200]
            raise CalendarError(f"Google Calendar {r.status_code}: {message}")
        return r.json() if r.content else {}
    raise CalendarError("Google Calendar kept refusing the access token")


async def _own(ctx, create: bool) -> str | None:
    """The agent's own calendar (made with calendar.app.created), created on first need."""
    cid = ctx.state.get("calendar_id")
    if cid or not create:
        return cid
    made = await _api(ctx, "POST", "/calendars", body={
        "summary": ctx.options.get("calendar_name", "Agent"),
        "description": "Managed by an agent through agentd",
        "timeZone": ctx.options.get("time_zone", "UTC")})
    ctx.state["calendar_id"] = made["id"]
    ctx.save()
    return made["id"]


def _event(e: dict) -> dict:
    return {k: e.get(k) for k in ("id", "summary", "description", "location", "status", "htmlLink")
            if e.get(k) is not None} | {"start": _when(e.get("start")), "end": _when(e.get("end"))}


def _when(v: dict | None) -> str | None:
    return (v or {}).get("dateTime") or (v or {}).get("date")


def _time(value: str, tz: str | None) -> dict:
    """An event time: a date (all day) or a date-time (RFC 3339)."""
    if len(value) == 10:
        return {"date": value}
    return {"dateTime": value, **({"timeZone": tz} if tz else {})}


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _calendars(ctx, calendars: str) -> list[str]:
    named = [c.strip() for c in calendars.split(",") if c.strip()]
    return named or list(ctx.options.get("freebusy_calendars", ["primary"]))


async def events_list(ctx, time_min: str, time_max: str) -> list:
    """Events on the agent's own calendar between two times (RFC 3339, e.g. 2026-10-27T00:00:00-04:00).

    time_min: start of the range
    time_max: end of the range
    """
    cid = await _own(ctx, create=False)
    if not cid:
        return []
    try:
        data = await _api(ctx, "GET", f"/calendars/{cid}/events", params={
            "timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "orderBy": "startTime",
            "maxResults": 250})
    except CalendarError as e:
        if " 404:" in str(e):  # the human deleted the agent's calendar
            ctx.state.pop("calendar_id", None)
            ctx.save()
            return []
        raise
    return [_event(e) for e in data.get("items", [])]


async def event_get(ctx, event_id: str) -> dict:
    """One event on the agent's own calendar.

    event_id: the event's id
    """
    cid = await _own(ctx, create=False)
    if not cid:
        raise CalendarError("the agent's calendar doesn't exist yet")
    return _event(await _api(ctx, "GET", f"/calendars/{cid}/events/{event_id}"))


async def freebusy(ctx, time_min: str, time_max: str, calendars: str = "") -> dict:
    """When people are busy (busy blocks only, no titles or details), on calendars the human can see.

    time_min: start of the range (RFC 3339)
    time_max: end of the range (RFC 3339)
    calendars: comma-separated calendar ids or emails (default: the human's own, as configured)
    """
    ids = _calendars(ctx, calendars)
    data = await _api(ctx, "POST", "/freeBusy", body={"timeMin": time_min, "timeMax": time_max,
                                                      "items": [{"id": i} for i in ids]})
    out = {}
    for cid, info in data.get("calendars", {}).items():
        out[cid] = {"busy": info.get("busy", [])}
        if info.get("errors"):  # e.g. notFound: they don't share free/busy with the human
            out[cid]["errors"] = [e.get("reason") for e in info["errors"]]
    return out


async def suggest_time(ctx, duration_minutes: int, window_start: str, window_end: str, calendars: str = "",
                       count: int = 3) -> list:
    """Free slots of a given length when everyone (and the agent's own calendar) is free.

    duration_minutes: how long the slot must be
    window_start: earliest start (RFC 3339)
    window_end: latest end (RFC 3339)
    calendars: comma-separated calendar ids or emails to check (default: the human's own)
    count: how many slots to return
    """
    ids = _calendars(ctx, calendars)
    own = await _own(ctx, create=False)
    busy_map = await freebusy(ctx, window_start, window_end, ",".join(ids + ([own] if own else [])))
    busy = sorted((_parse(b["start"]), _parse(b["end"])) for info in busy_map.values() for b in info["busy"])
    start, end = _parse(window_start), _parse(window_end)
    step, length = timedelta(minutes=15), timedelta(minutes=int(duration_minutes))
    t = start + timedelta(minutes=(-start.minute) % 15, seconds=-start.second, microseconds=-start.microsecond)
    slots = []
    while t + length <= end and len(slots) < int(count):
        clash = next((b for b in busy if b[0] < t + length and b[1] > t), None)
        if clash is None:
            slots.append({"start": t.astimezone(start.tzinfo).isoformat(),
                          "end": (t + length).astimezone(start.tzinfo).isoformat()})
            t += length
        else:
            t = max(t + step, clash[1])
            t += timedelta(minutes=(-t.minute) % 15, seconds=-t.second, microseconds=-t.microsecond)
    return slots


async def event_create(ctx, summary: str, start: str, end: str, description: str = "", location: str = "",
                       time_zone: str = "") -> dict:
    """Create an event on the agent's own calendar (created on first use; the human sees it overlaid).

    summary: the title
    start: RFC 3339 date-time, or YYYY-MM-DD for all day
    end: RFC 3339 date-time, or YYYY-MM-DD (exclusive) for all day
    description: details
    location: where
    time_zone: IANA zone for the times (e.g. America/New_York), if they carry no offset
    """
    cid = await _own(ctx, create=True)
    body = {"summary": summary, "start": _time(start, time_zone or None), "end": _time(end, time_zone or None)}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    return _event(await _api(ctx, "POST", f"/calendars/{cid}/events", body=body))


async def event_update(ctx, event_id: str, summary: str = "", start: str = "", end: str = "",
                       description: str = "", location: str = "", time_zone: str = "") -> dict:
    """Change an event on the agent's own calendar (only the fields given).

    event_id: the event's id
    summary: a new title
    start: a new start (RFC 3339, or YYYY-MM-DD)
    end: a new end (RFC 3339, or YYYY-MM-DD)
    description: new details
    location: a new place
    time_zone: IANA zone for the times
    """
    cid = await _own(ctx, create=False)
    if not cid:
        raise CalendarError("the agent's calendar doesn't exist yet")
    body: dict[str, Any] = {}
    for k, v in (("summary", summary), ("description", description), ("location", location)):
        if v:
            body[k] = v
    if start:
        body["start"] = _time(start, time_zone or None)
    if end:
        body["end"] = _time(end, time_zone or None)
    return _event(await _api(ctx, "PATCH", f"/calendars/{cid}/events/{event_id}", body=body))


async def event_delete(ctx, event_id: str) -> dict:
    """Delete an event from the agent's own calendar.

    event_id: the event's id
    """
    cid = await _own(ctx, create=False)
    if not cid:
        raise CalendarError("the agent's calendar doesn't exist yet")
    await _api(ctx, "DELETE", f"/calendars/{cid}/events/{event_id}")
    return {"deleted": event_id}
