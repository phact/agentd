"""
agentd.available(): which harnesses are ready and with which models, with
the model APIs faked (shapes from the real Anthropic, Codex/ChatGPT and
OpenAI-compatible lists); the agentd serve endpoints on top; and, with
AGENTD_LIVE=1, the real thing against this machine's credentials.
"""
import asyncio
import os

import pytest

from agentd import availability as av
from agentd.model_proxy import ModelUpstream

ANTHROPIC = {"data": [{"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5"},
                      {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"}]}
CODEX = {"models": [{"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "priority": 2},
                    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "priority": 1},
                    {"slug": "gpt-reserve", "visibility": "hide", "priority": 0}]}
SABIK = {"data": [{"id": "Qwen/Qwen3.8-27B"}, {"id": "whisper-large-v3-turbo-diarize"}, {"id": "hexgrad/Kokoro-82M"}]}
OPENAI = {"data": [{"id": "gpt-5.5", "created": 2}, {"id": "text-embedding-3-large", "created": 3},
                   {"id": "gpt-4o-realtime", "created": 4}, {"id": "o4-mini", "created": 1}]}


@pytest.fixture
def fake(monkeypatch):
    calls = []
    answers = {}

    async def get_json(url, credentials, headers=None):
        calls.append(url)
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                return answer
        return 404, {"error": {"message": "nope"}}

    monkeypatch.setattr(av, "_get_json", get_json)
    monkeypatch.setattr(av, "_clis_in_image", lambda *a: {"claude", "codex", "opencode", "omp"})
    monkeypatch.setattr(av, "_CACHE", {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class Creds:
        async def apply(self, headers):
            pass
    monkeypatch.setattr(av, "ClaudeCodeCredentials", Creds)
    monkeypatch.setattr(av, "CodexChatGPTCredentials", Creds)
    answers.update({
        "https://api.anthropic.com/v1/models": (200, ANTHROPIC),
        "https://chatgpt.com/backend-api/codex/models": (200, CODEX),
        "http://sabik:8001/v1/models": (200, SABIK),
        "https://api.openai.com/v1/models": (200, OPENAI),
    })
    return answers, calls


def _run(**kw):
    return asyncio.run(av.available_async(**kw))


def test_logins_only(fake):
    st = _run()
    assert st["claude-code"].ready and [m.id for m in st["claude-code"].models] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert st["claude-code"].default_model is None, "Claude Code picks its own default"
    codex = st["codex"]
    assert codex.ready and [m.id for m in codex.models] == ["gpt-6.1-sol", "gpt-6-astra"], "listed, by priority"
    assert codex.default_model == "gpt-6.1-sol" and codex.models[0].source == "chatgpt"
    assert st["omp"].ready and st["omp"].models[0].id == "claude-sonnet-5-5", "omp reaches Claude with the login"
    assert any("model=" in r for r in st["omp"].reasons), "no default model without an upstream"
    assert not st["opencode"].ready and any("ANTHROPIC_API_KEY" in r for r in st["opencode"].reasons)
    assert st["ptc"].ready


def test_upstreams_keys_and_filtering(fake, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    sabik = ModelUpstream("http://sabik:8001/v1", api="chat", name="sabik")
    st = _run(harness_options={"opencode": {"upstream": sabik}, "codex": {"upstream": {"base_url": "http://sabik:8001/v1"}},
                               "omp": {"upstream": sabik, "model": "Qwen/Qwen3.8-27B"}})
    oc = st["opencode"]
    assert oc.ready and oc.default_model == "Qwen/Qwen3.8-27B"
    assert [m.id for m in oc.models] == ["Qwen/Qwen3.8-27B", "claude-sonnet-5-5", "claude-opus-5-5", "gpt-5.5", "o4-mini"], \
        "speech/embedding models filtered; Claude via the API key; OpenAI chat models newest first"
    assert st["codex"].models == [av.ModelInfo("Qwen/Qwen3.8-27B", None, "upstream:upstream")]
    assert st["omp"].default_model == "Qwen/Qwen3.8-27B"
    assert st["codex"].default_model == "Qwen/Qwen3.8-27B"


def test_not_ready_reasons(fake, monkeypatch):
    answers, _ = fake
    answers["https://api.anthropic.com/v1/models"] = (401, {"error": {"message": "OAuth token has expired"}})
    monkeypatch.setattr(av, "_clis_in_image", lambda *a: {"claude"})
    st = _run(harness_options={"omp": {"upstream": ModelUpstream("http://down:1/v1", name="down")}})
    cc = st["claude-code"]
    assert not cc.ready and any("refused the Claude login" in r and "expired" in r for r in cc.reasons)
    assert not st["codex"].ready and any("`codex` is not in the sandbox image" in r for r in st["codex"].reasons)
    omp = st["omp"]
    assert not omp.ready and any("upstream down" in r for r in omp.reasons)


def test_cli_check_native_rootfs(tmp_path, monkeypatch):
    monkeypatch.setattr(av, "_CACHE", {})
    bin_dir = tmp_path / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "codex").write_text("")
    os.symlink("/home/agent/.local/bin/claude", bin_dir / "claude")  # dangling on the host, fine in the sandbox
    assert av._clis_in_image("krun", str(tmp_path), None, False) == {"claude", "codex"}
    assert "no base image" in av._clis_in_image("krun", str(tmp_path / "missing"), None, False)


def test_caching(fake):
    _, calls = fake
    _run()
    n = len(calls)
    _run()
    assert len(calls) == n, "lists are cached"
    _run(refresh=True)
    assert len(calls) == 2 * n


def test_serve_endpoints(monkeypatch):
    from test.test_serve import Client, FakePool, _short_tmp
    from agentd.serve import Server, ServeConfig

    async def fake_available(target, harness_options=None, refresh=False):
        return {
            "ptc": av.HarnessStatus("ptc", True),
            "claude-code": av.HarnessStatus("claude-code", True, models=[av.ModelInfo("claude-sonnet-5-5", "S", "anthropic")]),
            "codex": av.HarnessStatus("codex", False, ["`codex` is not in the sandbox image"],
                                      models=[av.ModelInfo("gpt-6.1-sol", None, "chatgpt")]),
            "opencode": av.HarnessStatus("opencode", True, models=[av.ModelInfo("Qwen/Q", None, "upstream:sabik")]),
            "omp": av.HarnessStatus("omp", True, models=[av.ModelInfo("Qwen/Q", None, "upstream:sabik"),
                                                        av.ModelInfo("claude-sonnet-5-5", "S", "anthropic")]),
        }
    monkeypatch.setattr(av, "available_async", fake_available)
    root = _short_tmp()

    async def main():
        cfg = ServeConfig(dir=root / "s", workspace_roots=[root / "ws"])
        server = Server(cfg, pool=FakePool(cfg))
        await server.start()
        try:
            peer = Client(cfg.peers_socket, "peer-1")
            _, h = await peer.call("GET", "/v1/harnesses")
            assert [x["name"] for x in h["data"]] == ["claude-code", "codex", "opencode", "omp"]
            assert h["data"][1]["ready"] is False and h["data"][1]["reasons"]
            _, models = await peer.call("GET", "/v1/models")
            by_id = {m["id"]: m for m in models["data"]}
            assert by_id["claude-sonnet-5-5"]["harnesses"] == ["claude-code", "omp"]
            assert by_id["Qwen/Q"]["harnesses"] == ["opencode", "omp"] and by_id["Qwen/Q"]["object"] == "model"
            assert "gpt-6.1-sol" not in by_id, "models of harnesses that aren't ready are left out"
            _, info = await peer.call("GET", "/v1/info")
            assert info["ready"] == {"claude-code": True, "codex": False, "opencode": True, "omp": True}
        finally:
            await server.stop()
    asyncio.run(main())


@pytest.mark.skipif(not os.environ.get("AGENTD_LIVE"), reason="set AGENTD_LIVE=1 for live checks")
def test_live_available():
    from agentd.sandbox.executor import default_executor

    with default_executor() as ex:
        st = av.available(ex, refresh=True)
    assert set(st) == {"ptc", "claude-code", "codex", "opencode", "omp"}
    for s in st.values():
        assert s.ready or s.reasons, f"{s.name}: not ready must say why"
        if s.ready:
            assert s.models, f"{s.name} is ready, so it has models"
