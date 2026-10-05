"""Human approvals for egress: a webhook announces, an approver app decides.

    approvals = Approvals(webhook="https://approver.example/hook", secret="...")
    KrunExecutor(egress=Egress(approvals=approvals))

When a sandbox connects somewhere not allowed, or uses a secret outside its
rules, or the agent asks ahead (the ``request_access`` skill), an approval
is created and POSTed to the webhook (JSON, signed: ``X-Agentd-Signature:
sha256=<HMAC of the body>``). The proxy holds the request for ``hold``
seconds: approved in time, it goes through; otherwise the agent gets a 403
saying the approval is pending (``id``, ``retry_after``) and retries later.

Secrets locked in fnox (a vault whose master password fnox's daemon doesn't
have yet) are listed in the approval's ``details["unlock"]``; allowing it then
needs the master password (``{"decision": ..., "password": ...}``), which
unlocks them (:func:`agentd.fnox.fill`) before the decision applies. A wrong
password leaves the approval pending. A pre-approved use that finds its secret
locked asks with an approval of kind ``unlock``.

Decisions (``agentd serve``: ``POST /v1/approvals/{id}``, or :meth:`decide`):
``once`` (the held request, or if none is held, the next matching one),
``session`` (until the sandbox stops), ``always``
(persisted in ``~/.agentd/egress/allow.toml``: host allowances, secret rules
and browser logins; not in the fnox config, whose contents are part of fnox's
cache key, so editing it would re-lock every unlocked secret), or ``deny``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agentd import fnox
from agentd.egress.policy import Allow, Policy, SecretRule
from agentd.sandbox.base import DEFAULT_HOME

if TYPE_CHECKING:
    from agentd.egress import EgressSession

logger = logging.getLogger(__name__)

ALLOW_FILE = DEFAULT_HOME / "egress" / "allow.toml"
DECISIONS = ("once", "session", "always", "deny")


@dataclass
class Approval:
    id: str
    kind: str  # connect | secret | unlock | device | browser | browser_login | browser_site | browser_host | browser_login_retry
    session: str
    details: dict[str, Any]
    reason: str = ""
    created: float = field(default_factory=time.time)
    status: str = "pending"           # pending | once | session | always | deny | expired
    decided_by: str | None = None
    decided: float | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


class Approvals:
    def __init__(self, webhook: str | None = None, *, secret: str | None = None, hold: float = 25.0,
                 expire: float = 3600.0, answer_url: str | None = None, allow_file: Path = ALLOW_FILE):
        self.webhook = webhook
        self.secret = (secret or os.environ.get("AGENTD_WEBHOOK_SECRET") or "").encode()
        self.hold = hold
        self.expire = expire
        self.answer_url = answer_url
        self.allow_file = Path(allow_file)
        self.items: dict[str, Approval] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self._holding: dict[str, int] = {}  # approval id -> requests held for it right now
        self._sessions: dict[str, tuple["EgressSession", Policy]] = {}
        self._dedupe: dict[tuple, str] = {}
        self._fillers: dict[str, Callable[[list[str], bytearray], dict[str, str]]] = {}  # approval id -> unlock

    # ------------------------------------------------------------------ #
    # Persistent grants ("always")
    # ------------------------------------------------------------------ #

    def _saved(self) -> dict[str, list]:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]
        try:
            data = tomllib.loads(self.allow_file.read_text())
        except (OSError, ValueError):
            return {}
        return {k: list(v) for k, v in data.items() if isinstance(v, list)}

    def saved_allows(self) -> list[str]:
        return self._saved().get("allow", [])

    def saved_logins(self) -> list[str]:
        """Browser logins approved "always", as ``site@host``."""
        return self._saved().get("browser_logins", [])

    def saved_sites(self) -> list[str]:
        """Sites whose signed-in session the browser may use without asking ("always")."""
        return self._saved().get("browser_sites", [])

    def saved_rules(self) -> list[SecretRule]:
        """Secret rules approved "always" (``[[secret_rules]]``: secret, domain, header, methods, paths)."""
        rules = []
        for r in self._saved().get("secret_rules", []):
            if isinstance(r, dict) and r.get("secret") and r.get("domain"):
                rules.append(SecretRule(secret=r["secret"], domain=str(r["domain"]).lower(),
                                        header=str(r.get("header", "authorization")).lower(),
                                        methods=tuple(r.get("methods") or ()), paths=tuple(r.get("paths") or ())))
        return rules

    def _save(self, key: str, value: str | dict) -> None:
        saved = self._saved()
        if value in saved.get(key, []):
            return
        saved.setdefault(key, []).append(value)
        lists = [f"{k} = [" + ", ".join(json.dumps(s) for s in v) + "]\n"
                 for k, v in saved.items() if all(isinstance(s, str) for s in v)]
        tables = [f"\n[[{k}]]\n" + "".join(f"{f} = {json.dumps(x)}\n" for f, x in t.items())
                  for k, v in saved.items() if not all(isinstance(s, str) for s in v) for t in v]
        self.allow_file.parent.mkdir(parents=True, exist_ok=True)
        self.allow_file.write_text("".join(lists + tables))

    def _save_allow(self, spec: str) -> None:
        self._save("allow", spec)

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    def asker(self, egress: "EgressSession", policy: Policy):
        """The proxy's hook for one session: ``await ask(kind, **details) -> (approved, approval id, status)``."""
        self._sessions[egress.session] = (egress, policy)
        policy.allows.extend(Allow.parse(s) for s in self.saved_allows())
        policy.rules.extend(r for r in self.saved_rules() if r.secret in egress.placeholders)

        async def ask(kind: str, **details: Any) -> tuple[bool, str]:
            names = details.get("secrets") if kind == "unlock" else \
                [details["secret"]] if kind == "secret" and details.get("secret") else []
            unlock = None
            if names:
                missing = await asyncio.to_thread(egress.uncached, [n for n in names if not egress.loaded(n)])
                if kind == "unlock" and not missing:
                    return True, "", "unlocked"  # unlocked meanwhile: nothing to ask
                unlock = (missing, egress.fill) if missing else None
            approval = self.request(kind, egress.session, details, unlock=unlock)
            if approval.status != "pending":
                return approval.status != "deny", approval.id, approval.status
            fut = self._waiters.setdefault(approval.id, asyncio.get_running_loop().create_future())
            self._holding[approval.id] = self._holding.get(approval.id, 0) + 1
            try:
                status = await asyncio.wait_for(asyncio.shield(fut), self.hold)
            except asyncio.TimeoutError:
                return False, approval.id, "pending"
            finally:
                self._holding[approval.id] -= 1
                if not self._holding[approval.id]:
                    del self._holding[approval.id]
            return status != "deny", approval.id, status

        return ask

    def forget(self, session: str) -> None:
        self._sessions.pop(session, None)

    # ------------------------------------------------------------------ #
    # Requests and decisions
    # ------------------------------------------------------------------ #

    def request(self, kind: str, session: str, details: dict[str, Any], reason: str = "",
                unlock: tuple[list[str], Callable[[list[str], bytearray], dict[str, str]]] | None = None) -> Approval:
        """A pending approval (an identical pending one is reused) announced to the webhook.

        ``unlock``: (secret names locked in fnox, a function that unlocks them with
        the master password); allowing the approval will need that password."""
        self._expire_old()
        if unlock and unlock[0]:
            details = {**details, "unlock": list(unlock[0])}
        key = (kind, session, json.dumps(details, sort_keys=True))
        existing = self._dedupe.get(key)
        if existing and existing in self.items and self.items[existing].status == "pending":
            return self.items[existing]
        approval = Approval(id=f"apr_{os.urandom(8).hex()}", kind=kind, session=session, details=details,
                            reason=reason)
        self.items[approval.id] = approval
        self._dedupe[key] = approval.id
        if unlock and unlock[0]:
            self._fillers[approval.id] = unlock[1]
        self._notify(approval)
        return approval

    def unlock(self, approval_id: str, password: bytearray | str) -> None:
        """Unlock the approval's locked secrets with the master password (zeroed afterwards).

        The approval stays pending if it fails: :class:`agentd.fnox.WrongPassword` (a
        ValueError: ask again) or :class:`agentd.fnox.UnlockFailed` (unlocked, but fnox
        couldn't read a secret: fix the vault or the fnox config, or deny). Blocking:
        fnox derives the vault key per secret; agentd serve runs it in a thread."""
        pw = password if isinstance(password, bytearray) else bytearray(str(password).encode())
        try:
            approval = self.items.get(approval_id)
            if approval is None:
                raise KeyError(approval_id)
            names = approval.details.get("unlock") or []
            if not names:
                return
            failed = self._fillers[approval_id](names, pw)
            if any(why == fnox.WRONG_PASSWORD for why in failed.values()):
                raise fnox.WrongPassword("master password didn't unlock the vault")
            if failed:
                raise fnox.UnlockFailed("the vault unlocked, but fnox couldn't read "
                                        + "; ".join(f"{name}: {why}" for name, why in failed.items())
                                        + " (renamed or deleted in the vault? fix it there or in the fnox config, "
                                        "or deny)")
            approval.details["unlock"] = []
            self._fillers.pop(approval_id, None)
        finally:
            for i in range(len(pw)):
                pw[i] = 0

    def decide(self, approval_id: str, decision: str, by: str = "local",
               password: bytearray | str | None = None) -> Approval:
        """Decide an approval. One that lists secrets to unlock needs ``password`` to be allowed."""
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        approval = self.items.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval.status != "pending":
            return approval
        if decision != "deny" and approval.details.get("unlock"):
            if password is None:
                raise ValueError(f"approving this unlocks {', '.join(approval.details['unlock'])} in fnox: "
                                 "it needs the master password")
            self.unlock(approval_id, password)
        elif isinstance(password, bytearray):
            password[:] = bytes(len(password))
        approval.status, approval.decided_by, approval.decided = decision, by, time.time()
        if decision in ("session", "always"):
            self._grant(approval, persist=decision == "always")
        elif decision == "once" and not self._holding.get(approval_id):
            # Nothing is held for it (asked ahead with request_access, or the hold ran
            # out): the next matching request may go through, once.
            self._grant(approval, persist=False, once=True)
        fut = self._waiters.pop(approval_id, None)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        return approval

    def _grant(self, approval: Approval, *, persist: bool, once: bool = False) -> None:
        d = approval.details
        targets = [self._sessions[approval.session]] if approval.session in self._sessions else \
            list(self._sessions.values())
        if approval.kind == "browser_login":
            if persist:
                self._save("browser_logins", f"{d['site']}@{d['host']}")
            return
        if approval.kind == "browser_site":
            if persist:
                self._save("browser_sites", d["site"])
            return
        if approval.kind == "connect":
            spec = f"{d.get('host') or d.get('ip')}:{d['port']}"
            for _, policy in targets:
                (policy.once_allows if once else policy.allows).append(Allow.parse(spec))
            if persist:
                self._save_allow(spec)
        elif approval.kind == "secret":
            rule = SecretRule(secret=d["secret"], domain=d["host"], header=d.get("header", "authorization"),
                              methods=(d["method"],) if d.get("method") else (),
                              paths=(d["path"].split("?", 1)[0],) if d.get("path") else ())
            for egress, policy in targets:
                if rule.secret in egress.placeholders:
                    (policy.once_rules if once else policy.rules).append(rule)
            if persist:
                self._save("secret_rules", {"secret": rule.secret, "domain": rule.domain, "header": rule.header,
                                            "methods": list(rule.methods), "paths": list(rule.paths)})

    def _expire_old(self) -> None:
        now = time.time()
        for a in self.items.values():
            if a.status == "pending" and now - a.created > self.expire:
                a.status = "expired"

    async def wait(self, approval_id: str, timeout: float | None = None) -> str:
        """An approval's status once decided, or after ``timeout`` (default: the hold) if it's still pending.

        Polls rather than awaiting a future: request tools run on the bridge's
        loop, decisions arrive on agentd serve's."""
        deadline = time.monotonic() + (self.hold if timeout is None else timeout)
        while True:
            a = self.items.get(approval_id)
            if a is None or a.status != "pending" or time.monotonic() >= deadline:
                return a.status if a else "unknown"
            await asyncio.sleep(0.2)

    def wait_note(self) -> str:
        return (f"It waits up to {_duration(self.hold)} for the human's answer; if it's still pending then, "
                "check again later (approvals arrive while you do other work).")

    def timeout_note(self) -> str:
        """For request tools' descriptions: how long a request waits for a decision."""
        return (f"A request nobody decides within {_duration(self.expire)} expires (status expired); "
                "ask again if it's still needed.")

    def hold_note(self) -> str:
        """For request_access's description: the hold, client timeouts, and what agentd's 403 means."""
        return (f"A connection or secret that needs approval is held up to {_duration(self.hold)} while the human "
                f"decides, so give network calls a timeout of at least {_duration(self.hold + 15)}. Not decided in "
                'time, the call gets HTTP 403 with JSON error.type "agentd_egress" and error.approval '
                '{id, status: "pending", retry_after}: it is waiting on a human, not denied; check '
                "access_status(id) and retry after retry_after seconds; status \"deny\" means the human refused. "
                "A 403 without error.approval means not "
                "allowed and nothing is pending: call request_access. Non-HTTP connections are just reset.")

    def list(self, *, pending_only: bool = False) -> list[Approval]:
        self._expire_old()
        items = sorted(self.items.values(), key=lambda a: a.created, reverse=True)
        return [a for a in items if a.status == "pending"] if pending_only else items

    # ------------------------------------------------------------------ #
    # Webhook
    # ------------------------------------------------------------------ #

    def sign(self, body: bytes) -> str:
        return "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def _notify(self, approval: Approval) -> None:
        if not self.webhook:
            return
        payload = {"type": "approval.requested", "approval": approval.public()}
        if self.answer_url:
            payload["answer"] = {"url": f"{self.answer_url.rstrip('/')}/v1/approvals/{approval.id}",
                                 "decisions": list(DECISIONS)}
        body = json.dumps(payload).encode()
        asyncio.get_running_loop().create_task(self._post(body))

    async def _post(self, body: bytes) -> None:
        import aiohttp

        headers = {"content-type": "application/json"}
        if self.secret:
            headers["x-agentd-signature"] = self.sign(body)
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
                    async with http.post(self.webhook, data=body, headers=headers) as r:
                        if r.status < 500:
                            return
            except Exception as e:  # noqa: BLE001
                logger.warning("approval webhook failed (%s): %s", self.webhook, e)
            await asyncio.sleep(2 ** attempt)


