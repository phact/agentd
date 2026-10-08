"""Google Calendar, as a native connector (docs/connectors.md).

Scopes (both non-sensitive): ``calendar.app.created`` (the agent's own
calendar, which it creates on first use and fully manages; the human overlays
it in Google Calendar) and ``calendar.events.freebusy`` (busy blocks, no titles
or details, on calendars the human can see).

On the agent's calendar it covers what the Events API offers there: times and
repetition (RFC 5545), alerts (reminders), guests (with or without Google
emailing them) and their permissions, Google Meet links, color, busy/free,
visibility, tentative, private and shared key/value properties, a source link,
quick add, search, and one occurrence / this and following / the whole series.
Not reachable with these scopes: focus time and out-of-office events (primary
calendar only), attachments (Drive), sharing the calendar (ACL), calendar-list
defaults and settings, push notifications (a public webhook).

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
    "event_instances": "read",
    "freebusy": "read",
    "suggest_time": "read",
    "calendar_info": "read",
    "event_create": "write",
    "event_quick_add": "write",
    "event_update": "write",
    "calendar_update": "write",
    "event_delete": "destructive",
}


# The scope each tool needs (Google lets the human untick either at consent).
SCOPES = {"freebusy": "calendar.events.freebusy", "suggest_time": "calendar.events.freebusy",
          **{t: "calendar.app.created" for t in TOOLS if t not in ("freebusy", "suggest_time")}}


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
    """Everything Google returns about an event, structured (snake_case, Google's defaults filled in), plus
    plain-language ``alerts`` and ``repeats`` for showing to a person."""
    start, end = e.get("start") or {}, e.get("end") or {}
    reminders = e.get("reminders") or {"useDefault": True}
    props = e.get("extendedProperties") or {}
    out: dict[str, Any] = {
        "id": e.get("id"),
        "summary": e.get("summary", ""),
        "description": e.get("description", ""),
        "location": e.get("location", ""),
        "start": _when(start), "end": _when(end),
        "all_day": "date" in start,
        "time_zone": start.get("timeZone") or end.get("timeZone"),
        "status": e.get("status", "confirmed"),
        "recurrence": e.get("recurrence") or [],
        "reminders": {"use_default": bool(reminders.get("useDefault")),
                      "overrides": sorted(({"method": r["method"], "minutes": r["minutes"]}
                                           for r in reminders.get("overrides") or []),
                                          key=lambda r: (r["minutes"], r["method"]))},
        "alerts": describe_reminders(reminders),
        "attendees": [{"email": a.get("email"), "display_name": a.get("displayName"),
                       "response": a.get("responseStatus", "needsAction"), "optional": bool(a.get("optional")),
                       "organizer": bool(a.get("organizer")), "self": bool(a.get("self")),
                       "comment": a.get("comment"), "additional_guests": a.get("additionalGuests", 0)}
                      for a in e.get("attendees") or []],
        "guests_can_modify": e.get("guestsCanModify", False),
        "guests_can_invite_others": e.get("guestsCanInviteOthers", True),
        "guests_can_see_other_guests": e.get("guestsCanSeeOtherGuests", True),
        "color": _COLOR_NAMES.get(e.get("colorId", ""), None),
        "show_as": "free" if e.get("transparency") == "transparent" else "busy",
        "visibility": e.get("visibility", "default"),
        "properties": props.get("private", {}),
        "shared_properties": props.get("shared", {}),
        "source": e.get("source"),
        "organizer": (e.get("organizer") or {}).get("email"),
        "creator": (e.get("creator") or {}).get("email"),
        "created": e.get("created"), "updated": e.get("updated"),
        "html_link": e.get("htmlLink"),
        "ical_uid": e.get("iCalUID"),
        "sequence": e.get("sequence", 0),
        "event_type": e.get("eventType", "default"),
    }
    if e.get("recurrence"):
        out["repeats"] = describe_recurrence(e["recurrence"])
    if e.get("recurringEventId"):  # an occurrence of a repeating event
        out["series_id"] = e["recurringEventId"]
        out["original_start"] = _when(e.get("originalStartTime"))
    conf = e.get("conferenceData")
    if conf or e.get("hangoutLink"):
        out["meet"] = e.get("hangoutLink") or next((p.get("uri") for p in (conf or {}).get("entryPoints", [])
                                                     if p.get("entryPointType") == "video"), None)
        out["conference"] = {"id": (conf or {}).get("conferenceId"),
                             "status": ((conf or {}).get("createRequest") or {}).get("status", {}).get("statusCode"),
                             "entry_points": [{"type": p.get("entryPointType"), "uri": p.get("uri"),
                                               "label": p.get("label"), "pin": p.get("pin")}
                                              for p in (conf or {}).get("entryPoints", [])]}
    if e.get("attachments"):
        out["attachments"] = [{"title": x.get("title"), "url": x.get("fileUrl"), "mime_type": x.get("mimeType")}
                              for x in e["attachments"]]
    if e.get("locked"):
        out["locked"] = True
    return out


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


# --------------------------------------------------------------------------- #
# Event fields beyond the times: alerts, guests, Meet, color, ...
# --------------------------------------------------------------------------- #

COLORS = {"lavender": "1", "sage": "2", "grape": "3", "flamingo": "4", "banana": "5", "tangerine": "6",
          "peacock": "7", "graphite": "8", "blueberry": "9", "basil": "10", "tomato": "11"}
_COLOR_NAMES = {v: k for k, v in COLORS.items()}
_EMAIL = __import__("re").compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def _minutes(amount: str) -> int:
    amount = amount.strip().lower()
    unit = amount[-1:] if amount[-1:] in "mhdw" else "m"
    number = amount[:-1] if amount[-1:] in "mhdw" else amount
    if not number.isdigit():
        raise CalendarError(f"not a reminder time: {amount!r} (e.g. 10m, 2h, 1d, 1w)")
    return int(number) * {"m": 1, "h": 60, "d": 1440, "w": 10080}[unit]


def _reminders(spec: str) -> dict:
    """"popup:10m,email:1d" (up to 5, 0 to 4 weeks before), "default" or "none"."""
    s = spec.strip().lower()
    if s == "default":
        return {"useDefault": True}
    if s == "none":
        return {"useDefault": False, "overrides": []}
    overrides = []
    for part in [x for x in s.split(",") if x.strip()]:
        method, _, amount = part.strip().partition(":")
        method = {"popup": "popup", "notification": "popup", "alert": "popup", "email": "email"}.get(method.strip())
        if method is None or not amount:
            raise CalendarError(f"not a reminder: {part.strip()!r} (e.g. popup:10m, email:1d)")
        minutes = _minutes(amount)
        if not 0 <= minutes <= 40320:
            raise CalendarError("reminders go from 0 minutes to 4 weeks before")
        overrides.append({"method": method, "minutes": minutes})
    if len(overrides) > 5:
        raise CalendarError("at most 5 reminders per event")
    return {"useDefault": False, "overrides": overrides}


def _ago(minutes: int) -> str:
    for unit, size in (("week", 10080), ("day", 1440), ("hour", 60)):
        if minutes and minutes % size == 0:
            n = minutes // size
            return f"{n} {unit}{'s' if n > 1 else ''} before"
    return "at the start" if minutes == 0 else f"{minutes} min before"


def describe_reminders(reminders: dict | None) -> str:
    if not reminders or reminders.get("useDefault"):
        return "the calendar's default alerts"
    o = sorted(reminders.get("overrides") or [], key=lambda r: (r["minutes"], r["method"]))  # Google reorders
    if not o:
        return "no alerts"
    return ", ".join(f"{_ago(r['minutes'])} ({'email' if r['method'] == 'email' else 'notification'})" for r in o)


def _guests(spec: str) -> list[tuple[str, str, bool]]:
    """"sam@x.com, ?pat@y.com" (? = optional), with +/- to add or remove: [(op, email, optional)]."""
    out = []
    for item in [x.strip() for x in spec.split(",") if x.strip()]:
        op = item[0] if item[0] in "+-" else "="
        item = item.lstrip("+-").strip()
        optional = item.startswith("?")
        email = item.lstrip("?").strip()
        if not _EMAIL.match(email):
            raise CalendarError(f"not an email address: {email!r}")
        out.append((op, email, optional))
    ops = {op for op, _, _ in out}
    if "=" in ops and ops - {"="}:
        raise CalendarError("give either a whole guest list, or changes with + and -, not both")
    return out


def _attendees(spec: str, current: list[dict]) -> list[dict]:
    items = _guests(spec)
    if items and items[0][0] == "=":
        return [{"email": e, **({"optional": True} if opt else {})} for _, e, opt in items]
    by_email = {a["email"].lower(): a for a in current}
    for op, e, opt in items:
        if op == "-":
            by_email.pop(e.lower(), None)
        else:
            by_email[e.lower()] = {"email": e, **({"optional": True} if opt else {})}
    return list(by_email.values())


def _yesno(value: str, name: str) -> bool | None:
    v = value.strip().lower()
    if not v:
        return None
    if v in ("yes", "true"):
        return True
    if v in ("no", "false"):
        return False
    raise CalendarError(f"{name} is yes or no")


def _props(value: str, name: str) -> dict[str, str] | None:
    if not value.strip():
        return None
    import json

    try:
        data = json.loads(value)
    except ValueError:
        raise CalendarError(f'{name} is a JSON object of strings, e.g. {{"task": "123"}}') from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise CalendarError(f'{name} is a JSON object of strings, e.g. {{"task": "123"}}')
    return data


_ENUMS = {"show_as": ("busy", "free"), "visibility": ("default", "public", "private", "confidential"),
          "status": ("confirmed", "tentative"), "meet": ("add", "remove")}


def _validate(args: dict) -> None:
    """Field values that can be checked before anyone is asked."""
    if args.get("reminders"):
        _reminders(args["reminders"])
    if args.get("attendees"):
        _guests(args["attendees"])
    if args.get("color") and args["color"].lower() not in COLORS:
        raise CalendarError(f"color is one of {', '.join(COLORS)}")
    for name, allowed in _ENUMS.items():
        if args.get(name) and args[name].lower() not in allowed:
            raise CalendarError(f"{name} is one of {', '.join(allowed)}")
    for name in ("guests_can_modify", "guests_can_invite_others", "guests_can_see_other_guests"):
        _yesno(args.get(name) or "", name)
    for name in ("properties", "shared_properties"):
        _props(args.get(name) or "", name)
    if args.get("source_url") and not args["source_url"].startswith(("https://", "http://")):
        raise CalendarError("source_url is an http(s) link")


def _fields(args: dict, current: dict | None = None) -> tuple[dict, dict]:
    """(event body, query params) for the fields given; ``current`` is the event being changed."""
    body: dict[str, Any] = {}
    params: dict[str, Any] = {"sendUpdates": "all" if args.get("notify_guests") else "none"}
    tz = args.get("time_zone") or None
    for k in ("summary", "description", "location"):
        if args.get(k):
            body[k] = args[k]
    if args.get("start"):
        body["start"] = _time(args["start"], tz)
    if args.get("end"):
        body["end"] = _time(args["end"], tz)
    if args.get("reminders"):
        body["reminders"] = _reminders(args["reminders"])
    if args.get("attendees"):
        body["attendees"] = _attendees(args["attendees"], (current or {}).get("attendees") or [])
    if args.get("color"):
        body["colorId"] = COLORS[args["color"].lower()]
    if args.get("show_as"):
        body["transparency"] = "transparent" if args["show_as"].lower() == "free" else "opaque"
    if args.get("visibility"):
        body["visibility"] = args["visibility"].lower()
    if args.get("status"):
        body["status"] = args["status"].lower()
    for arg, field_name in (("guests_can_modify", "guestsCanModify"), ("guests_can_invite_others",
                            "guestsCanInviteOthers"), ("guests_can_see_other_guests", "guestsCanSeeOtherGuests")):
        value = _yesno(args.get(arg) or "", arg)
        if value is not None:
            body[field_name] = value
    private, shared = _props(args.get("properties") or "", "properties"), \
        _props(args.get("shared_properties") or "", "shared_properties")
    if private is not None or shared is not None:
        props = dict((current or {}).get("extendedProperties") or {})
        if private is not None:
            props["private"] = {**props.get("private", {}), **private}
        if shared is not None:
            props["shared"] = {**props.get("shared", {}), **shared}
        body["extendedProperties"] = props
    if args.get("source_url"):
        body["source"] = {"url": args["source_url"], "title": args.get("source_title") or args["source_url"]}
    meet = (args.get("meet") or "").lower()
    if meet == "add":
        body["conferenceData"] = {"createRequest": {"requestId": __import__("uuid").uuid4().hex,
                                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    elif meet == "remove":
        body["conferenceData"] = None
    if meet or (current or {}).get("conferenceData"):
        params["conferenceDataVersion"] = 1  # keeps an existing Meet link through the change
    return body, params


def _extras(args: dict) -> list[str]:
    """Plain words for the non-time fields of a call, for the approval card."""
    out = []
    if args.get("reminders"):
        text = describe_reminders(_reminders(args["reminders"]))
        out.append(text if text in ("no alerts", "the calendar's default alerts") else "alerts: " + text)
    if args.get("attendees"):
        items = _guests(args["attendees"])
        if items[0][0] == "=":
            names = ", ".join(e + (" (optional)" if o else "") for _, e, o in items)
            out.append(f"guests: {names}")
        else:
            adds = [e for op, e, _ in items if op == "+"]
            drops = [e for op, e, _ in items if op == "-"]
            if adds:
                out.append("adds " + ", ".join(adds))
            if drops:
                out.append("removes " + ", ".join(drops))
        out.append("Google emails them" if args.get("notify_guests") else "no emails sent")
    elif args.get("notify_guests"):
        out.append("Google emails the guests about the change")
    meet = (args.get("meet") or "").lower()
    if meet:
        out.append("adds a Google Meet link" if meet == "add" else "removes the Meet link")
    if args.get("color"):
        out.append(f"color {args['color'].lower()}")
    if args.get("show_as"):
        out.append(f"shows as {args['show_as'].lower()}")
    if args.get("visibility"):
        out.append(args["visibility"].lower())
    if args.get("status"):
        out.append(args["status"].lower())
    for arg, label in (("guests_can_modify", "guests can edit"), ("guests_can_invite_others", "guests can invite"),
                       ("guests_can_see_other_guests", "guests see each other")):
        value = _yesno(args.get(arg) or "", arg)
        if value is not None:
            out.append(label if value else label.replace("can", "can't").replace("see", "don't see"))
    if args.get("properties") or args.get("shared_properties"):
        out.append("tags: " + ", ".join(f"{k}={v}" for name in ("properties", "shared_properties")
                                        for k, v in (_props(args.get(name) or "", name) or {}).items()))
    if args.get("source_url"):
        out.append(f"link: {args.get('source_title') or args['source_url']}")
    return out


# --------------------------------------------------------------------------- #
# Recurrence (RFC 5545 lines, as Google takes them)
# --------------------------------------------------------------------------- #

_RULE_PREFIXES = ("RRULE:", "EXRULE:", "RDATE", "EXDATE")
_DAYS = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday",
         "SA": "Saturday", "SU": "Sunday"}
_ORDINAL = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", -1: "last", -2: "second to last"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]


def _rules(recurrence: str) -> list[str]:
    """``recurrence`` (lines, or one line) as Google's list; [] for "none"."""
    if recurrence.strip().lower() == "none":
        return []
    lines = [line.strip() for line in recurrence.replace("\\n", "\n").splitlines() if line.strip()]
    for line in lines:
        if not line.upper().startswith(_RULE_PREFIXES):
            raise CalendarError(f"not an RFC 5545 recurrence line: {line!r} (e.g. RRULE:FREQ=WEEKLY;BYDAY=SU;COUNT=10)")
    return lines


def _rrule(lines: list[str]) -> tuple[int, dict[str, str]] | None:
    """(index, {part: value}) of the RRULE line."""
    for i, line in enumerate(lines):
        if line.upper().startswith("RRULE:"):
            return i, dict(p.split("=", 1) for p in line[6:].split(";") if "=" in p)
    return None


def _join(parts: dict[str, str]) -> str:
    return "RRULE:" + ";".join(f"{k}={v}" for k, v in parts.items())


def _ordinal_day(token: str) -> str:
    num, day = token[:-2], token[-2:]
    name = _DAYS.get(day, day)
    return f"the {_ORDINAL.get(int(num), num + 'th')} {name}" if num and num not in ("+",) else name


def _until(value: str) -> str:
    try:
        d = datetime.strptime(value[:8], "%Y%m%d")
    except ValueError:
        return value
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def describe_recurrence(recurrence: list[str] | str) -> str:
    """Plain words for RFC 5545 lines: "weekly on Sundays, 10 times"."""
    lines = recurrence if isinstance(recurrence, list) else _rules(recurrence)
    if not lines:
        return "doesn't repeat"
    found = _rrule(lines)
    if found is None:
        return "; ".join(lines)
    _, r = found
    freq = r.get("FREQ", "").upper()
    interval = int(r.get("INTERVAL", "1") or 1)
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(freq, freq.lower())
    if interval == 1:
        text = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly", "YEARLY": "yearly"}.get(freq, freq.lower())
    elif interval == 2:
        text = f"every other {unit}"
    else:
        text = f"every {interval} {unit}s"
    days = [d.strip() for d in r.get("BYDAY", "").split(",") if d.strip()]
    if days:
        plain = [d for d in days if d in _DAYS]
        if sorted(plain) == sorted(["MO", "TU", "WE", "TH", "FR"]) and len(days) == 5:
            text += " on weekdays"
        elif freq == "WEEKLY" or all(d in _DAYS for d in days):
            names = [_DAYS[d] + "s" for d in days if d in _DAYS]
            text += " on " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0])
        else:
            text += " on " + " and ".join(_ordinal_day(d) for d in days)
    month = _MONTHS[int(r["BYMONTH"]) - 1] if r.get("BYMONTH", "").isdigit() else None
    if r.get("BYMONTHDAY"):
        n = r["BYMONTHDAY"].split(",")[0]
        if month and n.isdigit():
            text += f" on {month} {n}"
            month = None
        else:
            suffix = "th" if n in ("11", "12", "13") else {"1": "st", "2": "nd", "3": "rd"}.get(n[-1], "th")
            text += f" on the {'last day' if n == '-1' else n + suffix}"
    if month:
        text += f" in {month}"
    if r.get("COUNT"):
        text += f", {r['COUNT']} times"
    if r.get("UNTIL"):
        text += f", until {_until(r['UNTIL'])}"
    if any(line.upper().startswith("EXDATE") for line in lines):
        text += ", with some dates skipped"
    return text


