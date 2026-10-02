"""Which harnesses are ready, and with which models, for a given sandbox.

    status = agentd.available(executor)            # or a patched client, or None (the default sandbox)
    status["codex"].ready, status["codex"].reasons
    status["codex"].default_model, [m.id for m in status["codex"].models]

A harness is ready when its CLI is in the executor's image and it has a
working route to a model:

  * ``claude-code``: Anthropic (``ANTHROPIC_API_KEY`` or your Claude Code
    login); models from the Anthropic models API.
  * ``codex``: a configured ``upstream`` (its ``/v1/models``), else
    ``OPENAI_API_KEY`` (OpenAI's model list), else your ChatGPT login (the
    Codex model list; its top listed model is the default).
  * ``opencode`` / ``omp``: a configured ``upstream``, Claude models
    (``omp`` also through your Claude login; ``opencode`` only with
    ``ANTHROPIC_API_KEY``: Anthropic rejects its requests made with a
    subscription login), OpenAI models with ``OPENAI_API_KEY``.
  * ``ptc``: agentd's own loop on the host; Claude models (your Claude login
    works through ``claude -p``) and OpenAI models with ``OPENAI_API_KEY``.

Model lists are fetched on the host with the same credentials agentd's model
proxies use (listing them also checks those credentials work), and cached
for ten minutes (``refresh=True`` to refetch). ``default_model`` is None when
the harness picks its own default (Claude Code, Codex with an API key).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentd.model_proxy import (ANTHROPIC_API, CHATGPT_BACKEND, OPENAI_API, ApiKeyCredentials, BearerCredentials,
                                ClaudeCodeCredentials, CodexChatGPTCredentials, CredentialError, ModelUpstream)

HARNESS_CLIS = {"claude-code": "claude", "codex": "codex", "opencode": "opencode", "omp": "omp"}
CACHE_TTL = 600.0
_NOT_CHAT = re.compile(r"whisper|tts|kokoro|embed|rerank|transcrib|speech|audio|realtime|moderation|dall-e|image",
                       re.IGNORECASE)
CODEX_CLIENT_VERSION = "0.159.2"


@dataclass
class ModelInfo:
    id: str
    name: str | None = None
    source: str = ""      # "anthropic", "openai", "chatgpt" or "upstream:<name>"


@dataclass
class HarnessStatus:
    name: str
    ready: bool
    reasons: list[str] = field(default_factory=list)   # why not ready (or notes when ready)
    default_model: str | None = None
    models: list[ModelInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Listing:
    ok: bool
    models: list[ModelInfo] = field(default_factory=list)
    error: str | None = None
    default: str | None = None


_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, refresh: bool):
    hit = _CACHE.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _store(key: str, value):
    _CACHE[key] = (time.monotonic(), value)
    return value


# --------------------------------------------------------------------------- #
# Model sources (host-side, with the proxies' credentials)
# --------------------------------------------------------------------------- #

async def _get_json(url: str, credentials, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    import aiohttp

    h = dict(headers or {})
    await credentials.apply(h)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        async with http.get(url, headers=h) as r:
            text = await r.text()
            try:
                return r.status, json.loads(text)
            except ValueError:
                return r.status, {"error": {"message": text[:200]}}


def _error(status: int, body: Any) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    message = err.get("message") if isinstance(err, dict) else (err or body.get("detail") if isinstance(body, dict) else body)
    return f"{status}: {message}"


async def _anthropic(refresh: bool) -> _Listing:
    key = os.environ.get("ANTHROPIC_API_KEY")
    cache_key = f"anthropic:{'key' if key else 'login'}"
    if (hit := _cached(cache_key, refresh)) is not None:
        return hit
    creds = ApiKeyCredentials(key) if key else ClaudeCodeCredentials()
    try:
        status, body = await _get_json(f"{ANTHROPIC_API}/v1/models?limit=1000", creds,
                                       {"anthropic-version": "2023-06-01"})
    except CredentialError as e:
        return _store(cache_key, _Listing(False, error=f"no Claude credentials: {e}"))
    except Exception as e:  # noqa: BLE001
        return _store(cache_key, _Listing(False, error=f"couldn't reach Anthropic: {e}"))
    if status != 200:
        return _store(cache_key, _Listing(False, error=f"Anthropic refused the {'API key' if key else 'Claude login'} "
                                                        f"({_error(status, body)})"))
    models = [ModelInfo(m["id"], m.get("display_name"), "anthropic") for m in body.get("data", [])]
    return _store(cache_key, _Listing(True, models))


async def _openai(refresh: bool) -> _Listing:
    """OPENAI_API_KEY: OpenAI's list. Else the ChatGPT login: Codex's list."""
    key = os.environ.get("OPENAI_API_KEY")
    cache_key = f"openai:{'key' if key else 'chatgpt'}"
    if (hit := _cached(cache_key, refresh)) is not None:
        return hit
    try:
        if key:
            status, body = await _get_json(f"{OPENAI_API}/v1/models", BearerCredentials(key))
            if status != 200:
                return _store(cache_key, _Listing(False, error=f"OpenAI refused the API key ({_error(status, body)})"))
            ids = sorted((m for m in body.get("data", []) if re.match(r"(gpt-|o\d|codex)", m["id"])
                          and not _NOT_CHAT.search(m["id"])), key=lambda m: m.get("created", 0), reverse=True)
            return _store(cache_key, _Listing(True, [ModelInfo(m["id"], None, "openai") for m in ids]))
        creds = CodexChatGPTCredentials()
        status, body = await _get_json(
            f"{CHATGPT_BACKEND}/backend-api/codex/models?client_version={CODEX_CLIENT_VERSION}", creds)
    except CredentialError as e:
        return _store(cache_key, _Listing(False, error=f"no OpenAI credentials (OPENAI_API_KEY or `codex login`): {e}"))
    except Exception as e:  # noqa: BLE001
        return _store(cache_key, _Listing(False, error=f"couldn't reach OpenAI: {e}"))
    if status != 200:
        return _store(cache_key, _Listing(False, error=f"ChatGPT refused the Codex login ({_error(status, body)})"))
    listed = sorted((m for m in body.get("models", []) if m.get("visibility") == "list"),
                    key=lambda m: m.get("priority", 1 << 30))
    models = [ModelInfo(m["slug"], m.get("display_name"), "chatgpt") for m in listed]
    return _store(cache_key, _Listing(True, models, default=models[0].id if models else None))


async def _upstream(up: ModelUpstream, refresh: bool) -> _Listing:
    cache_key = f"upstream:{up.name}:{up.base_url}"
    if (hit := _cached(cache_key, refresh)) is not None:
        return hit
    try:
        status, body = await _get_json(up.base_url.rstrip("/") + "/models", up.credentials())
    except CredentialError as e:
        return _store(cache_key, _Listing(False, error=f"upstream {up.name}: {e}"))
    except Exception as e:  # noqa: BLE001
        return _store(cache_key, _Listing(False, error=f"upstream {up.name} ({up.base_url}) is unreachable: {e}"))
    if status != 200:
        return _store(cache_key, _Listing(False, error=f"upstream {up.name} answered {_error(status, body)}"))
    models = [ModelInfo(m["id"], None, f"upstream:{up.name}") for m in body.get("data", [])
              if not _NOT_CHAT.search(m["id"])]
    return _store(cache_key, _Listing(True, models))


# --------------------------------------------------------------------------- #
# Which CLIs the sandbox image has
# --------------------------------------------------------------------------- #

def _image_target(executor) -> tuple[str, str, str | None]:
    """(backend, image location, colima profile) for an executor (None: the default sandbox)."""
    from agentd.sandbox import executor as ex

    if executor is None:
        choice = os.environ.get("AGENTD_SANDBOX") or (
            "krun" if ex.krun_available() else "krun-colima" if ex.colima_available()
            else "docker" if ex.docker_available() else None)
        if choice is None:
            return "none", "", None
        if choice == "krun":
            return "krun", str(ex.DEFAULT_ROOTFS), None
        if choice == "krun-colima":
            from agentd.sandbox import colima

            return "krun-colima", colima.vm_rootfs("agents"), colima.PROFILE
        return "docker", ex.DEFAULT_IMAGE, None
    if getattr(executor, "backend", "") == "docker":
        return "docker", executor.image, None
    if getattr(executor, "colima", None):
        return "krun-colima", str(executor.rootfs), executor.colima
    return "krun", str(executor.rootfs), None


def _clis_in_image(backend: str, location: str, profile: str | None, refresh: bool) -> set[str] | str:
    """The harness CLIs in the image, or an error string."""
    key = f"clis:{backend}:{location}:{profile}"
    if (hit := _cached(key, refresh)) is not None:
        return hit
    names = sorted(set(HARNESS_CLIS.values()))
    script = "for c in " + " ".join(names) + "; do p=" + "{root}/usr/local/bin/$c; [ -L \"$p\" -o -e \"$p\" ] && echo $c; done"
    try:
        if backend == "none":
            return "no sandbox is set up (see `agentd-sandbox status`)"
        if backend == "krun":
            root = Path(location)
            if not (root / "usr").is_dir():
                return _store(key, f"no base image at {root}")
            return _store(key, {c for c in names if os.path.lexists(root / "usr" / "local" / "bin" / c)})
        if backend == "krun-colima":
            from agentd.sandbox import colima

            r = colima.vm_sh(profile, script.format(root=location))
            if r.returncode not in (0, 1):
                return f"couldn't inspect the image in Colima VM {profile!r}: {(r.stderr or '').strip()[:200]}"
            return _store(key, set(r.stdout.split()))
        if shutil.which("docker") is None:
            return "docker is not installed"
        check = "for c in " + " ".join(names) + "; do command -v $c >/dev/null && echo $c; done"
        r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", location, "-c", check],
                           capture_output=True, text=True, timeout=120)
        if r.returncode not in (0, 1):
            return f"couldn't inspect Docker image {location}: {r.stderr.strip()[:200]}"
        return _store(key, set(r.stdout.split()))
    except (OSError, subprocess.SubprocessError) as e:
        return f"couldn't inspect the sandbox image: {e}"


# --------------------------------------------------------------------------- #
# Putting it together
# --------------------------------------------------------------------------- #

def _upstream_of(options: dict) -> ModelUpstream | None:
    up = options.get("upstream")
    return ModelUpstream(**up) if isinstance(up, dict) else up


async def available_async(target: Any = None, *, harness_options: dict[str, dict] | None = None,
                          refresh: bool = False) -> dict[str, HarnessStatus]:
    """Per harness: ready (and why not), default model and models. ``target`` is
    a sandbox executor, a client patched with ``patch_openai_with_ptc`` (its
    executor and harness options), or None for the default sandbox."""
    executor = target
    if target is not None and hasattr(target, "_harness_options"):  # a patched client
        harness_options = {**getattr(target, "_harness_options", {}), **(harness_options or {})}
        executor = getattr(target, "_agentd_executor", None)
    harness_options = harness_options or {}
    key_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    key_openai = bool(os.environ.get("OPENAI_API_KEY"))

    backend, location, profile = _image_target(executor)
    upstreams = {h: _upstream_of(o) for h, o in harness_options.items() if _upstream_of(o)}
    tasks = [asyncio.to_thread(_clis_in_image, backend, location, profile, refresh), _anthropic(refresh),
             _openai(refresh)] + [_upstream(u, refresh) for u in upstreams.values()]
    clis, anthropic, openai, *ups = await asyncio.gather(*tasks)
    up_listing = dict(zip(upstreams, ups))

    def harness(name: str, routes: list[tuple[_Listing, str | None]], default: str | None) -> HarnessStatus:
        st = HarnessStatus(name, ready=True)
        cli = HARNESS_CLIS.get(name)
        if cli:
            if isinstance(clis, str):
                st.ready, st.reasons = False, [clis]
            elif cli not in clis:
                st.ready = False
                st.reasons.append(f"`{cli}` is not in the sandbox image ({location})")
        usable = [(lst, why) for lst, why in routes if lst.ok]
        for lst, why in routes:
            if not lst.ok:
                st.reasons.append(lst.error or "model route unavailable")
        if not usable:
            st.ready = False
            if not routes:
                st.reasons.append("no model route: configure an upstream or credentials")
        for lst, _ in usable:
            st.models += [m for m in lst.models if m.id not in {x.id for x in st.models}]
        options = harness_options.get(name, {})
        st.default_model = options.get("model") or default
        if st.ready and st.default_model is None and name in ("opencode", "omp"):
            st.reasons.append("pass model= (these harnesses have no default model)")
        return st

    out: dict[str, HarnessStatus] = {}
    out["ptc"] = harness("ptc", [(anthropic, None)] + ([(openai, None)] if key_openai else []), None)
    out["claude-code"] = harness("claude-code", [(anthropic, None)], None)
    if "codex" in up_listing:
        lst = up_listing["codex"]
        out["codex"] = harness("codex", [(lst, None)], lst.models[0].id if lst.ok and lst.models else None)
    else:
        out["codex"] = harness("codex", [(openai, None)], openai.default)
    for name in ("opencode", "omp"):
        routes: list[tuple[_Listing, str | None]] = []
        default = None
        if name in up_listing:
            lst = up_listing[name]
            routes.append((lst, None))
            default = lst.models[0].id if lst.ok and lst.models else None
        if key_anthropic or name == "omp":
            routes.append((anthropic, None))
        if key_openai:
            routes.append((openai, None))
        st = harness(name, routes, default)
        if name == "opencode" and not key_anthropic:
            st.reasons.append("Claude models need ANTHROPIC_API_KEY (Anthropic rejects OpenCode with a Claude login)")
        out[name] = st
    return out


def available(target: Any = None, *, harness_options: dict[str, dict] | None = None,
              refresh: bool = False) -> dict[str, HarnessStatus]:
    """Synchronous :func:`available_async` (call that one from async code)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(available_async(target, harness_options=harness_options, refresh=refresh))
    raise RuntimeError("agentd.available() was called from async code; await agentd.available_async() instead")
