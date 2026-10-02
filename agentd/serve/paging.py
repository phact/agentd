"""Paging through JSONL files without reading them whole.

A cursor is a byte offset: where the next page starts (oldest-first) or
where it ends (newest-first). Clients treat it as opaque.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CHUNK = 64 * 1024


def _parse(line: bytes) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return {"raw": line.decode("utf-8", errors="replace").rstrip("\r\n")}


def parse_cursor(cursor: str | None) -> int | None:
    if cursor in (None, ""):
        return None
    if not str(cursor).isdigit():
        raise ValueError(f"bad cursor {cursor!r}")
    return int(cursor)


def page_jsonl(path: Path, *, cursor: str | None = None, limit: int = 50,
               order: str = "asc") -> tuple[list[Any], str | None]:
    """(records, next cursor or None) for one page of ``path``."""
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    limit = max(1, min(int(limit), 1000))
    offset = parse_cursor(cursor)
    size = path.stat().st_size
    with path.open("rb") as f:
        if order == "asc":
            start = offset or 0
            if start > size:
                raise ValueError("cursor is past the end of the transcript")
            f.seek(start)
            records = []
            pos = start
            while len(records) < limit:
                line = f.readline()
                if not line:
                    break
                pos += len(line)
                if line.strip():
                    records.append(_parse(line))
            return records, (str(pos) if pos < size else None)

        end = size if offset is None else offset
        if end > size:
            raise ValueError("cursor is past the end of the transcript")
        lines: list[tuple[int, bytes]] = []  # (start offset, line), newest first
        buf, buf_start = b"", end
        while len(lines) < limit and buf_start > 0:
            read = min(_CHUNK, buf_start)
            buf_start -= read
            f.seek(buf_start)
            buf = f.read(read) + buf
            parts = buf.split(b"\n")
            # parts[0] may be a partial line unless we reached the file start.
            complete, buf = (parts, b"") if buf_start == 0 else (parts[1:], parts[0])
            pos = buf_start + (0 if buf_start == 0 else len(buf) + 1)
            found = []
            for part in complete:
                found.append((pos, part))
                pos += len(part) + 1
            for start, part in reversed(found):
                if part.strip():
                    lines.append((start, part))
            if buf_start == 0:
                break
        page = lines[:limit]
        if not page:
            return [], None
        first = page[-1][0]
        return [_parse(line) for _, line in page], (str(first) if first > 0 else None)


def page_list(items: list[Any], *, cursor: str | None = None, limit: int = 50,
              order: str = "asc") -> tuple[list[Any], str | None]:
    """The same paging over an in-memory list (the cursor is an index)."""
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    limit = max(1, min(int(limit), 1000))
    offset = parse_cursor(cursor)
    if order == "asc":
        start = offset or 0
        page = items[start:start + limit]
        nxt = start + len(page)
        return page, (str(nxt) if nxt < len(items) else None)
    end = len(items) if offset is None else min(offset, len(items))
    start = max(0, end - limit)
    return list(reversed(items[start:end])), (str(start) if start > 0 else None)
