import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _test_state_root():
    # Under ~/.agentd so Docker Desktop / Colima share it with their VM.
    from agentd.sandbox.base import DEFAULT_HOME

    (DEFAULT_HOME / "tmp").mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="state-", dir=DEFAULT_HOME / "tmp"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _keep_test_state_out_of_real_dirs(monkeypatch, _test_state_root):
    """Tests never write to the real ~/.claude/projects, ~/.codex/sessions, ~/.omp,
    ~/.agentd/transcripts, ~/.agentd/responses or ~/.agentd/browser."""
    from agentd.harness import responses, transcripts

    root = _test_state_root
    original_native = transcripts.native_dir
    def native(harness, workspace):
        real = original_native(harness, workspace)
        return None if real is None else root / "native" / harness / real.name
    monkeypatch.setattr(transcripts, "native_dir", native)
    monkeypatch.setattr(transcripts, "DEFAULT_HOME", root)
    from agentd.harness import chat

    monkeypatch.setattr(chat, "SESSIONS_DIR", root / "harness-sessions")
    monkeypatch.setattr(responses, "RESPONSES_DIR", root / "responses")
    from agentd.devices import browser, browser_profile

    per_test = Path(tempfile.mkdtemp(prefix="browser-", dir=root))  # attempts and locks don't leak between tests
    monkeypatch.setattr(browser_profile, "ROOT", per_test)
    monkeypatch.setattr(browser, "ROOT", per_test)


def _live_backends():
    from agentd.sandbox.executor import docker_available, krun_available

    from agentd.sandbox.executor import colima_available

    return [b for b, ok in (("krun", krun_available()), ("krun-colima", colima_available()),
                            ("docker", docker_available())) if ok]


@pytest.fixture(params=["krun", "krun-colima", "docker"])
def live_executor(request):
    """Executor factory per backend for live tests (workspaces under ~/.agentd/tmp)."""
    from agentd.sandbox.executor import DockerExecutor, KrunExecutor

    if request.param not in _live_backends():
        pytest.skip(f"{request.param} sandbox not set up")
    if request.param == "krun-colima":
        return lambda **kw: KrunExecutor(colima=True, **kw)
    return KrunExecutor if request.param == "krun" else DockerExecutor
