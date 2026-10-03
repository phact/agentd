"""
The browser tools (agentd.devices.browser): TOTP, and with AGENTD_LIVE=1 a
real headed Chrome against a local test site: snapshot, click, a login with
two factors filled from fnox (the agent never sees them), the allowlist, and
the session wiped on close.
"""
import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest

from agentd import secrets
from agentd.devices import browser as br

SEED = "JBSWY3DPEHPK3PXP"


def test_totp_matches_rfc6238():
    # RFC 6238 test vector (SHA-1, 8 digits) for the ASCII seed "12345678901234567890"
    import base64

    seed = base64.b32encode(b"12345678901234567890").decode()
    assert br.totp(seed, at=59, digits=8) == "94287082"
    assert br.totp(seed, at=1111111109, digits=8) == "07081804"
    assert len(br.totp(SEED)) == 6


async def _site(state):
    from aiohttp import web

    page = "<html><head><title>{t}</title></head><body>{b}</body></html>"

    async def login_form(request):
        return web.Response(content_type="text/html", text=page.format(t="Sign in", b=(
            '<form method="post" action="/login"><label>Email <input name="user" type="email"></label>'
            '<label>Password <input name="pw" type="password"></label><button>Sign in</button></form>')))

    async def login(request):
        form = await request.post()
        state["login"] = (form.get("user"), form.get("pw"))
        if (form.get("user"), form.get("pw")) != ("alice@example.com", "s3cret-pass-1234"):
            return web.Response(content_type="text/html", text=page.format(t="Nope", b="wrong password"))
        raise web.HTTPFound("/2fa")

    async def twofa_form(request):
        return web.Response(content_type="text/html", text=page.format(t="Code", b=(
            '<form method="post" action="/2fa"><input name="code" type="text" placeholder="6-digit code">'
            '<button>Verify</button></form>')))

    async def twofa(request):
        form = await request.post()
        state["code"] = form.get("code")
        if form.get("code") not in (br.totp(SEED), br.totp(SEED, at=time.time() - 30)):
            return web.Response(content_type="text/html", text=page.format(t="Nope", b="bad code"))
        resp = web.HTTPFound("/home")
        resp.set_cookie("session", "abc")
        raise resp

    async def home(request):
        return web.Response(content_type="text/html", text=page.format(t="Home", b=(
            f'<h1>Welcome alice</h1><p id="c">clicks: {state.get("clicks", 0)}</p>'
            '<form method="post" action="/click"><button>Click me</button></form>')))

    async def click(request):
        state["clicks"] = state.get("clicks", 0) + 1
        raise web.HTTPFound("/home")

    app = web.Application()
    app.router.add_get("/login", login_form)
    app.router.add_post("/login", login)
    app.router.add_get("/2fa", twofa_form)
    app.router.add_post("/2fa", twofa)
    app.router.add_get("/home", home)
    app.router.add_post("/click", click)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


@pytest.mark.skipif(not os.environ.get("AGENTD_LIVE") or br.find_chrome() is None or shutil.which("fnox") is None,
                    reason="set AGENTD_LIVE=1 (needs Chrome and fnox; opens a browser window)")
def test_live_browser_login_click_and_wipe(tmp_path):
    (tmp_path / "fnox.toml").write_text(
        '[providers.plain]\ntype = "plain"\n[secrets]\n'
        'SITE_USER = { provider = "plain", value = "alice@example.com" }\n'
        'SITE_PASS = { provider = "plain", value = "s3cret-pass-1234" }\n'
        f'SITE_TOTP = {{ provider = "plain", value = "{SEED}" }}\n')
    secrets.forget()

    async def main():
        state = {}
        runner, port = await _site(state)
        from agentd.egress.approvals import Approvals

        approvals = Approvals(allow_file=tmp_path / "allow.toml")
        b = br.Browser(workspace=tmp_path, allowed=True, allow=["127.0.0.1"], fnox_cwd=tmp_path, approvals=approvals,
                       logins={"local": {"url": f"http://127.0.0.1:{port}/login", "username": "SITE_USER",
                                         "password": "SITE_PASS", "totp": "SITE_TOTP"},
                               "elsewhere": {"hosts": ["example.org"], "password": "SITE_PASS"}})
        outputs = []
        try:
            page = await b.open(f"http://127.0.0.1:{port}/login")
            outputs.append(page)
            assert page["title"] == "Sign in" and any(e.get("password") for e in page["elements"])
            with pytest.raises(br.BrowserAccessError, match="request_login"):
                await b.login("local")  # the lease alone gives no credentials
            approvals.decide(b.request_login("elsewhere", "x")["id"], "session")
            with pytest.raises(br.BrowserAccessError, match="login hosts"):
                await b.login("elsewhere")  # approved, but this page isn't on its hosts
            assert "login" not in state, "nothing was typed"
            approvals.decide(b.request_login("local", "sign in to check the clicks")["id"], "once")
            page = await b.login("local")
            outputs.append(page)
            assert state["login"] == ("alice@example.com", "s3cret-pass-1234"), "credentials filled from fnox"
            assert page["title"] == "Home" and "Welcome alice" in page["text"], page
            with pytest.raises(br.BrowserAccessError, match="request_login"):
                await b.login("local")  # "once" is spent
            button = next(e["ref"] for e in page["elements"] if e.get("text") == "Click me")
            page = await b.click(button)
            outputs.append(page)
            assert state["clicks"] == 1 and "clicks: 1" in page["text"]
            shot = await b.screenshot()
            assert Path(shot["path"]).stat().st_size > 1000
            blocked = await b.open("https://example.com/")
            outputs.append(blocked)
            assert "BLOCKED" in (blocked.get("error") or "") or blocked.get("url", "").startswith("chrome-error"), blocked
            profile = b._profile
            await b.close()
            assert profile is not None and not profile.exists(), "the session (cookies) is wiped"
            flat = repr(outputs)
            assert "s3cret-pass-1234" not in flat and SEED not in flat, "the agent never sees the credentials"
        finally:
            await b.close()
            await runner.cleanup()

    asyncio.run(main())