def _human_time(value: str | None) -> str:
    if not value:
        return "?"
    if len(value) == 10:
        d = datetime.strptime(value, "%Y-%m-%d")
        return f"{d.strftime('%a %b')} {d.day}"
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{d.strftime('%a %b')} {d.day}, {d.strftime('%I:%M %p').lstrip('0')}"


def _clock(value: str) -> str:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d.strftime("%I:%M %p").lstrip("0")


async def _get(ctx, event_id: str) -> dict:
    cid = await _own(ctx, create=False)
    if not cid:
        raise CalendarError("the agent's calendar doesn't exist yet")
    return await _api(ctx, "GET", f"/calendars/{cid}/events/{event_id}")


async def _resolve(ctx, event_id: str, scope: str, original_start: str = "") -> tuple[dict, dict | None]:
    """(the series or single event, the occurrence acted on). The approval's summary and the action
    both use this, so the card can't promise one thing and the call do another.

    For a repeating event, scope "this" or "following" needs one occurrence: an occurrence id, or the
    series id with ``original_start`` (the date-time it was scheduled for). A series id alone is refused
    rather than read as the whole series."""
    e = await _get(ctx, event_id)
    if e.get("recurringEventId"):
        series, occurrence = await _get(ctx, e["recurringEventId"]), e
    else:
        series, occurrence = e, None
    if not series.get("recurrence") or scope == "series":
        return series, occurrence
    if occurrence is not None:
        if original_start and _when(occurrence.get("originalStartTime")) and not _same_time(
                original_start, _when(occurrence["originalStartTime"])):
            raise CalendarError(f"{event_id} is the occurrence of {_when(occurrence['originalStartTime'])}, "
                                f"not {original_start}")
        return series, occurrence
    if not original_start:
        raise CalendarError(f'{event_id} is a repeating event\'s series id: for scope "{scope}", pass an occurrence '
                            'id from events_list (or this id with original_start, the date-time it was scheduled for)')
    cid = await _own(ctx, create=False)
    found = await _api(ctx, "GET", f"/calendars/{cid}/events/{series['id']}/instances",
                       params={"originalStart": original_start, "showDeleted": "false"})
    items = [i for i in found.get("items", []) if _same_time(original_start, _when(i.get("originalStartTime")))]
    if not items:
        raise CalendarError(f"no occurrence of that series is scheduled for {original_start}")
    return series, items[0]


