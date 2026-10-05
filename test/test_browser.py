"""
The browser tools (agentd.devices.browser). Without Chrome: TOTP, login and
site approvals, the gate's logic, the attempt cap. With AGENTD_LIVE=1, a real
headed Chrome against a local test site (localhost stands for a site with a
signed-in session, 127.0.0.1 for any other): the gate (pages, WebSockets),
filling logins by structure (the agent navigates and submits), two-step and
TOTP steps, refusals, the attempt cap, banners and focus theft, popups, tabs,
and a persistent base profile across sessions.
"""
import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from agentd import secrets
from agentd.devices import browser as br
from agentd.egress.approvals import Approvals

SEED = "JBSWY3DPEHPK3PXP"
LIVE = pytest.mark.skipif(not os.environ.get("AGENTD_LIVE") or br.find_chrome() is None or shutil.which("fnox") is None,
                          reason="set AGENTD_LIVE=1 (needs Chrome and fnox; opens a browser window)")


def _fnox(tmp_path):
    (tmp_path / "fnox.toml").write_text(
        '[providers.plain]\ntype = "plain"\n[secrets]\n'
        'SITE_USER = { provider = "plain", value = "alice@example.com" }\n'
        'SITE_PASS = { provider = "plain", value = "s3cret-pass-1234" }\n'
        f'SITE_TOTP = {{ provider = "plain", value = "{SEED}" }}\n')
    secrets.forget()


def _login(port, path="/login", **more):
    return {"url": f"http://localhost:{port}{path}", "hosts": ["localhost"], "username": "SITE_USER",
            "password": "SITE_PASS", "totp": "SITE_TOTP", **more}