# --------------------------------------------------------------------------- #
# The request_access skill (host-side tool)
# --------------------------------------------------------------------------- #

_ACTIVE: Approvals | None = None


def _duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:g} seconds"
    if seconds < 7200:
        return f"{seconds / 60:g} minutes"
    return f"{seconds / 3600:g} hours"


def register_request_tool(func, approvals: Approvals | None, *notes: str) -> None:
    """Register a request_* skill, its description saying when an undecided request expires (and ``notes``)."""
    from agentd.tool_decorator import SCHEMA_REGISTRY, tool

    tool(func)
    if approvals is not None:
        fn = SCHEMA_REGISTRY[func.__name__]["function"]
        first, _, rest = fn["description"].partition("\n")
        fn["description"] = " ".join((first, approvals.timeout_note(), *notes)) + "\n" + rest


def enable_access_skill(approvals: Approvals) -> None:
    """Register ``list_secrets`` / ``request_access`` / ``access_status`` as skills for agents."""
    global _ACTIVE
    from agentd.tool_decorator import tool

    _ACTIVE = approvals
    tool(list_secrets)
    register_request_tool(request_access, approvals, approvals.wait_note(), approvals.hold_note())
    tool(access_status)


def _current_egress():
    sessions = list(_ACTIVE._sessions.values()) if _ACTIVE is not None else []
    return sessions[-1][0] if sessions else None


