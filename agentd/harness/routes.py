"""Where a model-agnostic harness (OpenCode, omp) sends its model calls.

The sandbox has no network: every route is a sandbox-side endpoint tunneled
to a host proxy that adds the real credential.

  * ``upstream=ModelUpstream(...)``: that OpenAI-compatible server (a LAN box,
    vLLM, ...), proxied on its own sandbox port.
  * a ``claude-*`` model (or ``anthropic/...``): agentd's Anthropic endpoint
    (``ANTHROPIC_API_KEY`` on the host, else your Claude Code login).
  * any other model: agentd's OpenAI endpoint, when the host has
    ``OPENAI_API_KEY`` (a ChatGPT login only works through Codex's own backend).
"""
from __future__ import annotations

from dataclasses import dataclass

from agentd.harness.chat import _is_claude_model
from agentd.model_proxy import BearerCredentials, ModelUpstream
from agentd.sandbox.session import ANTHROPIC_ENDPOINT, OPENAI_ENDPOINT

PLACEHOLDER_KEY = "agentd-sandbox-placeholder"  # the host proxy replaces it


@dataclass(frozen=True)
class ModelRoute:
    api: str        # "anthropic" | "openai-responses" | "openai-chat"
    origin: str     # e.g. http://127.0.0.1:8080, as seen in the sandbox
    base_url: str   # origin + API base path, e.g. http://127.0.0.1:8090/v1
    model: str      # model id at that API


async def resolve(executor, session, model: str | None, upstream: ModelUpstream | None, harness: str) -> ModelRoute:
    if upstream is not None:
        if not model:
            raise ValueError(f"{harness}: pass model= (the model's id on {upstream.base_url})")
        base = await executor.run(session.model_upstream(upstream))
        origin = base[: len(base) - len(upstream.path)] if upstream.path else base
        return ModelRoute("openai-chat" if upstream.api == "chat" else "openai-responses", origin, base, model)
    if not model:
        raise ValueError(f"{harness}: pass model= (e.g. a claude-* model, or an upstream's model)")
    bare = model.split("/", 1)[1] if model.startswith(("anthropic/", "openai/")) else model
    if model.startswith("anthropic/") or _is_claude_model(bare):
        origin = f"http://{ANTHROPIC_ENDPOINT[1]}:{ANTHROPIC_ENDPOINT[2]}"
        return ModelRoute("anthropic", origin, origin + "/v1", bare)
    if isinstance(session.openai_credentials, BearerCredentials):
        origin = f"http://{OPENAI_ENDPOINT[1]}:{OPENAI_ENDPOINT[2]}"
        return ModelRoute("openai-responses", origin, origin + "/v1", bare)
    raise ValueError(f"{harness}: model {model!r} needs upstream=ModelUpstream(...) or OPENAI_API_KEY on the host "
                     "(a ChatGPT login only works with the codex harness)")