def test_logins_need_approval_per_site(tmp_path):
    from agentd.egress.approvals import Approvals

    approvals = Approvals(allow_file=tmp_path / "allow.toml")
    logins = {"gh": {"url": "https://github.com/login", "username": "GH_USER", "password": "GH_PASS",
                     "totp": "GH_TOTP"},
              "bank": {"hosts": ["*.bank.example"], "password": "BANK_PASS"},
              "bad": {"password": "X"}}
    b = br.Browser(chrome="/bin/true", allowed=True, approvals=approvals, logins=logins)

    with pytest.raises(LookupError):
        b.request_login("nope", "x")
    with pytest.raises(ValueError, match="url or hosts"):
        b.request_login("bad", "x")
    with pytest.raises(br.BrowserAccessError, match="request_login"):
        b._use_login("gh")

    req = b.request_login("gh", "open a PR")
    a = approvals.items[req["id"]]
    assert a.kind == "browser_login" and a.details == {
        "site": "gh", "host": "github.com", "hosts": ["github.com"], "secrets": ["GH_USER", "GH_PASS", "GH_TOTP"]}
    with pytest.raises(br.BrowserAccessError):
        b._use_login("gh")  # still pending
    approvals.decide(req["id"], "once")
    b._use_login("gh")
    with pytest.raises(br.BrowserAccessError, match="request_login"):
        b._use_login("gh")  # spent
    with pytest.raises(br.BrowserAccessError):
        b._use_login("bank")  # gh's approval says nothing about bank

    approvals.decide(b.request_login("bank", "pay a bill")["id"], "session")
    b._use_login("bank")
    b._use_login("bank")
    asyncio.run(b.close())
    with pytest.raises(br.BrowserAccessError):
        b._use_login("bank")  # session approvals end with the browser session

    approvals.decide(b.request_login("bank", "again")["id"], "deny")
    with pytest.raises(br.BrowserAccessError, match="denied"):
        b._use_login("bank")

    approvals.decide(b.request_login("gh", "every day")["id"], "always")
    assert approvals.saved_logins() == ["gh@github.com"]
    fresh = br.Browser(chrome="/bin/true", allowed=True, approvals=Approvals(allow_file=tmp_path / "allow.toml"),
                       logins=logins)
    fresh._use_login("gh")  # remembered
    assert b.request_login("gh", "x") == {"status": "allowed"}

    pre = br.Browser(chrome="/bin/true", logins=logins, logins_allowed={"bank"})
    pre._use_login("bank")
    with pytest.raises(br.BrowserAccessError):
        pre._use_login("gh")


def test_lease_needs_approval(tmp_path):
    if br.find_chrome() is None:
        pytest.skip("needs Chrome")
    from agentd.egress.approvals import Approvals

    approvals = Approvals(allow_file=tmp_path / "allow.toml")
    b = br.Browser(workspace=tmp_path, approvals=approvals)

    async def main():
        with pytest.raises(br.BrowserAccessError, match="request_browser"):
            await b.snapshot()
        req = b.request(5, "look something up")
        approvals.decide(req["id"], "session")
        await b._check()
        assert 290 < b.lease_until - time.time() <= 300
    asyncio.run(main())
