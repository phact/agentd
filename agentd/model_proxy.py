"""Credential-injecting model API proxy.

Sandboxes never hold model credentials. A harness inside the sandbox points
its base URL (e.g. ``ANTHROPIC_BASE_URL``) at a sandbox-side endpoint, the
sandbox connector tunnels that to this proxy on the host, and the proxy
strips whatever auth the sandbox sent, adds the real credential, and streams
the upstream response back untouched.

The proxy listens on a host Unix socket, so it is not reachable over the
network; sandboxes reach it only through their host-dialed connection.

Credential sources:
  * ``ApiKeyCredentials``          -> ``x-api-key`` (Anthropic API key)
  * ``ClaudeKeychainCredentials``  -> the Claude Code subscription OAuth token
    from the macOS Keychain, sent as ``Authorization: Bearer``.
  * ``BearerCredentials``          -> ``Authorization: Bearer`` (OpenAI API key)
  * ``CodexChatGPTCredentials``    -> the Codex CLI's ChatGPT login from
    ``~/.codex/auth.json``: ``Authorization`` plus ``ChatGPT-Account-ID``.

Subscription tokens are re-read from the host's store when they change or
near expiry; the proxy never refreshes them itself, because refresh tokens
rotate and doing so would log out the host's own CLI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

ANTHROPIC_API = "https://api.anthropic.com"
OPENAI_API = "https://api.openai.com"
CHATGPT_BACKEND = "https://chatgpt.com"
OAUTH_BETA = "oauth-2025-04-20"

# Hop-by-hop headers plus any auth the sandbox might try to supply.
_STRIP_REQUEST = {
    "host", "connection", "keep-alive", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
    "authorization", "x-api-key", "chatgpt-account-id",
}
_STRIP_RESPONSE = {
    "connection", "keep-alive", "transfer-encoding", "content-length", "trailer", "upgrade",
}


class Credentials(Protocol):
    async def apply(self, headers: dict[str, str]) -> None: ...


class ApiKeyCredentials:
    def __init__(self, api_key: str):
        self._key = api_key

    async def apply(self, headers: dict[str, str]) -> None:
        headers["x-api-key"] = self._key


class ClaudeKeychainCredentials:
    """Claude Code subscription OAuth token, read from the host's store."""

    SERVICE = "Claude Code-credentials"
    REFRESH_MARGIN_S = 300

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def apply(self, headers: dict[str, str]) -> None:
        token = await self._current_token()
        headers["authorization"] = f"Bearer {token}"
        betas = [b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
        if OAUTH_BETA not in betas:
            betas.append(OAUTH_BETA)
        headers["anthropic-beta"] = ",".join(betas)

    async def _current_token(self) -> str:
        async with self._lock:
            if self._token is None or time.time() > self._expires_at - self.REFRESH_MARGIN_S:
                self._token, self._expires_at = await asyncio.to_thread(self._read)
            if time.time() >= self._expires_at:
                raise CredentialError(
                    "Claude Code OAuth token has expired; run `claude` on the host to refresh it"
                )
            return self._token

    @classmethod
    def _read(cls) -> tuple[str, float]:
        if sys.platform == "darwin":
            r = subprocess.run(
                ["security", "find-generic-password", "-s", cls.SERVICE, "-w"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                raise CredentialError(f"Claude Code credentials not in Keychain: {r.stderr.strip()}")
            raw = r.stdout
        else:
            path = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / ".credentials.json"
            try:
                raw = path.read_text()
            except OSError as e:
                raise CredentialError(f"Claude Code credentials not found at {path}: {e}") from e
        oauth = json.loads(raw).get("claudeAiOauth") or {}
        if "accessToken" not in oauth:
            raise CredentialError("Claude Code credentials have no OAuth access token")
        return oauth["accessToken"], oauth.get("expiresAt", 0) / 1000


class BearerCredentials:
    def __init__(self, token: str):
        self._token = token

    async def apply(self, headers: dict[str, str]) -> None:
        headers["authorization"] = f"Bearer {self._token}"


class CodexChatGPTCredentials:
    """The Codex CLI's ChatGPT login, read from the host's ``auth.json``."""

    def __init__(self, codex_home: Path | None = None):
        self.path = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        self._mtime = None
        self._tokens: dict | None = None

    def claims(self) -> dict:
        """Non-secret account claims (plan, account id) for the sandbox's placeholder login."""
        tokens = self._load()
        auth = _jwt_claims(tokens.get("id_token", "")).get("https://api.openai.com/auth", {})
        return {
            "chatgpt_plan_type": auth.get("chatgpt_plan_type"),
            "chatgpt_account_id": tokens.get("account_id") or auth.get("chatgpt_account_id"),
        }

    async def apply(self, headers: dict[str, str]) -> None:
        tokens = self._load()
        exp = _jwt_claims(tokens["access_token"]).get("exp", 0)
        if exp and time.time() >= exp:
            self._mtime = None  # maybe the host CLI refreshed since; re-read next time
            raise CredentialError("Codex ChatGPT login has expired; run `codex` on the host to refresh it")
        headers["authorization"] = f"Bearer {tokens['access_token']}"
        if tokens.get("account_id"):
            headers["chatgpt-account-id"] = tokens["account_id"]

    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as e:
            raise CredentialError(f"Codex is not logged in ({self.path}): {e}") from e
        if self._tokens is None or mtime != self._mtime:
            data = json.loads(self.path.read_text())
            if data.get("auth_mode") not in (None, "chatgpt") or not data.get("tokens"):
                raise CredentialError(f"{self.path} is not a ChatGPT login")
            self._tokens, self._mtime = data["tokens"], mtime
        return self._tokens


def _jwt_claims(jwt: str) -> dict:
    import base64

    try:
        payload = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class CredentialError(RuntimeError):
    pass


def default_anthropic_credentials() -> Credentials:
    """API key if one is configured, else the host's Claude Code login."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    return ApiKeyCredentials(key) if key else ClaudeKeychainCredentials()


def default_openai_upstream() -> tuple[str, Credentials, tuple[str, ...]]:
    """(upstream, credentials, allowed path prefixes) for Codex's model calls:
    an OpenAI API key if configured, else the host's Codex ChatGPT login."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return OPENAI_API, BearerCredentials(key), ("/v1/",)
    # Model calls, plus the account check Codex does to route a ChatGPT workspace.
    return CHATGPT_BACKEND, CodexChatGPTCredentials(), ("/backend-api/codex/", "/backend-api/wham/accounts/check")


class ModelProxy:
    """Reverse proxy that adds credentials and forwards to one upstream API."""

    def __init__(
        self,
        socket_path: str | Path,
        credentials: Credentials,
        upstream: str = ANTHROPIC_API,
        allowed_prefixes: tuple[str, ...] = ("/v1/", "/api/hello"),
        ssl_context=None,
    ):
        """``ssl_context``: terminate TLS on the socket (for sandbox clients that
        must see the real ``https://`` hostname; see agentd.sandbox.tls)."""
        self.socket_path = Path(socket_path)
        self.credentials = credentials
        self.upstream = upstream.rstrip("/")
        self.allowed_prefixes = allowed_prefixes
        self.ssl_context = ssl_context
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> Path:
        # auto_decompress=False: pass compressed bodies through byte for byte.
        self._session = aiohttp.ClientSession(
            auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, sock_read=600)
        )
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        await web.UnixSite(self._runner, str(self.socket_path), ssl_context=self.ssl_context).start()
        os.chmod(self.socket_path, 0o600)
        return self.socket_path

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()
        self.socket_path.unlink(missing_ok=True)

    async def __aenter__(self) -> "ModelProxy":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = request.rel_url.path
        if request.headers.get("upgrade", "").lower() == "websocket":
            # Not proxied (yet); clients such as Codex fall back to HTTP streaming.
            return web.json_response({"error": "websocket transport not supported by agentd proxy"}, status=426)
        if not path.startswith(self.allowed_prefixes):
            logger.warning("model proxy: blocked %s %s", request.method, path)
            return web.json_response({"error": f"path {path} not allowed"}, status=403)
        headers = {k.lower(): v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST}
        try:
            await self.credentials.apply(headers)
        except CredentialError as e:
            return web.json_response(
                {"type": "error", "error": {"type": "authentication_error", "message": str(e)}},
                status=401,
            )
        body = await request.read()
        url = self.upstream + str(request.rel_url)
        logger.info("model proxy: %s %s", request.method, path)
        # aiohttp would add its own Accept-Encoding; with auto_decompress off that
        # hands clients compressed bodies they never asked for (Codex can't read them).
        async with self._session.request(
            request.method, url, headers=headers, data=body, skip_auto_headers=("Accept-Encoding",)
        ) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for k, v in upstream.headers.items():
                if k.lower() not in _STRIP_RESPONSE:
                    response.headers.add(k, v)
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
            except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                logger.debug("model proxy: client went away during %s %s", request.method, path)
            return response