def _ref(page, text):
    return next(e["ref"] for e in page["elements"] if e.get("text") == text)


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

    async def old_login(request):
        raise web.HTTPFound("/login-js")

    async def login_js(request):
        script = ("setTimeout(() => { document.body.innerHTML = `<form method='post' action='/login'>"
                  "<label>Email <input name='user' type='email'></label><label>Password "
                  "<input name='pw' type='password'></label><button>Sign in</button></form>`; }, 1500)")
        return web.Response(content_type="text/html", text=page.format(t="Sign in", b=f"Loading...<script>{script}</script>"))

    async def login_hostile(request):
        # A prefilled username, a OneTrust banner that covers the page after 0.8 s, and a chat
        # widget that grabs focus twice while the password is typed.
        body = ("<form method='post' action='/login'><label>Email <input name='user' type='email' "
                "value='old@example.com'></label><label>Password <input id='pw' name='pw' type='password'>"
                "</label><button>Sign in</button></form>"
                "<script>setTimeout(() => { const b = document.createElement('div'); b.id = 'onetrust-banner-sdk';"
                "b.style = 'position:fixed;inset:0;background:#fff;z-index:9';"
                "b.innerHTML = `<button id='onetrust-accept-btn-handler'>I understand</button>`;"
                "document.body.appendChild(b); b.querySelector('button').onclick = () => { fetch('/consent');"
                "b.remove(); }; }, 800);"
                "let steals = 0; document.getElementById('pw').addEventListener('input', () => {"
                "if (steals++ < 2) { const c = document.createElement('textarea'); document.body.appendChild(c);"
                "c.focus(); } });</script>")
        return web.Response(content_type="text/html", text=page.format(t="Sign in", b=body))

    async def consent(request):
        state["consent"] = state.get("consent", 0) + 1
        return web.Response(text="ok")

    async def login_short(request):
        return web.Response(content_type="text/html", text=page.format(t="Sign in", b=(
            "<form method='post' action='/login'><input name='pw' type='password' maxlength='4'>"
            "<button>Sign in</button></form>")))

    async def popup(request):
        return web.Response(content_type="text/html", text=page.format(t="Links", b=(
            "<a href='https://example.org/' target='_blank'>Open elsewhere</a>")))

    async def nothing(request):
        return web.Response(content_type="text/html", text=page.format(t="Empty", b="no form here"))

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

    async def signup(request):
        return web.Response(content_type="text/html", text=page.format(t="Sign up", b=(
            "<form method='post' action='/login'><input name='user' type='email'>"
            "<input name='pw' type='password'><input name='pw2' type='password'><button>Create</button></form>")))

    async def step1(request):
        return web.Response(content_type="text/html", text=page.format(t="Sign in", b=(
            "<form method='post' action='/step1'><input name='user' type='email' autocomplete='username'>"
            "<button>Next</button></form>")))

    async def step1_post(request):
        state["step_user"] = (await request.post()).get("user")
        raise web.HTTPFound("/step2")

    async def step2(request):
        return web.Response(content_type="text/html", text=page.format(t="Password", b=(
            "<form method='post' action='/step2'><input name='pw' type='password'><button>Sign in</button></form>")))

    async def step2_post(request):
        state["step_pw"] = (await request.post()).get("pw")
        raise web.HTTPFound("/home")

    async def whoami(request):
        return web.Response(content_type="text/html", text=page.format(
            t="Who", b=f"session={request.cookies.get('session', 'none')}"))

    async def ws_page(request):
        target = request.query["to"]
        return web.Response(content_type="text/html", text=page.format(t="WS", b=(
            f"<script>const w = new WebSocket('ws://{target}/ws'); w.onopen = () => fetch('/wsok');</script>")))

    async def ws(request):
        w = web.WebSocketResponse()
        await w.prepare(request)
        state["ws"] = state.get("ws", 0) + 1
        await asyncio.sleep(1)
        return w

    async def wsok(request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/login", login_form)
    app.router.add_get("/old-login", old_login)
    app.router.add_get("/login-js", login_js)
    app.router.add_get("/nothing", nothing)
    app.router.add_get("/popup", popup)
    app.router.add_get("/login-hostile", login_hostile)
    app.router.add_get("/consent", consent)
    app.router.add_get("/login-short", login_short)
    app.router.add_post("/login", login)
    app.router.add_get("/2fa", twofa_form)
    app.router.add_post("/2fa", twofa)
    app.router.add_get("/home", home)
    app.router.add_post("/click", click)
    app.router.add_get("/signup", signup)
    app.router.add_get("/step1", step1)
    app.router.add_post("/step1", step1_post)
    app.router.add_get("/step2", step2)
    app.router.add_post("/step2", step2_post)
    app.router.add_get("/whoami", whoami)
    app.router.add_get("/ws-page", ws_page)
    app.router.add_get("/ws", ws)
    app.router.add_get("/wsok", wsok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


@LIVE
def test_live_login_flow_gate_tabs_and_popups(tmp_path):
    _fnox(tmp_path)

    async def main():
        state = {}
        runner, port = await _site(state)
        approvals = Approvals(allow_file=tmp_path / "allow.toml", hold=0.2)
        b = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, approvals=approvals,
                       logins={"local": _login(port)})
        outputs = []
        try:
            # localhost holds a signed-in session: closed until this session gets a grant.
            page = await b.open(f"http://localhost:{port}/login")
            assert "request_site('localhost'" in page["error"] and "request_login" in page["error"], page
            ok = await b.open(f"http://127.0.0.1:{port}/home")
            assert ok["title"] == "Home", "other sites are open"
            with pytest.raises(br.BrowserAccessError, match="request_login"):
                await b.fill_login("local")
            approvals.decide(b.request_login("local", "check the clicks")["id"], "once")
            page = await b.open(f"http://localhost:{port}/login")  # the login approval opens its site
            outputs.append(page)
            assert page["title"] == "Sign in"

            r = await b.fill_login("local")
            outputs.append(r)
            assert r["filled"] == ["username", "password"] and "login" not in state, "filled, not submitted"
            page = await b.click(_ref(r, "Sign in"))
            outputs.append(page)
            assert state["login"] == ("alice@example.com", "s3cret-pass-1234") and page["title"] == "Code"
            r = await b.fill_login("local")  # the TOTP step, right after the password
            outputs.append(r)
            assert r["filled"] == ["totp"]
            page = await b.click(_ref(r, "Verify"))
            outputs.append(page)
            assert page["title"] == "Home" and state["code"] in (br.totp(SEED), br.totp(SEED, at=time.time() - 30))
            assert state["code"] in secrets.known_values(), "the typed code is scrubbed from what the agent sees"
            flat = json.dumps(outputs)
            assert "s3cret-pass-1234" not in flat and SEED not in flat and "alice@example.com" not in flat
            await b.open(f"http://localhost:{port}/login")
            with pytest.raises(br.BrowserAccessError, match="request_login"):
                await b.fill_login("local")  # "once" is spent

            # Tabs.
            await b.new_tab(f"http://127.0.0.1:{port}/popup")
            tabs = await b.tabs()
            assert len(tabs) == 2 and [t["title"] for t in tabs if t["current"]] == ["Links"]
            # A link that opens yet another tab is policed like the page (here: a host off the allowlist).
            b.allow = ["127.0.0.1", "localhost"]
            await b.click(_ref(await b.snapshot(), "Open elsewhere"))
            await asyncio.sleep(2)
            infos = (await b._pipe.send("Target.getTargets"))["targetInfos"]
            popup = next(t for t in infos if t["type"] == "page" and "example.org" in t["url"])
            sid = (await b._pipe.send("Target.attachToTarget", {"targetId": popup["targetId"], "flatten": True}))["sessionId"]
            frame = (await b._pipe.send("Page.getFrameTree", {}, sid))["frameTree"]["frame"]
            assert frame.get("unreachableUrl") == "https://example.org/", frame
            assert len(await b.tabs()) == 3, "the popup is a tab too"
            first = next(t["id"] for t in await b.tabs() if t["title"] == "Sign in")
            page = await b.switch_tab(first)
            assert page["title"] == "Sign in"
            await b.close_tab(next(t["id"] for t in await b.tabs() if t["title"] == "Links"))
            assert [t["title"] for t in await b.tabs() if t["current"]] == ["Sign in"] and len(await b.tabs()) == 2
            profile = b._profile
            await b.close()
            assert profile is not None and not profile.exists(), "a throwaway profile is deleted"
        finally:
            await b.close()
            await runner.cleanup()
    asyncio.run(main())


@LIVE
def test_live_fill_picks_fields_by_structure(tmp_path):
    _fnox(tmp_path)

    async def main():
        state = {}
        runner, port = await _site(state)
        b = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, logins_allowed=True,
                       logins={"local": _login(port), "short": _login(port, "/login-short")})
        try:
            await b.open(f"http://localhost:{port}/signup")
            with pytest.raises(br.BrowserAccessError, match="several password boxes"):
                await b.fill_login("local")  # a sign-up form: refused
            await b.open(f"http://localhost:{port}/2fa")
            with pytest.raises(br.BrowserAccessError, match="no password was filled"):
                await b.fill_login("local")  # a lone code box with no login step before it
            await b.open(f"http://127.0.0.1:{port}/login")
            with pytest.raises(br.BrowserAccessError, match="login hosts"):
                await b.fill_login("local")  # same form, another site: credentials aren't typed there
            # Two steps: the username alone (explicitly marked), then the password alone.
            await b.open(f"http://localhost:{port}/step1")
            r = await b.fill_login("local")
            assert r["filled"] == ["username"]
            page = await b.click(_ref(r, "Next"))
            assert state["step_user"] == "alice@example.com" and page["title"] == "Password"
            r = await b.fill_login("local")
            assert r["filled"] == ["password"]
            await b.click(_ref(r, "Sign in"))
            assert state["step_pw"] == "s3cret-pass-1234"
            # A box that can't take the whole password: the fill fails before anything is submitted.
            await b.open(f"http://localhost:{port}/login-short")
            with pytest.raises(br.BrowserAccessError, match="took focus"):
                await b.fill_login("short")
        finally:
            await b.close()
            await runner.cleanup()
    asyncio.run(main())


