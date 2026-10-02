"""Human approvals for egress: a webhook announces, an approver app decides.

    approvals = Approvals(webhook="https://approver.example/hook", secret="...")
    KrunExecutor(egress=Egress(approvals=approvals))

When a sandbox connects somewhere not allowed, or uses a secret outside its
rules, or the agent asks ahead (the ``request_access`` skill), an approval
is created and POSTed to the webhook (JSON, signed: ``X-Agentd-Signature:
sha256=<HMAC of the body>``). The proxy holds the request for ``hold``
seconds: approved in time, it goes through; otherwise the agent gets a 403
saying the approval is pending (``id``, ``retry_after``) and retries later.

Decisions (``agentd serve``: ``POST /v1/approvals/{id}``, or :meth:`decide`):
``once`` (this request), ``session`` (until the sandbox stops), ``always``
(persisted: secret rules into the fnox config they came from, host
allowances into ``~/.agentd/egress/allow.toml``), or ``deny``.
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
from typing import TYPE_CHECKING, Any

from agentd.egress.policy import Allow, Policy, SecretRule, add_rule_to_fnox
from agentd.sandbox.base import DEFAULT_HOME

if TYPE_CHECKING:
    from agentd.egress import EgressSession

logger = logging.getLogger(__name__)

ALLOW_FILE = DEFAULT_HOME / "egress" / "allow.toml"
DECISIONS = ("once", "session", "always", "deny")


@dataclass
class Approval:
    id: str
    kind: str                         # "connect" | "secret"
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
        self._sessions: dict[str, tuple["EgressSession", Policy]] = {}
        self._dedupe: dict[tuple, str] = {}

    # ------------------------------------------------------------------ #
    # Persistent allowances (host grants that fnox can't hold)
    # ------------------------------------------------------------------ #

    def saved_allows(self) -> list[str]:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]
        try:
            return list(tomllib.loads(self.allow_file.read_text()).get("allow", []))
        except (OSError, ValueError):
            return []

    def _save_allow(self, spec: str) -> None:
        specs = self.saved_allows()
        if spec in specs:
            return
        specs.append(spec)
        self.allow_file.parent.mkdir(parents=True, exist_ok=True)
        self.allow_file.write_text("allow = [" + ", ".join(json.dumps(s) for s in specs) + "]\n")

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    def asker(self, egress: "EgressSession", policy: Policy):
        """The proxy's hook for one session: ``await ask(kind, **details) -> (approved, approval id, status)``."""
        self._sessions[egress.session] = (egress, policy)
        policy.allows.extend(Allow.parse(s) for s in self.saved_allows())

        async def ask(kind: str, **details: Any) -> tuple[bool, str]:
            approval = self.request(kind, egress.session, details)
            if approval.status != "pending":
                return approval.status != "deny", approval.id, approval.status
            fut = self._waiters.setdefault(approval.id, asyncio.get_running_loop().create_future())
            try:
                status = await asyncio.wait_for(asyncio.shield(fut), self.hold)
            except asyncio.TimeoutError:
                return False, approval.id, "pending"
            return status != "deny", approval.id, status

        return ask

    def forget(self, session: str) -> None:
        self._sessions.pop(session, None)

    # ------------------------------------------------------------------ #
    # Requests and decisions
    # ------------------------------------------------------------------ #

    def request(self, kind: str, session: str, details: dict[str, Any], reason: str = "") -> Approval:
        """A pending approval (an identical pending one is reused) announced to the webhook."""
        self._expire_old()
        key = (kind, session, json.dumps(details, sort_keys=True))
        existing = self._dedupe.get(key)
        if existing and existing in self.items and self.items[existing].status == "pending":
            return self.items[existing]
        approval = Approval(id=f"apr_{os.urandom(8).hex()}", kind=kind, session=session, details=details,
                            reason=reason)
        self.items[approval.id] = approval
        self._dedupe[key] = approval.id
        self._notify(approval)
        return approval

    def decide(self, approval_id: str, decision: str, by: str = "local") -> Approval:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        approval = self.items.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval.status != "pending":
            return approval
        approval.status, approval.decided_by, approval.decided = decision, by, time.time()
        if decision in ("session", "always"):
            self._grant(approval, persist=decision == "always")
        fut = self._waiters.pop(approval_id, None)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        return approval

    def _grant(self, approval: Approval, *, persist: bool) -> None:
        d = approval.details
        targets = [self._sessions[approval.session]] if approval.session in self._sessions else \
            list(self._sessions.values())
        if approval.kind == "connect":
            spec = f"{d.get('host') or d.get('ip')}:{d['port']}"
            for _, policy in targets:
                policy.allows.append(Allow.parse(spec))
            if persist:
                self._save_allow(spec)
        elif approval.kind == "secret":
            rule = SecretRule(secret=d["secret"], domain=d["host"], header=d.get("header", "authorization"),
                              methods=(d["method"],) if d.get("method") else (),
                              paths=(d["path"].split("?", 1)[0],) if d.get("path") else ())
            for egress, policy in targets:
                if rule.secret in egress.placeholders:
                    policy.rules.append(rule)
                    if persist and egress.config_files:
                        add_rule_to_fnox(egress.config_files[-1], rule)
                        persist = False  # once

    def _expire_old(self) -> None:
        now = time.time()
        for a in self.items.values():
            if a.status == "pending" and now - a.created > self.expire:
                a.status = "expired"

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
    """Register ``request_access`` / ``access_status`` as skills for agents."""
    global _ACTIVE
    from agentd.tool_decorator import tool

    _ACTIVE = approvals
    register_request_tool(request_access, approvals, approvals.hold_note())
    tool(access_status)


async def request_access(host: str, reason: str, port: int = 443, secret: str = "", method: str = "",
                         path: str = "") -> dict:
    """Ask the human to let this sandbox reach a host, or use a secret there. Returns an approval id; check it with access_status.

    host: the host name (e.g. api.github.com)
    reason: why the task needs it, for the human approving
    port: the port (default 443)
    secret: a secret's name to use there (e.g. GITHUB_TOKEN), if any
    method: the HTTP method the secret is needed for (e.g. POST)
    path: the URL path the secret is needed for (e.g. /repos/me/app/pulls)
    """
    if _ACTIVE is None:
        raise RuntimeError("approvals aren't enabled")
    sessions = list(_ACTIVE._sessions)
    session = sessions[-1] if sessions else ""
    if secret:
        details = {"secret": secret, "host": host.lower(), "method": method.upper(), "path": path,
                   "header": "authorization"}
        approval = _ACTIVE.request("secret", session, details, reason)
    else:
        approval = _ACTIVE.request("connect", session, {"host": host.lower(), "ip": "", "port": port}, reason)
    return {"id": approval.id, "status": approval.status}


async def access_status(approval_id: str) -> dict:
    """The status of an access request: pending, once, session, always, deny or expired.

    approval_id: the id request_access returned
    """
    if _ACTIVE is None:
        raise RuntimeError("approvals aren't enabled")
    a = _ACTIVE.items.get(approval_id)
    return {"id": approval_id, "status": a.status if a else "unknown"}
