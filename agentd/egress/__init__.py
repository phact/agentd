"""Network access for libkrun sandboxes, with secrets kept on the host.

    KrunExecutor(egress=Egress(allow=["pypi.org", "files.pythonhosted.org"]))

gives the sandbox a network card (agentd-net) whose every connection goes
through :class:`~agentd.egress.proxy.EgressProxy`: hosts with fnox secret
rules are intercepted to swap placeholders for real values, allowed hosts
pass through untouched, everything else is refused (or asked about, with
approvals). The sandbox only ever holds placeholders. See
docs/egress-and-secrets.md. Docker sandboxes have no network.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentd import fnox
from agentd.egress.ca import SessionCA
from agentd.egress.policy import (Allow, Grant, Policy, SecretRule, fnox_config_files, fnox_get, load_rules,
                                  load_secret_names, make_placeholder, opaque_placeholder)
from agentd.egress.proxy import EgressProxy
from agentd.fnox import SecretMissing
from agentd.sandbox.base import DEFAULT_HOME

logger = logging.getLogger(__name__)

CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"     # the image's bundle, with our CA appended
CA_FILE = "/etc/ssl/certs/agentd-egress.pem"        # our CA alone
# Where common runtimes look for extra or replacement CA bundles.
CA_ENV = {
    "SSL_CERT_FILE": CA_BUNDLE, "REQUESTS_CA_BUNDLE": CA_BUNDLE, "CURL_CA_BUNDLE": CA_BUNDLE,
    "PIP_CERT": CA_BUNDLE, "GIT_SSL_CAINFO": CA_BUNDLE, "NODE_EXTRA_CA_CERTS": CA_FILE,
}


@dataclass(frozen=True)
class Egress:
    """What a sandbox's network may reach.

    ``allow``: hosts reachable as-is ("pypi.org" means ports 443 and 80;
    "github.com:22"; "*.example.com"; "10.0.2.58:8001"). ``fnox``: use fnox's
    ``[proxy.rules]`` (from the workspace's fnox config) to inject secrets.
    ``secrets``: which of fnox's secrets the sandbox gets a placeholder for and
    may ask to use (None: every secret in the fnox config; secrets with rules
    always). ``approvals``: an :class:`agentd.egress.approvals.Approvals` to
    ask about anything else instead of refusing it."""

    allow: tuple[str, ...] = ()
    fnox: bool = True
    secrets: tuple[str, ...] | None = None
    fnox_profile: str | None = None
    fnox_bin: str = "fnox"
    audit: Path | None = DEFAULT_HOME / "egress" / "audit.jsonl"
    approvals: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "allow", tuple(self.allow))
        if self.secrets is not None:
            object.__setattr__(self, "secrets", tuple(self.secrets))
        for spec in self.allow:
            Allow.parse(spec)


class EgressSession:
    """One sandbox session's egress: its policy, secrets, CA and proxy."""

    def __init__(self, egress: Egress, workspace: Path, socket_path: Path, session: str = ""):
        self.egress = egress
        self.workspace = Path(workspace)
        self.socket_path = Path(socket_path)
        self.session = session
        self.ca = SessionCA()
        self.placeholders: dict[str, str] = {}
        self.descriptions: dict[str, str] = {}   # secret name -> fnox's description
        self.proxy: EgressProxy | None = None
        self.config_files: list[Path] = []

    async def start(self) -> None:
        e = self.egress
        rules: list[SecretRule] = []
        secrets: dict[str, str] = {}
        if e.fnox:
            self.config_files = await asyncio.to_thread(
                fnox_config_files, self.workspace, fnox=e.fnox_bin, profile=e.fnox_profile)
            rules = load_rules(self.config_files)
            locked: set[str] = set()
            for name in dict.fromkeys(r.secret for r in rules):
                # Read now only what fnox has unlocked (for format-preserving placeholders);
                # a locked secret is read, or unlocked, when a request first needs it.
                try:
                    secrets[name] = await asyncio.to_thread(
                        fnox_get, name, self.workspace, fnox=e.fnox_bin, profile=e.fnox_profile)
                except SecretMissing:
                    locked.add(name)
                except (RuntimeError, OSError) as err:
                    logger.warning("egress: %s (its rules are disabled)", err)
            rules = [r for r in rules if r.secret in secrets or r.secret in locked]
            # Every other secret the sandbox may ask for gets a placeholder now; its
            # value is read from fnox only once a rule or approval lets it be sent.
            names = load_secret_names(self.config_files, e.fnox_profile)
            for name in names if e.secrets is None else [n for n in e.secrets if n in names]:
                self.descriptions[name] = names[name]
        for r in rules:
            if r.secret not in self.placeholders:
                ph = r.placeholder or (make_placeholder(secrets[r.secret]) if r.secret in secrets
                                       else opaque_placeholder())
                self.placeholders[r.secret] = ph
            self.descriptions.setdefault(r.secret, "")
        for name in self.descriptions:
            self.placeholders.setdefault(name, opaque_placeholder())
        policy = Policy(rules=rules, allows=[Allow.parse(a) for a in e.allow])
        ask = None
        if e.approvals is not None:
            ask = e.approvals.asker(self, policy)
        self.proxy = EgressProxy(self.socket_path, policy, secrets=secrets, placeholders=self.placeholders,
                                 ca=self.ca, audit_path=e.audit, session=self.session, ask=ask,
                                 load=self._load if e.fnox else None,
                                 report=e.approvals.grant_used if e.approvals is not None else None)
        await self.proxy.start()

    async def _load(self, name: str) -> None:
        """Read a secret from fnox for the proxy, the first time it may be sent
        (:class:`SecretMissing` if it's locked in fnox)."""
        e = self.egress
        try:
            value = await asyncio.to_thread(fnox_get, name, self.workspace, fnox=e.fnox_bin, profile=e.fnox_profile)
        except (RuntimeError, OSError) as err:
            logger.warning("egress: %s", err)
            return
        if self.proxy is not None:
            self.proxy.add_secret(name, value)

    def loaded(self, name: str) -> bool:
        return self.proxy is not None and name in self.proxy.secrets

    def uncached(self, names: list[str]) -> list[str]:
        """Which of ``names`` fnox can't read without an unlock (blocking)."""
        e = self.egress
        return fnox.uncached(names, self.workspace, fnox=e.fnox_bin, profile=e.fnox_profile) if e.fnox else []

    def fill(self, names: list[str], password: bytearray) -> dict[str, str]:
        """Unlock ``names`` in fnox with the master password (blocking; zeroes it)."""
        e = self.egress
        return fnox.fill(names, password, cwd=self.workspace, fnox=e.fnox_bin, profile=e.fnox_profile)

    def list_secrets(self) -> list[dict[str, Any]]:
        """What the sandbox can use or ask for: names, descriptions, env vars and rules (never values)."""
        policy = self.proxy.policy if self.proxy is not None else Policy()
        rules = [(r, False) for r in policy.rules] + [(r, True) for r in policy.once_rules]
        return [{"name": name, "description": desc, "env": name,
                 "rules": [{"host": r.domain, "header": r.header, "methods": list(r.methods) or ["*"],
                            "paths": list(r.paths) or ["*"], **({"once": True} if once else {})}
                           for r, once in rules if r.secret == name]}
                for name, desc in self.descriptions.items()]

    async def stop(self) -> None:
        if self.proxy is not None:
            await self.proxy.stop()
            self.proxy = None
        if self.egress.approvals is not None:
            self.egress.approvals.forget(self.session)

    @property
    def policy(self) -> Policy:
        assert self.proxy is not None
        return self.proxy.policy

    def sandbox_env(self) -> dict[str, str]:
        """Placeholders and CA settings for every process in the sandbox."""
        return {**CA_ENV, **self.placeholders}

    def trust_script(self) -> tuple[str, bytes]:
        """(shell script, stdin) that installs the session CA in the sandbox, as root."""
        script = (f"cat > {CA_FILE} && cat {CA_FILE} >> {CA_BUNDLE} && chmod 644 {CA_FILE}")
        return script, self.ca.pem


__all__ = ["Egress", "EgressSession", "EgressProxy", "Policy", "SecretRule", "Allow", "Grant"]
