"""Every fnox call agentd makes: reads, and unlocking with a password the host supplies.

fnox's daemon (``[daemon] enabled = true`` in the fnox config) is the only
cache. Reads never prompt: a secret whose vault is locked comes back empty and
raises :class:`SecretMissing`. :func:`fill` unlocks: it runs an interactive
``fnox get`` per name on a pty, answers the master-password prompt, and the
daemon keeps the value for later reads.

Two fnox details this depends on:

  * The daemon's cache key hashes every ``FNOX_*`` variable (and provider ones
    such as ``ENPASS_PASSWORD``), so reads and fills run with the same
    environment, with no ``FNOX_*`` variables (``FNOX_PROFILE`` becomes
    ``-P``). Non-interactive reads use the flag, not ``FNOX_NON_INTERACTIVE``.
  * The cache is split by command (``get`` vs ``exec``) and by secret, so a
    fill is one ``fnox get`` per name.
"""
from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import shutil
import termios
import time
import warnings
import weakref
from pathlib import Path


class SecretMissing(LookupError):
    """fnox has no value for these secrets right now (their vault is locked)."""

    def __init__(self, names: list[str] | str):
        self.names = [names] if isinstance(names, str) else list(names)
        super().__init__(f"{', '.join(self.names)}: locked in fnox (needs unlocking)")


class WrongPassword(ValueError):
    """The master password didn't unlock the vault (the approval stays pending: ask again)."""


class UnlockFailed(RuntimeError):
    """The vault unlocked but fnox still couldn't read a secret, e.g. its item was renamed or deleted."""


WRONG_PASSWORD = "wrong password"
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")


def _reason(output: bytes, code: int) -> str:
    """Why an interactive ``fnox get`` failed, from what it printed: WRONG_PASSWORD, or fnox's own error."""
    text = _ANSI.sub("", output.decode(errors="replace")).replace("\r", "")
    if "auth_failed" in text or "Could not unlock" in text:
        return WRONG_PASSWORD
    kind = re.search(r"fnox::[\w:]+", text)
    message = re.search(r"×\s*(.+)", text)
    if message:
        return message.group(1).strip() + (f" ({kind.group(0)})" if kind else "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else f"fnox exited {code}"


_clear_hooks: "weakref.WeakSet" = weakref.WeakSet()


def on_clear(obj) -> None:
    """Call ``obj.forget_secrets()`` whenever :func:`clear` runs (e.g. an egress proxy's read values)."""
    _clear_hooks.add(obj)


def clear(*, fnox: str = "fnox") -> None:
    """Clear every running fnox daemon's cache (after a vault changes), and make agentd forget
    values it read, so the next use reads (or unlocks) again."""
    r = subprocess.run([fnox, "daemon", "clear"], env=env(), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"fnox daemon clear failed: {(r.stderr or r.stdout).strip()[:300]}")
    for obj in list(_clear_hooks):
        obj.forget_secrets()


def env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("FNOX_")}


def _base(fnox: str, profile: str | None) -> list[str]:
    profile = profile or os.environ.get("FNOX_PROFILE")
    return [fnox] + (["-P", profile] if profile else [])


def get(name: str, cwd: str | Path, *, fnox: str = "fnox", profile: str | None = None) -> str:
    """One secret's value, without prompting. :class:`SecretMissing` if fnox has none now."""
    r = subprocess.run(_base(fnox, profile) + ["--non-interactive", "get", name], cwd=cwd, env=env(),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"fnox couldn't get {name}: {(r.stderr or r.stdout).strip()[:300]}")
    value = r.stdout.rstrip("\n")
    if not value:
        raise SecretMissing(name)
    return value


def uncached(names: list[str], cwd: str | Path, *, fnox: str = "fnox", profile: str | None = None) -> list[str]:
    """The names fnox can't read without an unlock."""
    missing = []
    for name in dict.fromkeys(names):
        try:
            get(name, cwd, fnox=fnox, profile=profile)
        except SecretMissing:
            missing.append(name)
    return missing


def _fill_one(name: str, password: bytearray, cwd: str | Path, fnox: str, profile: str | None,
              timeout: float) -> str | None:
    """``fnox get NAME`` on a pty, answering the master-password prompt. None if it worked, else why not."""
    argv = _base(fnox, profile) + ["get", name]
    argv[0] = shutil.which(argv[0]) or argv[0]
    child_env = env()
    cwd = os.fspath(cwd)
    null = os.open(os.devnull, os.O_WRONLY)
    # The child only makes system calls before exec (no imports, no allocation-heavy
    # Python), so forking from a threaded process (agentd serve) is safe.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid, fd = pty.fork()
    if pid == 0:  # child: the value goes nowhere, the prompt comes to us on the pty
        try:
            os.chdir(cwd)
            os.dup2(null, 1)
            os.execve(argv[0], argv, child_env)
        finally:
            os._exit(127)
    os.close(null)
    answered = False
    seen = b""
    output = b""  # what fnox printed after the prompt (its error, if any)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # the child closed the pty
                break
            if not chunk:
                break
            seen = (seen + chunk)[-512:]
            if answered:
                output = (output + chunk)[-8192:]
            # A prompt ends with "password ...: " and no newline yet.
            if not answered and b"password" in seen.lower() and seen.rstrip().endswith(b":"):
                attrs = termios.tcgetattr(fd)
                attrs[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
                os.write(fd, memoryview(password))
                os.write(fd, b"\n")
                answered = True
                seen = b""
    finally:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
            if not done:
                os.kill(pid, signal.SIGKILL)
                done, status = os.waitpid(pid, 0)
        except ChildProcessError:
            status = 1
        os.close(fd)
    code = os.waitstatus_to_exitcode(status)
    if code == 0:
        return None  # (no prompt: it was already unlocked)
    return _reason(output if answered else seen, code)


def fill(names: list[str], password: bytearray, *, cwd: str | Path, fnox: str = "fnox",
         profile: str | None = None, timeout: float = 60) -> dict[str, str]:
    """Unlock ``names`` in fnox's daemon with ``password`` (zeroed when done).

    Returns ``{name: why}`` for the names that failed: :data:`WRONG_PASSWORD`, or
    fnox's error (e.g. the item was renamed or deleted in the vault)."""
    failed: dict[str, str] = {}
    try:
        for name in dict.fromkeys(names):
            why = _fill_one(name, password, cwd, fnox, profile, timeout)
            if why is not None:
                failed[name] = why
    finally:
        for i in range(len(password)):
            password[i] = 0
    if not failed and uncached(names, cwd, fnox=fnox, profile=profile):
        raise RuntimeError("fnox unlocked the vault but didn't keep the values: enable its daemon "
                           "([daemon] enabled = true in the fnox config)")
    return failed


def config_files(cwd: str | Path, *, fnox: str = "fnox", profile: str | None = None) -> list[Path]:
    """The config files fnox would load from ``cwd`` (its own discovery)."""
    try:
        r = subprocess.run(_base(fnox, profile) + ["config-files"], cwd=cwd, env=env(), capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [Path(line.strip()) for line in r.stdout.splitlines() if line.strip().endswith(".toml")]
