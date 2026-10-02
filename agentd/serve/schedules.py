"""Scheduled turns for ``agentd serve`` (``POST /v1/schedules``).

A schedule sends ``input`` (and ``instructions``) at set times, each run a
background turn:

  * target: ``session_id`` (an existing session), or a new session per run
    (``workspace``, ``harness``, ``model``, ``image``);
  * timing: ``at`` (one time; RFC 3339 or a Unix timestamp) or ``every``: an
    interval (``"90s"``, ``"30m"``, ``"2h"``, ``"1d"``; at least a minute) or
    a 5-field cron expression (``"0 9 * * 1-5"``), in ``timezone`` (IANA name;
    default: the box's local time zone).

Runs of one schedule never overlap, and a run waits while its session is busy.
Schedules persist in ``schedules.json``. If agentd was down when runs were
due, the latest one runs once at startup and earlier ones are skipped (and
counted in ``missed``). A schedule belongs to its creator and runs as them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from agentd.serve.store import _atomic_write, new_id

if TYPE_CHECKING:
    from agentd.serve.app import Server

logger = logging.getLogger(__name__)

_INTERVAL = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
MIN_INTERVAL = 60
KEEP_RUNS = 20


def parse_interval(text: str) -> int | None:
    m = _INTERVAL.match(str(text))
    return int(m.group(1)) * _UNITS[m.group(2)] if m else None


# --------------------------------------------------------------------------- #
# Cron
# --------------------------------------------------------------------------- #

class Cron:
    """Classic 5-field cron: minute hour day-of-month month day-of-week.
    Fields take ``*``, numbers, ranges ``a-b``, steps ``*/n`` / ``a-b/n`` and
    lists. Day of week is 0-6 from Sunday (7 is Sunday too). When both day
    fields are restricted, either may match (as in cron)."""

    RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron expressions have 5 fields (minute hour day month weekday): {expr!r}")
        self.expr = expr
        fields = [self._field(p, lo, hi) for p, (lo, hi) in zip(parts, self.RANGES)]
        self.minutes, self.hours, self.days, self.months, dows = fields
        self.dows = {d % 7 for d in dows}
        self.days_any, self.dows_any = parts[2] == "*", parts[4] == "*"

    @staticmethod
    def _field(text: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for item in text.split(","):
            base, _, step = item.partition("/")
            step_n = int(step) if step else 1
            if step_n < 1:
                raise ValueError(f"bad cron step in {text!r}")
            if base == "*":
                a, b = lo, hi
            elif "-" in base:
                a, b = (int(x) for x in base.split("-", 1))
            else:
                a = b = int(base)
                if step:
                    b = hi
            if not (lo <= a <= b <= hi):
                raise ValueError(f"cron field {text!r} is out of range {lo}-{hi}")
            out.update(range(a, b + 1, step_n))
        return out

    def _day_ok(self, d: datetime) -> bool:
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.dows
        if self.days_any and self.dows_any:
            return True
        if self.days_any:
            return dow
        if self.dows_any:
            return dom
        return dom or dow

    def next_after(self, after: float, tz) -> float:
        """The first matching minute strictly after ``after`` (a timestamp), in ``tz``."""
        t = datetime.fromtimestamp(after, tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(100_000):
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t.replace(tzinfo=tz).timestamp()
        raise ValueError(f"cron expression {self.expr!r} never matches")


def _tz(name: str | None):
    if not name:
        return datetime.now().astimezone().tzinfo
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _parse_at(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("'at' needs a time zone offset (e.g. 2026-10-02T09:00:00-05:00)")
    return dt.timestamp()


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #

@dataclass
class Schedule:
    id: str
    owner: str
    input: Any
    instructions: str | None = None
    session_id: str | None = None
    target: dict[str, Any] = field(default_factory=dict)   # new session per run: workspace/harness/model/image
    at: float | None = None
    every: str | None = None
    timezone: str | None = None
    next_run: float | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)  # newest last: response_id, started_at, session_id
    missed: int = 0
    active: bool = True
    created_at: float = field(default_factory=time.time)

    def following(self, after: float) -> float | None:
        """The next run time strictly after ``after``."""
        if self.at is not None:
            return None
        interval = parse_interval(self.every or "")
        if interval is not None:
            base = self.next_run if self.next_run is not None else after
            n = max(1, int((after - base) // interval) + 1)
            return base + n * interval
        return Cron(self.every).next_after(after, _tz(self.timezone))


class Scheduler:
    def __init__(self, server: "Server"):
        self.server = server
        self.path = server.config.dir / "schedules.json"
        self.schedules: dict[str, Schedule] = {}
        self.running: dict[str, Any] = {}  # schedule id -> its current Turn
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        if self.path.is_file():
            for s in json.loads(self.path.read_text()).get("schedules", []):
                self.schedules[s["id"]] = Schedule(**s)

    def save(self) -> None:
        _atomic_write(self.path, json.dumps({"schedules": [asdict(s) for s in self.schedules.values()]}))

    # API ------------------------------------------------------------------

    def create(self, who: str, body: dict[str, Any]) -> Schedule:
        from agentd.serve.app import HTTPError

        if "input" not in body:
            raise HTTPError(400, "input is required")
        if ("at" in body) == ("every" in body):
            raise HTTPError(400, "give exactly one of 'at' or 'every'")
        s = Schedule(id=new_id("sch"), owner=who, input=body["input"], instructions=body.get("instructions"),
                     session_id=body.get("session_id"), timezone=body.get("timezone"))
        if s.session_id:
            session = self.server._session(s.session_id)
            self.server._drivable(who, session)
            if session.closed:
                raise HTTPError(409, f"session {session.id} is closed")
        else:
            s.target = {k: body[k] for k in ("workspace", "harness", "model", "image") if body.get(k) is not None}
            if s.target.get("workspace"):
                self.server.config.resolve_workspace(s.target["workspace"], s.id)  # validate now
        now = time.time()
        try:
            if "at" in body:
                s.at = _parse_at(body["at"])
                s.next_run = s.at
            else:
                s.every = str(body["every"])
                interval = parse_interval(s.every)
                if interval is not None:
                    if interval < MIN_INTERVAL:
                        raise HTTPError(400, f"intervals must be at least {MIN_INTERVAL}s")
                    s.next_run = now + interval
                else:
                    _tz(s.timezone)
                    s.next_run = Cron(s.every).next_after(now, _tz(s.timezone))
        except (ValueError, KeyError) as e:
            raise HTTPError(400, f"bad schedule timing: {e}") from None
        self.schedules[s.id] = s
        self.save()
        self._wake.set()
        return s

    def all(self) -> list[Schedule]:
        return sorted(self.schedules.values(), key=lambda s: s.created_at)

    def get(self, schedule_id: str) -> Schedule:
        from agentd.serve.app import HTTPError

        s = self.schedules.get(schedule_id)
        if s is None:
            raise HTTPError(404, f"no schedule {schedule_id}", "not_found_error")
        return s

    def delete(self, who: str, schedule_id: str) -> Schedule:
        from agentd.serve.app import LOCAL, HTTPError

        s = self.get(schedule_id)
        if who not in (LOCAL, s.owner) and who not in self.server.config.drivers:
            raise HTTPError(403, f"schedule {s.id} belongs to {s.owner}", "permission_error")
        del self.schedules[s.id]
        self.save()
        return s

    def drop_session(self, session_id: str) -> None:
        changed = False
        for s in self.schedules.values():
            if s.session_id == session_id and s.active:
                s.active, s.next_run, changed = False, None, True
        if changed:
            self.save()

    def view(self, s: Schedule) -> dict[str, Any]:
        turn = self.running.get(s.id)
        return {**asdict(s), "running_response_id": turn.response_id if turn else None}

    # Running --------------------------------------------------------------

    def catch_up(self, now: float) -> None:
        """At startup: runs missed while agentd was down collapse into one."""
        for s in self.schedules.values():
            if not s.active or s.next_run is None or s.next_run > now:
                continue
            if s.at is None:
                skipped = 0
                t = s.next_run
                while True:
                    nxt = s.following(t)
                    if nxt is None or nxt > now:
                        break
                    skipped += 1
                    t = nxt
                s.missed += skipped
                s.next_run = t  # the latest missed run: due now
        self.save()

    async def tick(self, now: float | None = None) -> None:
        from agentd.serve.app import HTTPError

        now = time.time() if now is None else now
        for s in list(self.schedules.values()):
            turn = self.running.get(s.id)
            if turn is not None:
                if not turn.done.is_set():
                    continue  # never overlap
                del self.running[s.id]
            if not s.active or s.next_run is None or s.next_run > now:
                continue
            if s.session_id and s.session_id in self.server.running:
                continue  # wait for the session's current turn
            body = {"input": s.input, "background": True}
            if s.instructions is not None:
                body["instructions"] = s.instructions
            if s.session_id:
                body["session_id"] = s.session_id
            else:
                body.update(s.target)
            due = s.next_run
            try:
                turn = self.server.start_turn(s.owner, body, background=True)
                await turn.started.wait()
            except HTTPError as e:
                if e.status == 409 and "already has a turn" in e.message:
                    continue
                logger.warning("agentd serve: schedule %s can't run: %s", s.id, e.message)
                s.runs.append({"started_at": now, "error": e.message})
            else:
                self.running[s.id] = turn
                s.runs.append({"started_at": now, "due": due, "response_id": turn.response_id,
                               "session_id": turn.session_id})
            s.runs = s.runs[-KEEP_RUNS:]
            s.next_run = s.following(max(now, due))
            if s.next_run is None:
                s.active = False
            self.save()

    def start(self) -> None:
        self.catch_up(time.time())

        async def loop():
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("agentd serve: scheduler tick failed")
                upcoming = [s.next_run for s in self.schedules.values() if s.active and s.next_run]
                delay = min([30.0] + [max(0.5, t - time.time()) for t in upcoming])
                if self.running or any(s.session_id in self.server.running for s in self.schedules.values()):
                    delay = min(delay, 1.0)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass

        self._task = asyncio.ensure_future(loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
