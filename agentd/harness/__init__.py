"""Agent harnesses that run inside an agentd sandbox session.

``harness="ptc"`` is agentd's own loop (the model writes code fences, agentd
runs them in the executor). The others run a whole third-party agent — its
loop, tools and all — inside the same :class:`~agentd.sandbox.session.SandboxSession`:

  * ``"claude-code"``  Claude Code CLI via ``claude -p --output-format stream-json``
  * ``"codex"``        OpenAI Codex CLI via ``codex exec --json``

Every harness gets the same workspace, skills (MCP tools via the skills CLI)
and model proxy. Its session transcripts are kept on the host and synced to
the CLI's usual location (see :mod:`agentd.harness.transcripts`).
"""
from __future__ import annotations

from pathlib import Path

from agentd.harness import transcripts
from agentd.harness.transcripts import claude_project_dirname  # noqa: F401  (public helper)
from agentd.sandbox.session import TranscriptMount

HARNESSES = ("ptc", "claude-code", "codex")


def default_transcript_mounts(workspace: Path, root: Path | None = None) -> dict[str, TranscriptMount]:
    """Each harness's transcript store, mounted at its own location in the sandbox,
    and synced to the CLI's native path on the host (see agentd.harness.transcripts).
    ``root`` replaces ``~/.agentd/transcripts`` as the store root."""
    return {
        name: TranscriptMount(
            transcripts.sandbox_dir(name, workspace),
            transcripts.store_dir(name, workspace, root),
            transcripts.native_dir(name, workspace),
        )
        for name in transcripts.HARNESS_NAMES
    }


def get_harness(name: str):
    if name == "claude-code":
        from agentd.harness.claude_code import ClaudeCodeHarness

        return ClaudeCodeHarness
    if name == "codex":
        from agentd.harness.codex import CodexHarness

        return CodexHarness
    raise ValueError(f"unknown harness {name!r}; expected one of {HARNESSES}")
