"""What a sandbox's connections may do: fnox's secret rules plus agentd's allowlist.

Secret rules come from fnox's ``[proxy.rules]`` (the config files fnox itself
would load from the workspace, ``fnox config-files``)::

    [[proxy.rules]]
    secret = "GITHUB_TOKEN"
    domain = "api.github.com"
    header = "authorization"
    methods = ["GET"]            # default: any
    paths = ["/repos/me/**"]     # default: any
    placeholder = "ghp_000..."   # default: generated

A connection to a rule's domain on port 443 is intercepted (TLS terminated with
the session's CA) so the placeholder can be swapped for the real value in the
rule's header, for matching methods and paths only.

The allowlist (``Egress(allow=[...])`` and grants) lets connections through
untouched: ``"pypi.org"`` (ports 443 and 80), ``"github.com:22"``,
``"*.githubusercontent.com"``, ``"10.0.2.58:8001"``. Everything else is
refused (or, with approvals on, asked about).
"""
from __future__ import annotations

import fnmatch
import os
import re
import secrets as _secrets
import string
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_PORTS = (443, 80)


@dataclass(frozen=True)
class SecretRule:
    secret: str
    domain: str
    header: str = "authorization"
    methods: tuple[str, ...] = ()     # empty: any
    paths: tuple[str, ...] = ()       # globs; empty: any
    placeholder: str | None = None
    port: int = 443                   # fnox rules are HTTPS on 443

    def matches(self, method: str, path: str) -> bool:
        if self.methods and method.upper() not in self.methods:
            return False
        path = path.split("?", 1)[0]
        return not self.paths or any(_glob(p, path) for p in self.paths)

    def describe(self) -> str:
        return (f"{self.secret} -> {self.domain} methods={','.join(self.methods) or '*'} "
                f"paths={','.join(self.paths) or '*'} header={self.header}")


def _glob(pattern: str, path: str) -> bool:
    """fnox-style path globs: ``**`` spans segments, ``*`` stays within one."""
    regex = re.escape(pattern).replace(r"\*\*", "\0").replace(r"\*", "[^/]*").replace("\0", ".*")
    return re.fullmatch(regex, path) is not None


@dataclass(frozen=True)
class Allow:
    host: str                 # exact name, "*.suffix", or an IP literal
    ports: tuple[int, ...]    # ports allowed

    @classmethod
    def parse(cls, spec: str) -> "Allow":
        host, _, port = spec.rpartition(":") if spec.count(":") == 1 else (spec, "", "")
        if not port:
            return cls(spec.lower(), DEFAULT_PORTS)
        return cls(host.lower(), (int(port),))

    def matches(self, host: str | None, ip: str, port: int) -> bool:
        if port not in self.ports:
            return False
        names = [n for n in (host, ip) if n]
        return any(fnmatch.fnmatchcase(n.lower(), self.host) for n in names)


@dataclass
class Policy:
    rules: list[SecretRule] = field(default_factory=list)
    allows: list[Allow] = field(default_factory=list)
    # Single-use grants ("once" decided while nothing was held): gone once used.
    once_rules: list[SecretRule] = field(default_factory=list)
    once_allows: list[Allow] = field(default_factory=list)

    def rules_for(self, host: str | None, port: int | None = None) -> list[SecretRule]:
        return [r for r in self.rules + self.once_rules
                if host and r.domain == host.lower() and (port is None or r.port == port)]

    def use(self, rule: SecretRule) -> None:
        """A rule was used to send its secret: a single-use one is spent."""
        for i, r in enumerate(self.once_rules):
            if r is rule:
                del self.once_rules[i]
                return

    def connect(self, host: str | None, ip: str, port: int) -> str:
        """"intercept" | "pass" | "deny" for a new connection."""
        if self.rules_for(host, port):
            return "intercept"
        if any(a.matches(host, ip, port) for a in self.allows):
            return "pass"
        for i, a in enumerate(self.once_allows):
            if a.matches(host, ip, port):
                del self.once_allows[i]
                return "pass"
        return "deny"