@LIVE
def test_live_banners_focus_theft_and_late_forms(tmp_path):
    _fnox(tmp_path)

    async def main():
        state = {}
        runner, port = await _site(state)
        b = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, logins_allowed=True,
                       logins={"local": _login(port)})
        try:
            await b.open(f"http://localhost:{port}/login-hostile")
            r = await b.fill_login("local")
            await b.click(_ref(r, "Sign in"))
            assert state.get("consent") == 1, "the consent banner was accepted"
            assert state["login"] == ("alice@example.com", "s3cret-pass-1234"), \
                "the prefilled username was replaced and no keystroke went to the chat widget"
            await b.open(f"http://localhost:{port}/old-login")  # redirects; the form appears 1.5 s later
            r = await b.fill_login("local")
            assert r["filled"] == ["username", "password"]
        finally:
            await b.close()
            await runner.cleanup()
    asyncio.run(main())


@LIVE
def test_live_gate_blocks_websockets(tmp_path):
    async def main():
        state = {}
        runner, port = await _site(state)
        approvals = Approvals(allow_file=tmp_path / "allow.toml", hold=0.2)
        b = br.Browser(workspace=tmp_path, allowed=True, approvals=approvals, gated=["localhost"])
        try:
            await b.open(f"http://127.0.0.1:{port}/ws-page?to=localhost:{port}")
            await asyncio.sleep(1.5)
            assert state.get("ws", 0) == 0, "a page elsewhere can't reach the gated site, WebSockets included"
            await b.open(f"http://127.0.0.1:{port}/ws-page?to=127.0.0.1:{port}")
            await asyncio.sleep(1.5)
            assert state.get("ws") == 1, "other WebSockets work"
            r = b.request_site("localhost", "read my dashboard")
            approvals.decide(r["id"], "session")
            await b.open(f"http://127.0.0.1:{port}/ws-page?to=localhost:{port}")
            await asyncio.sleep(1.5)
            assert state["ws"] == 2, "granted for the session"
        finally:
            await b.close()
            await runner.cleanup()
    asyncio.run(main())