def _same_time(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if len(a) == 10 or len(b) == 10:
        return a[:10] == b[:10]
    return _parse(a) == _parse(b)


# --------------------------------------------------------------------------- #
# Approvals: which calls are approved every time, and what they say
# --------------------------------------------------------------------------- #

def check(tool: str, args: dict) -> None:
    """What can be checked from the arguments alone, before the human is asked."""
    scope = args.get("scope", "this")
    if tool in ("event_update", "event_delete") and scope not in ("this", "following", "series"):
        raise CalendarError('scope is "this", "following" or "series"')
    recurrence = args.get("recurrence") or ""
    rules = _rules(recurrence) if recurrence else []
    if tool == "event_create" and rules and len(args.get("start", "")) > 10 and not args.get("time_zone"):
        raise CalendarError("a repeating timed event needs a time_zone (e.g. America/New_York), so it keeps its "
                            "local time across daylight-saving changes")
    if tool == "event_update" and recurrence and scope == "this":
        raise CalendarError('a new recurrence applies to the series: use scope "series" or "following"')
    if args.get("original_start") and scope == "series":
        raise CalendarError('original_start picks one occurrence: use scope "this" or "following"')
    if tool == "event_quick_add" and not (args.get("text") or "").strip():
        raise CalendarError("quick add needs the text of the event")
    if tool == "event_create" and (args.get("meet") or "").lower() == "remove":
        raise CalendarError('meet is "add" when creating')
    _validate(args)


def each_time(tool: str, args: dict) -> bool:
    """Approved on every call, whatever was approved before: changing a whole series or this and
    following (a session or always approval of event_update covers single events and single
    occurrences only), and anything Google emails people about."""
    if args.get("notify_guests"):
        return True
    return tool == "event_update" and args.get("scope", "this") in ("series", "following")


async def describe(ctx, tool: str, args: dict) -> str | None:
    """A plain-language line for the approval card, next to the raw arguments."""
    title = args.get("summary")
    extras = _extras(args)
    tail = ("; " + "; ".join(extras)) if extras else ""
    if tool == "event_create":
        when = f"{_human_time(args.get('start'))}"
        if args.get("recurrence"):
            return f"New repeating event '{title}': {describe_recurrence(args['recurrence'])}, starting {when}{tail}"
        return f"New event '{title}' on {when}{tail}"
    if tool == "event_quick_add":
        return f"New event from the text: \"{args.get('text')}\" (Google reads the date and time from it){tail}"
    if tool == "calendar_update":
        changes = [f"{label} {args[k]}" for k, label in (("name", "renamed to"), ("time_zone", "time zone"),
                                                          ("description", "description:")) if args.get(k)]
        return "The agent's calendar: " + (", ".join(changes) or "no change")
    if tool not in ("event_update", "event_delete"):
        return None
    scope = args.get("scope", "this")
    series, occurrence = await _resolve(ctx, args["event_id"], scope, args.get("original_start", ""))
    name = (occurrence or series).get("summary", "an event")
    rule = series.get("recurrence") or []
    guests = (occurrence or series).get("attendees") or []
    if tool == "event_delete":
        told = f"; Google emails its {len(guests)} guests" if guests and args.get("notify_guests") else \
            (f"; its {len(guests)} guests aren't told" if guests else "")
        if not rule:
            return f"Delete '{name}' ({_human_time(_when(series.get('start')))}){told}"
        if scope == "series":
            return f"Delete the whole series '{name}' ({describe_recurrence(rule)}){told}"
        on = _human_time(_when((occurrence or series).get("originalStartTime") or (occurrence or series).get("start")))
        if scope == "following":
            return f"End the series '{name}' before {on} (cancel that one and every one after it){told}"
        return f"Cancel '{name}' on {on} only (the rest of the series stays){told}"
    changes = []
    if title and title != name:
        changes.append(f"renamed to '{title}'")
    if args.get("start") and args.get("end") and args["start"][:10] == args["end"][:10] and len(args["start"]) > 10:
        span = f"{_clock(args['start'])}–{_clock(args['end'])}"
        changes.append(f"now {span}" if scope != "this" or not rule else f"moved to {_human_time(args['start'])[:10]} {span}")
    else:
        for field, label in (("start", "starts"), ("end", "ends")):
            if args.get(field):
                changes.append(f"{label} {_human_time(args[field])}")
    if args.get("location"):
        changes.append(f"at {args['location']}")
    if args.get("description"):
        changes.append("new details")
    if args.get("recurrence"):
        changes.append(f"{describe_recurrence(rule)} → {describe_recurrence(args['recurrence'])}")
    changes += extras
    what = ", ".join(changes) or "no visible change"
    if not rule or scope == "series":
        return f"'{name}'{' (whole series)' if rule else ''}: {what}"
    on = _human_time(_when((occurrence or series).get("originalStartTime") or (occurrence or series).get("start")))
    if scope == "following":
        return f"From {on} on, '{name}': {what}"
    return f"Just {on}, '{name}': {what}"


async def events_list(ctx, time_min: str, time_max: str, query: str = "", property: str = "",
                      show_cancelled: bool = False, max_results: int = 250) -> list:
    """Events on the agent's own calendar between two times (RFC 3339, e.g. 2026-10-27T00:00:00-04:00). A repeating event is listed once per occurrence: each has its own id (an occurrence id), plus series_id and original_start; pass that id with scope="this" to change one date, or scope="series" for all of them.

    time_min: start of the range
    time_max: end of the range
    query: free-text search (title, description, location, guests)
    property: only events tagged with this private property, as key=value
    show_cancelled: include cancelled events and occurrences
    max_results: at most this many (up to 2500)
    """
    cid = await _own(ctx, create=False)
    if not cid:
        return []
    params: dict[str, Any] = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                              "orderBy": "startTime", "showDeleted": "true" if show_cancelled else "false"}
    if query:
        params["q"] = query
    if property:
        params["privateExtendedProperty"] = property
    out: list = []
    limit = max(1, min(int(max_results), 2500))
    while len(out) < limit:
        params["maxResults"] = min(250, limit - len(out))
        try:
            data = await _api(ctx, "GET", f"/calendars/{cid}/events", params=params)
        except CalendarError as e:
            if " 404:" in str(e):  # the human deleted the agent's calendar
                ctx.state.pop("calendar_id", None)
                ctx.save()
                return []
            raise
        out += [_event(e) for e in data.get("items", [])]
        if not data.get("nextPageToken"):
            break
        params["pageToken"] = data["nextPageToken"]
    return out[:limit]


