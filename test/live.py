"""Helpers for live tests (AGENTD_LIVE=1: real sandboxes, real model calls)."""
import os
import tempfile

import pytest


def live_tmp():
    from agentd.sandbox.base import DEFAULT_HOME

    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=DEFAULT_HOME / "tmp")


live = pytest.mark.skipif(
    os.environ.get("AGENTD_LIVE") != "1",
    reason="set AGENTD_LIVE=1 (makes real model calls)",
)