@LIVE
def test_live_attempt_cap(tmp_path, monkeypatch):
    _fnox(tmp_path)
    (tmp_path / "fnox.toml").write_text((tmp_path / "fnox.toml").read_text().replace("s3cret-pass-1234", "wrong"))
    secrets.forget()
    monkeypatch.setattr(br, "LOGIN_FORM_WAIT", 2.0)

    async def main():
        state = {}
        runner, port = await _site(state)
        approvals = Approvals(allow_file=tmp_path / "allow.toml", hold=0.2)
        b = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, logins_allowed=True, approvals=approvals,
                       logins={"local": _login(port, totp=None)})
        try:
            for _ in range(2):  # wrong password: back on a page with a password box
                await b.open(f"http://localhost:{port}/login")
                r = await b.fill_login("local")
                await b.click(_ref(r, "Sign in"))
                await b.open(f"http://localhost:{port}/login")
            with pytest.raises(br.BrowserAccessError, match="approves a retry") as e:
                await b.fill_login("local")
            retry = next(a for a in approvals.items.values() if a.kind == "browser_login_retry")
            assert len(retry.details["attempts"]) == 2 and str(retry.id) in str(e.value)
            other = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, logins_allowed=True,
                               approvals=approvals, logins={"local": _login(port, totp=None)})
            with pytest.raises(br.BrowserAccessError, match="approves a retry"):
                other._check_attempts("local")  # counted across agents
            approvals.decide(retry.id, "once")
            r = await b.fill_login("local")
            assert r["filled"] == ["username", "password"]
        finally:
            await b.close()
            await runner.cleanup()
    asyncio.run(main())


@LIVE
def test_live_base_profile_keeps_sessions(tmp_path):
    from agentd.devices.browser_profile import BaseProfile

    _fnox(tmp_path)

    async def main():
        state = {}
        runner, port = await _site(state)
        profile = BaseProfile(root=tmp_path / "browser")
        logins = {"local": _login(port)}
        try:
            for _ in range(2):  # sign in once; the next session is still signed in
                b = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, logins_allowed=True,
                               profile=profile, logins=logins)
                page = await b.open(f"http://localhost:{port}/whoami")
                if "session=abc" not in page["text"]:
                    await b.open(f"http://localhost:{port}/login")
                    await b.click(_ref(await b.fill_login("local"), "Sign in"))
                    await b.click(_ref(await b.fill_login("local"), "Verify"))
                    assert "session=abc" in (await b.open(f"http://localhost:{port}/whoami"))["text"]
                    first_clone = b._profile
                else:
                    state["kept"] = True
                await b.close()
            assert state.get("kept") and not first_clone.exists(), "kept in the base; the clone is gone"
            # A sensitive login is signed out (its data cleared) when the session ends.
            logins["local"]["tier"] = "sensitive"
            b = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, logins_allowed=True,
                           profile=profile, logins=logins)
            assert "session=abc" in (await b.open(f"http://localhost:{port}/whoami"))["text"]
            await b.close()
            b = br.Browser(workspace=tmp_path, allowed=True, fnox_cwd=tmp_path, logins_allowed=True,
                           profile=profile, logins=logins)
            assert "session=none" in (await b.open(f"http://localhost:{port}/whoami"))["text"]
            await b.close()
        finally:
            await runner.cleanup()
    asyncio.run(main())


@pytest.fixture
def unlocked(monkeypatch):
    """fnox has every login secret (locked vaults: test_unlock_* below)."""
    monkeypatch.setattr(br.Browser, "_uncached", lambda self, names: [])


def test_logins_need_approval_per_site(tmp_path, unlocked):
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
        "site": "gh", "host": "github.com", "hosts": ["github.com", "*.github.com"],
        "secrets": ["GH_USER", "GH_PASS", "GH_TOTP"]}
    with pytest.raises(br.BrowserAccessError):
        b._use_login("gh")  # still pending
    approvals.decide(req["id"], "once")
    b._use_login("gh", spend=False)
    b._use_login("gh", spend=False)  # checked, not spent (e.g. no form on the page yet)
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