async def event_instances(ctx, series_id: str, time_min: str = "", time_max: str = "") -> list:
    """The occurrences of one repeating event (its series id, or any of its occurrence ids), optionally within a range.

    series_id: the series id (an occurrence id works too)
    time_min: start of the range (RFC 3339), optional
    time_max: end of the range (RFC 3339), optional
    """
    series, _ = await _resolve(ctx, series_id, "series")
    cid = await _own(ctx, create=False)
    params = {k: v for k, v in (("timeMin", time_min), ("timeMax", time_max)) if v}
    data = await _api(ctx, "GET", f"/calendars/{cid}/events/{series['id']}/instances", params={**params,
                                                                                               "maxResults": 2500})
    return [_event(e) for e in data.get("items", [])]


async def calendar_info(ctx) -> dict:
    """The agent's own calendar: id, name, description, time zone (exists: false until first used)."""
    cid = await _own(ctx, create=False)
    if not cid:
        return {"exists": False, "name": ctx.options.get("calendar_name", "Agent")}
    c = await _api(ctx, "GET", f"/calendars/{cid}")
    return {"exists": True, "id": c["id"], "name": c.get("summary"), "description": c.get("description"),
            "time_zone": c.get("timeZone")}


async def event_get(ctx, event_id: str) -> dict:
    """One event on the agent's own calendar: an occurrence id (from events_list) gives that date, with its series_id; a series id gives the series, with its recurrence.

    event_id: the event's id
    """
    return _event(await _get(ctx, event_id))


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


