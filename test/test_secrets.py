"""
Host-side secrets (agentd.secrets): values from fnox for @tools and MCP
servers, and the bridge scrubbing them out of everything a sandbox receives.
"""
import asyncio
import shutil
import tempfile
from pathlib import Path

import aiohttp
import pytest

from agentd import secrets
from agentd.mcp_bridge import MCPBridge

pytestmark = pytest.mark.skipif(shutil.which("fnox") is None, reason="needs fnox")

VALUE = "pg://agent:hunter2-very-secret@db.internal/app"


@pytest.fixture
def fnox_dir(tmp_path):
    secrets.forget()
    (tmp_path / "fnox.toml").write_text('[providers.plain]\ntype = "plain"\n[secrets]\n'
                                        f'DATABASE_URL = {{ provider = "plain", value = "{VALUE}" }}\n'
                                        'API_KEY = { provider = "plain", value = "key-123456" }\n')
    yield tmp_path
    secrets.forget()


def test_secret_and_env(fnox_dir):
    assert secrets.secret("DATABASE_URL", cwd=fnox_dir) == VALUE
    assert secrets.secret_env(["API_KEY"], cwd=fnox_dir) == {"API_KEY": "key-123456"}
    with pytest.raises(RuntimeError):
        secrets.secret("NOPE", cwd=fnox_dir)


def test_scrub_nested(fnox_dir):
    secrets.secret("DATABASE_URL", cwd=fnox_dir)
    out = secrets.scrub({"rows": [f"connected to {VALUE}", ("x", VALUE)], VALUE: 1})
    assert VALUE not in repr(out) and "[secret redacted by agentd]" in out["rows"][0]
    assert secrets.scrub("nothing here") == "nothing here"


def test_bridge_results_are_scrubbed(fnox_dir):
    async def main():
        sock = Path(tempfile.mkdtemp(dir="/tmp", prefix="sb-")) / "bridge.sock"
        bridge = MCPBridge(socket_path=str(sock))

        def leaky(sql: str) -> dict:
            url = secrets.secret("DATABASE_URL", cwd=fnox_dir)
            return {"rows": [1, 2], "debug": f"ran {sql!r} on {url}"}

        def broken() -> None:
            raise RuntimeError(f"cannot connect to {secrets.secret('DATABASE_URL', cwd=fnox_dir)}")

        bridge.register_local_tool("leaky", leaky)
        bridge.register_local_tool("broken", broken)
        await bridge.start()
        try:
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(sock))) as http:
                async with http.post("http://bridge/call/leaky", json={"sql": "select 1"}) as r:
                    body = await r.json()
                assert body["rows"] == [1, 2] and VALUE not in body["debug"] and "redacted" in body["debug"]
                async with http.post("http://bridge/call/broken", json={}) as r:
                    text = await r.text()
                assert r.status == 500 and VALUE not in text
        finally:
            await bridge.stop()

    asyncio.run(main())