def test_request_tools_wait_for_the_answer(tmp_path, unlocked):
    from agentd.egress.approvals import Approvals

    approvals = Approvals(hold=5, allow_file=tmp_path / "allow.toml")
    b = br.Browser(chrome="/bin/true", approvals=approvals,
                   logins={"gh": {"url": "https://github.com/login", "password": "P"}})
    br.enable_browser_skills(b)
    from agentd.tool_decorator import FUNCTION_REGISTRY, SCHEMA_REGISTRY

    async def main():
        async def approve_soon():
            await asyncio.sleep(0.3)
            approvals.decide(next(iter(approvals.items)), "session")
        t0 = time.monotonic()
        r, _ = await asyncio.gather(FUNCTION_REGISTRY["request_login"](site="gh", reason="x"), approve_soon())
        assert r["status"] == "session" and time.monotonic() - t0 < 2, "returned as soon as it was decided"
        approvals.hold = 0.3
        r = await FUNCTION_REGISTRY["request_browser"](minutes=5, reason="y")
        assert r["status"] == "pending", "undecided: back after the hold"
    try:
        assert "waits up to 5 seconds" in SCHEMA_REGISTRY["request_login"]["function"]["description"]
        asyncio.run(main())
    finally:
        for f in br.TOOLS:
            FUNCTION_REGISTRY.pop(f.__name__, None)
            SCHEMA_REGISTRY.pop(f.__name__, None)


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


def test_gate_logic(tmp_path, unlocked):
    approvals = Approvals(allow_file=tmp_path / "allow.toml")
    b = br.Browser(chrome="/bin/true", approvals=approvals, gated=["examplemail.com"],
                   logins={"bank": {"url": "https://www.examplebank.com/login", "password": "P"},
                           "sso": {"url": "https://app.example.com/", "hosts": ["app.example.com", "login.example-idp.com"],
                                   "password": "Q"}})
    assert b._gated_sites() == {"examplebank.com", "examplemail.com", "example.com", "example-idp.com"}
    assert not b._proxy_allows("api.examplebank.com"), "the whole registrable domain, not just the login's hosts"
    assert b._proxy_allows("news.example.org")
    assert "wss://*.examplebank.com/*" in b._blocked_urls()
    approvals.decide(b.request_site("www.examplebank.com", "pay a bill")["id"], "session")
    assert b._proxy_allows("api.examplebank.com") and "wss://*.examplebank.com/*" not in b._blocked_urls()
    approvals.decide(b.request_login("sso", "x")["id"], "once")
    assert b._proxy_allows("login.example-idp.com"), "a login approval opens its sites, identity provider included"
    approvals.decide(b.request_site("examplemail.com", "y")["id"], "always")
    fresh = br.Browser(chrome="/bin/true", approvals=Approvals(allow_file=tmp_path / "allow.toml"),
                       gated=["examplemail.com"])
    assert fresh._proxy_allows("examplemail.com") and not br.Browser(chrome="/bin/true", gated=["examplemail.com"],
                                                                  )._proxy_allows("x.examplemail.com")


def test_attempt_cap_bookkeeping(tmp_path):
    approvals = Approvals(allow_file=tmp_path / "allow.toml")
    b = br.Browser(chrome="/bin/true", approvals=approvals, logins={"bank": {"url": "https://bank.example/",
                                                                             "password": "P"}})
    b._check_attempts("bank")
    with b._attempts() as data:
        data["bank"] = [{"id": "a", "at": time.time(), "failed": True},
                        {"id": "b", "at": time.time(), "failed": None},      # unjudged counts against it
                        {"id": "c", "at": time.time(), "failed": False},     # a success doesn't
                        {"id": "d", "at": time.time() - 90000, "failed": True}]  # older than 24 h
    with pytest.raises(br.BrowserAccessError, match="approves a retry"):
        b._check_attempts("bank")
    with pytest.raises(br.BrowserAccessError):
        b._check_attempts("bank")
    assert len([a for a in approvals.items.values() if a.kind == "browser_login_retry"]) == 1, "asked once"
    approvals.decide(next(iter(approvals.items)), "once")
    b._check_attempts("bank")
    b._check_attempts("bank")
    with b._fill_lock("bank"):
        with pytest.raises(br.BrowserAccessError, match="another agent"):
            with b._fill_lock("bank"):
                pass