_FIELD_DOCS = """    reminders: alerts, e.g. popup:10m,email:1d (up to 5, at most 4 weeks before), default, or none
    attendees: guest emails, comma-separated; ?email for an optional guest; on update, +email / -email to add or remove
    notify_guests: have Google email the guests (invitation or update); otherwise nobody is emailed
    meet: add (a Google Meet link) or remove
    color: lavender, sage, grape, flamingo, banana, tangerine, peacock, graphite, blueberry, basil or tomato
    show_as: busy or free
    visibility: default, public, private or confidential
    status: confirmed or tentative
    guests_can_modify: yes or no
    guests_can_invite_others: yes or no
    guests_can_see_other_guests: yes or no
    properties: private key/value tags as a JSON object of strings (searchable with events_list property)
    shared_properties: key/value tags guests' copies can see, as a JSON object of strings
    source_url: a link back to where the event came from
    source_title: that link's title"""


async def event_create(ctx, summary: str, start: str, end: str, description: str = "", location: str = "",
                       time_zone: str = "", recurrence: str = "", reminders: str = "", attendees: str = "",
                       notify_guests: bool = False, meet: str = "", color: str = "", show_as: str = "",
                       visibility: str = "", status: str = "", guests_can_modify: str = "",
                       guests_can_invite_others: str = "", guests_can_see_other_guests: str = "",
                       properties: str = "", shared_properties: str = "", source_url: str = "",
                       source_title: str = "") -> dict:
    """Create an event on the agent's own calendar (created on first use; the human sees it overlaid). To repeat it, give recurrence as RFC 5545 lines, and a time_zone for timed events.

    summary: the title
    start: RFC 3339 date-time, or YYYY-MM-DD for all day (for a repeating event: the first occurrence)
    end: RFC 3339 date-time, or YYYY-MM-DD (exclusive) for all day
    description: details
    location: where
    time_zone: IANA zone (e.g. America/New_York); required when a timed event repeats
    recurrence: e.g. RRULE:FREQ=WEEKLY;BYDAY=SU;COUNT=10 or RRULE:FREQ=MONTHLY;BYDAY=2TU;UNTIL=20261231 (several lines: one per line)
    """
    args = {k: v for k, v in locals().items() if k != "ctx"}
    check("event_create", args)
    cid = await _own(ctx, create=True)
    body, params = _fields(args)
    if recurrence:
        body["recurrence"] = _rules(recurrence)
    return _event(await _api(ctx, "POST", f"/calendars/{cid}/events", params=params, body=body))


