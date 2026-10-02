"""Where harness session transcripts live, and syncing them to the real CLIs' paths.

Every harness gets the same setup:

  * **store**  ``~/.agentd/transcripts/<harness>/<encoded workspace>/`` on the
    host, mounted into the sandbox at the harness's own location. The sandbox
    sees only this workspace's transcripts, never the rest of your history.
  * **native** where the CLI keeps sessions on the host:
    ``~/.claude/projects/<encoded workspace>/`` (``$CLAUDE_CONFIG_DIR``),
    ``~/.codex/sessions/`` (``$CODEX_HOME``) and
    ``~/.omp/agent/sessions/--<workspace>--/``. OpenCode keeps sessions in a
    SQLite database, so its store holds one ``opencode export`` JSON file per
    session instead, with no native copy (``opencode import FILE`` on the
    host brings one in).

After each turn (and when the session stops) new or changed store files are
copied to the native path, so ``claude --resume`` / ``codex resume`` on the
host find sandbox sessions. Before resuming a session by id, its files are
pulled from the native path if they are newer there (e.g. you continued it
on the host). Copies keep mtimes, so a file is only copied when it changed.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from agentd.sandbox.base import DEFAULT_HOME
from agentd.sandbox.session import SANDBOX_HOME

HARNESS_NAMES = ("claude-code", "codex", "opencode", "omp")


def claude_project_dirname(cwd: str | Path) -> str:
    """The folder name Claude Code files a cwd's sessions under."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def omp_dirname(cwd: str | Path) -> str:
    """The folder name omp files a cwd's sessions under."""
    return "--" + str(cwd).strip("/").replace("/", "-") + "--"


def store_dir(harness: str, workspace: str | Path, root: Path | None = None) -> Path:
    """agentd's copy of ``harness``'s transcripts for ``workspace``."""
    return Path(root or DEFAULT_HOME / "transcripts") / harness / claude_project_dirname(workspace)


def sandbox_dir(harness: str, workspace: str | Path) -> str:
    """Where the harness looks for its sessions inside the sandbox."""
    if harness == "claude-code":
        return f"{SANDBOX_HOME}/.claude/projects/{claude_project_dirname(workspace)}"
    if harness == "codex":
        return f"{SANDBOX_HOME}/.codex/sessions"
    if harness == "omp":
        return f"{SANDBOX_HOME}/.omp/agent/sessions/{omp_dirname(workspace)}"
    if harness == "opencode":
        return f"{SANDBOX_HOME}/.agentd/opencode-sessions"
    raise ValueError(f"no transcripts for harness {harness!r}")


def native_dir(harness: str, workspace: str | Path) -> Path | None:
    """Where the CLI keeps these sessions on the host (None: no file-based copy)."""
    if harness == "claude-code":
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        return config / "projects" / claude_project_dirname(workspace)
    if harness == "codex":
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if harness == "omp":
        return Path.home() / ".omp" / "agent" / "sessions" / omp_dirname(workspace)
    if harness == "opencode":
        return None
    raise ValueError(f"no transcripts for harness {harness!r}")


def _copy_if_newer(src: Path, dst: Path) -> bool:
    """Copy ``src`` over ``dst`` if ``dst`` is missing or older; keeps mtime."""
    s = src.stat()
    if dst.exists():
        d = dst.stat()
        if d.st_mtime_ns >= s.st_mtime_ns and d.st_size == s.st_size:
            return False
        if d.st_mtime_ns > s.st_mtime_ns:
            return False  # destination changed more recently; leave it
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.agentd-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return True


def _files(root: Path) -> list[Path]:
    # Dotfiles are locks and temporaries (ours, omp's), never transcripts.
    return [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")] if root.is_dir() else []


def sync_out(store: Path, native: Path) -> list[Path]:
    """Copy new/changed store files to the native path; returns what was copied."""
    copied = []
    for src in _files(store):
        dst = native / src.relative_to(store)
        if _copy_if_newer(src, dst):
            copied.append(dst)
    return copied


def session_files(harness: str, root: Path, session_id: str) -> list[Path]:
    """The files that make up one session under ``root`` (store or native)."""
    if not root.is_dir():
        return []
    if harness == "claude-code":
        files = [root / f"{session_id}.jsonl"] if (root / f"{session_id}.jsonl").is_file() else []
        return files + _files(root / session_id)  # per-session subdir (e.g. subagents)
    if harness == "codex":
        return [p for p in root.rglob(f"rollout-*{session_id}.jsonl") if p.is_file()]
    if harness == "omp":
        return [p for p in root.glob(f"*_{session_id}.jsonl") if p.is_file()]
    if harness == "opencode":
        return [root / f"{session_id}.json"] if (root / f"{session_id}.json").is_file() else []
    return []


def pull_in(harness: str, store: Path, native: Path | None, session_id: str) -> list[Path]:
    """Bring one session's files from the native path into the store if newer there."""
    copied = []
    if native is None:  # no file-based native copy (OpenCode)
        return copied
    for src in session_files(harness, native, session_id):
        dst = store / src.relative_to(native)
        if _copy_if_newer(src, dst):
            copied.append(dst)
    return copied