async def list_secrets() -> list:
    """List the secrets this sandbox can use or ask for: name, description and the rules that let it be sent now. Never values.

    Each one is in the environment as $NAME, holding a placeholder: put it where a rule says (e.g. the Authorization header for api.github.com) and agentd swaps in the real value on the host for that rule's host, methods and paths. Anywhere else, ask first with request_access(host, reason, secret=NAME, method=..., path=...).
    """
    if _ACTIVE is None:
        raise RuntimeError("approvals aren't enabled")
    egress = _current_egress()
    return egress.list_secrets() if egress is not None else []


async def request_access(host: str, reason: str, port: int = 443, secret: str = "", method: str = "",
                         path: str = "") -> dict:
    """Ask the human to let this sandbox reach a host, or use a secret there. Returns an approval id; check it with access_status.

    host: the host name (e.g. api.github.com)
    reason: why the task needs it, for the human approving
    port: the port (default 443)
    secret: a secret's name to use there (one list_secrets shows, e.g. GITHUB_TOKEN), if any
    method: the HTTP method the secret is needed for (e.g. POST)
    path: the URL path the secret is needed for (e.g. /repos/me/app/pulls)
    """
    if _ACTIVE is None:
        raise RuntimeError("approvals aren't enabled")
    sessions = list(_ACTIVE._sessions)
    session = sessions[-1] if sessions else ""
    egress = _current_egress()
    if secret and egress is not None and secret not in egress.placeholders:
        raise ValueError(f"no secret named {secret!r} here; list_secrets shows the ones you can ask for")
    if secret:
        details = {"secret": secret, "host": host.lower(), "method": method.upper(), "path": path,
                   "header": "authorization"}
        unlock = None
        if egress is not None and not egress.loaded(secret):
            missing = await asyncio.to_thread(egress.uncached, [secret])
            unlock = (missing, egress.fill) if missing else None
        approval = _ACTIVE.request("secret", session, details, reason, unlock=unlock)
    else:
        approval = _ACTIVE.request("connect", session, {"host": host.lower(), "ip": "", "port": port}, reason)
    return {"id": approval.id, "status": await _ACTIVE.wait(approval.id)}


async def access_status(approval_id: str) -> dict:
    """The status of an access request: pending, once, session, always, deny or expired.

    approval_id: the id request_access returned
    """
    if _ACTIVE is None:
        raise RuntimeError("approvals aren't enabled")
    a = _ACTIVE.items.get(approval_id)
    return {"id": approval_id, "status": a.status if a else "unknown"}