async def event_quick_add(ctx, text: str, notify_guests: bool = False) -> dict:
    """Create an event on the agent's own calendar from a sentence, the way Google Calendar's quick add reads it (e.g. "Dinner with Sam tomorrow 7pm at Lupa").

    text: the event in plain words
    notify_guests: have Google email anyone the text names as a guest
    """
    cid = await _own(ctx, create=True)
    return _event(await _api(ctx, "POST", f"/calendars/{cid}/events/quickAdd",
                             params={"text": text, "sendUpdates": "all" if notify_guests else "none"}))


async def calendar_update(ctx, name: str = "", description: str = "", time_zone: str = "") -> dict:
    """Rename the agent's own calendar, or change its description or time zone (the one new events default to).

    name: a new name
    description: a new description
    time_zone: IANA zone (e.g. America/New_York)
    """
    cid = await _own(ctx, create=True)
    body = {k: v for k, v in (("summary", name), ("description", description), ("timeZone", time_zone)) if v}
    c = await _api(ctx, "PATCH", f"/calendars/{cid}", body=body)
    return {"id": c["id"], "name": c.get("summary"), "description": c.get("description"),
            "time_zone": c.get("timeZone")}


def _ended_before(rule_parts: dict[str, str], occurrence_start: dict) -> str:
    """The series' RRULE ending just before an occurrence (UNTIL; COUNT dropped)."""
    parts = {k: v for k, v in rule_parts.items() if k not in ("COUNT", "UNTIL")}
    if occurrence_start.get("date"):
        day = datetime.strptime(occurrence_start["date"], "%Y-%m-%d") - timedelta(days=1)
        parts["UNTIL"] = day.strftime("%Y%m%d")
    else:
        moment = _parse(occurrence_start["dateTime"]).astimezone(timezone.utc) - timedelta(seconds=1)
        parts["UNTIL"] = moment.strftime("%Y%m%dT%H%M%SZ")
    return _join(parts)


