"""Raw Claude Code sessions on this box (``~/.claude/projects``), read-only.

These are sessions run directly on the host, outside agentd. agentd's own
sandboxed sessions are copied there too (transcript sync), so they are left
out: they are served as agentd sessions instead.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentd.harness import transcripts

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")


def agentd_claude_ids(transcripts_root: Path | None, extra: set[str] = frozenset()) -> set[str]:
    """Claude Code session ids agentd ran (its transcript store), plus ``extra``."""
    root = Path(transcripts_root or transcripts.DEFAULT_HOME / "transcripts") / "claude-code"
    ids = set(extra)
    if root.is_dir():
        ids |= {p.stem for p in root.glob("*/*.jsonl")}
    return ids


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _summary(path: Path) -> dict[str, Any]:
    """cwd and title (first real user message) from the head of a transcript."""
    cwd, title = None, None
    with path.open("rb") as f:
        for _, line in zip(range(200), f):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            cwd = cwd or rec.get("cwd")
            if title is None and rec.get("type") == "user" and not rec.get("isMeta"):
                text = _text((rec.get("message") or {}).get("content")).strip()
                if text and not text.startswith("<"):
                    title = text[:120]
            if cwd and title:
                break
    return {"cwd": cwd, "title": title}


def list_sessions(root: Path, exclude: set[str]) -> list[dict[str, Any]]:
    """Newest first."""
    out = []
    if not root.is_dir():
        return out
    for path in root.glob("*/*.jsonl"):
        if path.stem in exclude:
            continue
        st = path.stat()
        out.append({"id": path.stem, "project": path.parent.name, "size": st.st_size,
                    "modified": st.st_mtime, **_summary(path)})
    out.sort(key=lambda s: s["modified"], reverse=True)
    return out


def find_session(root: Path, session_id: str, exclude: set[str]) -> Path | None:
    if not _ID.match(session_id) or session_id in exclude:
        return None
    matches = sorted(root.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None