# --------------------------------------------------------------------------- #
# fnox
# --------------------------------------------------------------------------- #

def fnox_config_files(cwd: Path, *, fnox: str = "fnox", profile: str | None = None) -> list[Path]:
    """The config files fnox would load from ``cwd`` (its own discovery)."""
    from agentd import fnox as _fnox

    return _fnox.config_files(cwd, fnox=fnox, profile=profile)


def load_rules(files: list[Path]) -> list[SecretRule]:
    """``[[proxy.rules]]`` from fnox config files (all of them, in order)."""
    rules: list[SecretRule] = []
    for path in files:
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for r in (data.get("proxy") or {}).get("rules") or []:
            if not r.get("secret") or not r.get("domain"):
                continue
            rules.append(SecretRule(
                secret=r["secret"], domain=str(r["domain"]).lower(),
                header=str(r.get("header", "authorization")).lower(),
                methods=tuple(m.upper() for m in r.get("methods") or [] if m != "*"),
                paths=tuple(p for p in r.get("paths") or [] if p not in ("*", "/**")),
                placeholder=r.get("placeholder"),
            ))
    return rules


def load_secret_names(files: list[Path], profile: str | None = None) -> dict[str, str]:
    """``{name: description}`` of the secrets fnox config files define (top-level
    ``[secrets]`` plus the profile's), read from the files: no values, no providers."""
    profiles = [p.strip() for p in (profile or os.environ.get("FNOX_PROFILE") or "").split(",") if p.strip()]
    names: dict[str, str] = {}
    for path in files:
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, ValueError):
            continue
        tables = [data.get("secrets") or {}]
        tables += [((data.get("profiles") or {}).get(p) or {}).get("secrets") or {} for p in profiles]
        for table in tables:
            for name, spec in table.items():
                desc = spec.get("description") if isinstance(spec, dict) else None
                names[name] = str(desc or names.get(name) or "")
    return names


def fnox_get(name: str, cwd: Path, *, fnox: str = "fnox", profile: str | None = None) -> str:
    """One secret's value, from fnox on the host (no prompts; :class:`agentd.fnox.SecretMissing` if locked)."""
    from agentd import fnox as _fnox

    return _fnox.get(name, cwd, fnox=fnox, profile=profile)


def make_placeholder(value: str) -> str:
    """Same length as ``value``, keeping a short type prefix such as ``ghp_``
    (so SDKs that check the format accept it, and scrubbing never changes a
    response's length)."""
    m = re.match(r"^[A-Za-z]{2,8}[_-]", value)
    prefix = m.group(0) if m else ""
    alphabet = string.ascii_letters + string.digits
    body = "".join(_secrets.choice(alphabet) for _ in range(max(len(value) - len(prefix), 8)))
    return (prefix + body)[: max(len(value), len(prefix) + 8)]


def opaque_placeholder() -> str:
    """A placeholder for a secret not read yet (its format unknown)."""
    alphabet = string.ascii_letters + string.digits
    return "agentd_ph_" + "".join(_secrets.choice(alphabet) for _ in range(24))


def add_rule_to_fnox(config: Path, rule: SecretRule) -> None:
    """Append a rule to a fnox config ("always" grants live in fnox's config)."""
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["", "[[proxy.rules]]", f"secret = {q(rule.secret)}", f"domain = {q(rule.domain)}",
             f"header = {q(rule.header)}"]
    if rule.methods:
        lines.append("methods = [" + ", ".join(q(m) for m in rule.methods) + "]")
    if rule.paths:
        lines.append("paths = [" + ", ".join(q(p) for p in rule.paths) + "]")
    if rule.placeholder:
        lines.append(f"placeholder = {q(rule.placeholder)}")
    with open(config, "a") as f:
        f.write("\n".join(lines) + "\n")


def rules_summary(rules: list[SecretRule]) -> list[dict[str, Any]]:
    return [{"secret": r.secret, "domain": r.domain, "header": r.header, "methods": list(r.methods) or ["*"],
             "paths": list(r.paths) or ["*"]} for r in rules]