_CARRIED = ("summary", "description", "location", "reminders", "attendees", "colorId", "transparency", "visibility",
            "status", "guestsCanModify", "guestsCanInviteOthers", "guestsCanSeeOtherGuests", "extendedProperties",
            "source", "conferenceData")


async def _split(ctx, series: dict, occurrence: dict, body: dict, params: dict, recurrence: str) -> dict:
    """This and following: end the series before the occurrence, and start a new series there with the
    changes (Google has no single call for it). The original is restored if the second step fails."""
    cid = await _own(ctx, create=False)
    lines = list(series.get("recurrence") or [])
    found = _rrule(lines)
    if found is None:
        raise CalendarError("that event doesn't repeat: use scope=\"this\"")
    idx, parts = found
    new_lines = _rules(recurrence) if recurrence else list(lines)
    if not recurrence and parts.get("COUNT"):  # the new series gets what's left of the count
        start = _when(occurrence["originalStartTime"])
        before = await _api(ctx, "GET", f"/calendars/{cid}/events/{series['id']}/instances", params={
            "timeMax": start if len(start) > 10 else start + "T00:00:00Z", "maxResults": 2500})
        left = int(parts["COUNT"]) - len(before.get("items", []))
        if left <= 0:
            raise CalendarError("no occurrences left from that one on")
        new_parts = dict(parts)
        new_parts["COUNT"] = str(left)
        new_lines[idx] = _join(new_parts)
    if not new_lines:
        raise CalendarError("for this and following, give the new recurrence (or use scope=\"this\")")
    ended = list(lines)
    ended[idx] = _ended_before(parts, occurrence["originalStartTime"])
    await _api(ctx, "PATCH", f"/calendars/{cid}/events/{series['id']}", body={"recurrence": ended},
               params={"sendUpdates": params.get("sendUpdates", "none")})
    fresh = {k: series[k] for k in _CARRIED if series.get(k) is not None}
    fresh["start"], fresh["end"] = dict(occurrence["start"]), dict(occurrence["end"])
    for side in ("start", "end"):  # keep the series' zone, which a repeating event needs
        if series.get(side, {}).get("timeZone") and "dateTime" in fresh[side]:
            fresh[side]["timeZone"] = series[side]["timeZone"]
    fresh.update(body)
    if fresh.get("conferenceData") is None:
        fresh.pop("conferenceData", None)
    fresh["recurrence"] = new_lines
    try:
        made = await _api(ctx, "POST", f"/calendars/{cid}/events", body=fresh,
                          params={**params, **({"conferenceDataVersion": 1} if fresh.get("conferenceData") else {})})
    except CalendarError:
        await _api(ctx, "PATCH", f"/calendars/{cid}/events/{series['id']}", body={"recurrence": lines},
                   params={"sendUpdates": "none"})
        raise
    return {"ended_series": _event({**series, "recurrence": ended}), "new_series": _event(made)}


