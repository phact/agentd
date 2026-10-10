"""The harness-neutral event stream every harness yields."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class HarnessEvent:
    """One thing a harness did: ``text``, ``tool_use``, ``tool_result``, ``task`` or ``result``.

    ``result`` is always last: its ``text`` is the final reply and
    ``session_id`` the harness's native session (for resume). A
    ``tool_result`` carries the ``id`` of the ``tool_use`` it answers. A
    ``task`` (``data``: the CLI's notification) is a background task that
    finished, at the start of the unprompted turn it set off.
    """

    kind: str
    text: str = ""
    name: str = ""
    data: Any = None
    session_id: str | None = None
    is_error: bool = False
    id: str = ""