async def event_update(ctx, event_id: str, scope: str = "this", summary: str = "", start: str = "", end: str = "",
                       description: str = "", location: str = "", time_zone: str = "", recurrence: str = "",
                       original_start: str = "", reminders: str = "", attendees: str = "",
                       notify_guests: bool = False, meet: str = "", color: str = "", show_as: str = "",
                       visibility: str = "", status: str = "", guests_can_modify: str = "",
                       guests_can_invite_others: str = "", guests_can_see_other_guests: str = "",
                       properties: str = "", shared_properties: str = "", source_url: str = "",
                       source_title: str = "") -> dict:
    """Change an event on the agent's own calendar (only the fields given). For a repeating event, scope says what changes: "this" (one occurrence: pass an occurrence id from events_list; a series id is refused unless original_start names the date), "following" (that occurrence and every later one) or "series" (all of them; an occurrence id is resolved to its series). recurrence replaces the pattern (scope "series" or "following"); "none" makes the series a single event.

    event_id: an occurrence id (from events_list) or a series id
    scope: this | following | series
    summary: a new title
    start: a new start (RFC 3339, or YYYY-MM-DD); for a series, the first occurrence's
    end: a new end (RFC 3339, or YYYY-MM-DD)
    description: new details
    location: a new place
    time_zone: IANA zone for the times
    recurrence: new RFC 5545 lines (e.g. RRULE:FREQ=WEEKLY;BYDAY=SA;UNTIL=20261231), or none
    original_start: with a series id and scope this or following, the date-time the occurrence was scheduled for
    """
    args = {k: v for k, v in locals().items() if k != "ctx"}
    check("event_update", args)
    cid = await _own(ctx, create=False)
    if not cid:
        raise CalendarError("the agent's calendar doesn't exist yet")
    series, occurrence = await _resolve(ctx, event_id, scope, original_start)
    repeating = bool(series.get("recurrence"))
    target = series if scope == "series" or occurrence is None else occurrence
    body, params = _fields(args, current=series if scope == "following" else target)
    if scope == "following" and repeating:
        return await _split(ctx, series, occurrence, body, params, recurrence)
    if target is series and recurrence:
        body["recurrence"] = _rules(recurrence)
        timed = "dateTime" in (body.get("start") or series.get("start", {}))
        if body["recurrence"] and timed and not (time_zone or series.get("start", {}).get("timeZone")):
            raise CalendarError("a repeating timed event needs a time_zone")
    return _event(await _api(ctx, "PATCH", f"/calendars/{cid}/events/{target['id']}", params=params, body=body))


async def event_delete(ctx, event_id: str, scope: str = "this", original_start: str = "",
                       notify_guests: bool = False) -> dict:
    """Delete from the agent's own calendar. For a repeating event: "this" cancels one occurrence (pass an occurrence id from events_list; a series id is refused unless original_start names the date; the series stays), "following" ends the series before it, "series" deletes every occurrence.

    event_id: an occurrence id (from events_list) or a series id
    scope: this | following | series
    original_start: with a series id and scope this or following, the date-time the occurrence was scheduled for
    notify_guests: have Google email the guests that it's cancelled
    """
    check("event_delete", {"scope": scope, "original_start": original_start})
    cid = await _own(ctx, create=False)
    if not cid:
        raise CalendarError("the agent's calendar doesn't exist yet")
    series, occurrence = await _resolve(ctx, event_id, scope, original_start)
    send = {"sendUpdates": "all" if notify_guests else "none"}
    if scope == "following" and series.get("recurrence"):
        lines = list(series["recurrence"])
        idx, parts = _rrule(lines)
        lines[idx] = _ended_before(parts, occurrence["originalStartTime"])
        await _api(ctx, "PATCH", f"/calendars/{cid}/events/{series['id']}", body={"recurrence": lines}, params=send)
        return {"ended_series": series["id"], "repeats": describe_recurrence(lines)}
    target = series["id"] if scope == "series" or occurrence is None else occurrence["id"]
    await _api(ctx, "DELETE", f"/calendars/{cid}/events/{target}", params=send)
    return {"deleted": target, "scope": scope if series.get("recurrence") else "event"}


event_create.__doc__ = event_create.__doc__.rstrip() + "\n" + _FIELD_DOCS + "\n    "
event_update.__doc__ = event_update.__doc__.rstrip() + "\n" + _FIELD_DOCS + "\n    "
