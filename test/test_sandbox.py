"""
Integration tests for agentd sandboxes, run against every available backend.

  krun:   the signed launcher (agentd/sandbox/build.sh) and base images under
          ~/.agentd/rootfs (python-3.11-slim for sandbox tests, agents for
          executor tests), built with `python -m agentd.sandbox.rootfs`.
  docker: a working `docker` with the python:3.11-slim and
          agentd-sandbox-agents images (the latter built by the same
          rootfs builder).

A backend that is not set up is skipped. Workspaces live under
~/.agentd/tmp because Docker Desktop / Colima only share $HOME with their VM.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agentd.sandbox.base import DEFAULT_HOME, Endpoint
from agentd.sandbox.docker import DockerSandbox
from agentd.sandbox.krun import DEFAULT_LAUNCHER, KrunSandbox

PY_ROOTFS = DEFAULT_HOME / "rootfs" / "python-3.11-slim"
AGENTS_ROOTFS = DEFAULT_HOME / "rootfs" / "agents"
PY_IMAGE = "python:3.11-slim"
AGENTS_IMAGE = "agentd-sandbox-agents"
PY = "/usr/local/bin/python3"


def _docker_image(image: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _krun_rootfs(rootfs: Path) -> bool:
    return DEFAULT_LAUNCHER.exists() and (rootfs / "usr" / "local" / "bin" / "python3").exists()


_COLIMA_READY = None


def _colima_ready() -> bool:
    global _COLIMA_READY
    if _COLIMA_READY is None:
        from agentd.sandbox.executor import colima_available

        _COLIMA_READY = colima_available()
    return _COLIMA_READY


def run(coro):
    return asyncio.run(coro)


def workdir():
    base = DEFAULT_HOME / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)


async def _py(sb, code, **kw):
    out, rc = await sb.exec([PY, "-c", code], **kw)
    return out.decode(errors="replace"), rc


@pytest.fixture(params=["krun", "krun-colima", "docker"])
def make_sandbox(request):
    """A sandbox factory for each backend, on a plain python image."""
    if request.param == "krun":
        if not _krun_rootfs(PY_ROOTFS):
            pytest.skip("libkrun launcher or python-3.11-slim rootfs not set up")
        make = lambda **kw: KrunSandbox(rootfs=PY_ROOTFS, **kw)  # noqa: E731
    elif request.param == "krun-colima":
        if not _colima_ready():
            pytest.skip("Colima profile not set up (agentd-sandbox colima setup)")
        from agentd.sandbox import colima
        make = lambda **kw: KrunSandbox(rootfs=colima.vm_rootfs("agents"), colima=colima.PROFILE, **kw)  # noqa: E731
    else:
        if not _docker_image(PY_IMAGE):
            pytest.skip(f"docker or the {PY_IMAGE} image not available")
        make = lambda **kw: DockerSandbox(image=PY_IMAGE, **kw)  # noqa: E731
    make.backend = request.param
    return make


@pytest.fixture(params=["krun", "krun-colima", "docker"])
def make_executor(request):
    """An executor factory for each backend, on the agents image."""
    from agentd.sandbox.executor import DockerExecutor, KrunExecutor

    if request.param == "krun":
        if not (_krun_rootfs(AGENTS_ROOTFS) and os.path.lexists(AGENTS_ROOTFS / "usr/local/bin/claude")):
            pytest.skip("libkrun launcher or agents rootfs not set up")
        make = lambda **kw: KrunExecutor(AGENTS_ROOTFS, **kw)  # noqa: E731
    elif request.param == "krun-colima":
        if not _colima_ready():
            pytest.skip("Colima profile not set up (agentd-sandbox colima setup)")
        make = lambda **kw: KrunExecutor(colima=True, **kw)  # noqa: E731
    else:
        if not _docker_image(AGENTS_IMAGE):
            pytest.skip(f"docker or the {AGENTS_IMAGE} image not available")
        make = lambda **kw: DockerExecutor(AGENTS_IMAGE, **kw)  # noqa: E731
    make.backend = request.param
    return make


# --------------------------------------------------------------------------- #
# Sandbox
# --------------------------------------------------------------------------- #

class TestIsolation:
    def test_no_network_egress(self, make_sandbox):
        code = (
            "import socket\n"
            "for h,p in [('1.1.1.1',53),('8.8.8.8',443),('93.184.215.14',80)]:\n"
            "    try:\n"
            "        socket.create_connection((h,p),timeout=3); print('CONNECTED',h)\n"
            "    except OSError as e: print('blocked',h,e.errno)\n"
        )

        async def go():
            async with make_sandbox() as sb:
                return await _py(sb, code)

        out, rc = run(go())
        assert rc == 0, out
        assert "CONNECTED" not in out, out
        assert out.count("blocked") == 3, out

    def test_sessions_do_not_share_writes(self, make_sandbox):
        async def go():
            async with make_sandbox() as a, make_sandbox() as b:
                await a.exec(["/bin/sh", "-c", "echo secret > /etc/agentd-marker"])
                out_a, _ = await a.exec(["/bin/cat", "/etc/agentd-marker"])
                _, rc_b = await b.exec(["/bin/cat", "/etc/agentd-marker"])
                return out_a, rc_b

        out_a, rc_b = run(go())
        assert out_a.strip() == b"secret"
        assert rc_b != 0, "another sandbox must not see the first one's write"


def test_krun_read_only_base_with_session_overlay():
    """Writes anywhere succeed per session, vanish after, and never touch the base."""
    if not _krun_rootfs(PY_ROOTFS):
        pytest.skip("libkrun not set up")
    marker = PY_ROOTFS / "usr" / "local" / "bin" / "agentd-overlay-test"

    async def go():
        async with KrunSandbox(rootfs=PY_ROOTFS) as sb:
            return await sb.exec(["/bin/sh", "-c", "echo x > /usr/local/bin/agentd-overlay-test && cat /usr/local/bin/agentd-overlay-test"])

    out, rc = run(go())
    assert (out.strip(), rc) == (b"x", 0)
    assert not marker.exists()


def test_docker_container_is_locked_down():
    if not _docker_image(PY_IMAGE):
        pytest.skip("docker not available")

    async def go():
        async with DockerSandbox(image=PY_IMAGE) as sb:
            r = subprocess.run(["docker", "inspect", sb.container_name, "--format",
                                "{{.HostConfig.NetworkMode}}|{{.HostConfig.CapDrop}}|{{.HostConfig.SecurityOpt}}"],
                               capture_output=True, text=True)
            return r.stdout.strip(), sb.container_name

    info, name = run(go())
    network, cap_drop, secopt = info.split("|")
    assert network == "none" and "ALL" in cap_drop and "no-new-privileges" in secopt
    gone = subprocess.run(["docker", "inspect", name], capture_output=True)
    assert gone.returncode != 0, "container must be removed when the sandbox stops"


@pytest.mark.skipif(sys.platform != "darwin", reason="Docker VM file sharing is a macOS concern")
def test_docker_rejects_workspace_not_shared_with_vm():
    if not _docker_image(PY_IMAGE):
        pytest.skip("docker not available")
    with tempfile.TemporaryDirectory() as ws:  # /var/folders: not shared by Colima/Docker Desktop by default
        async def go():
            await DockerSandbox(image=PY_IMAGE, workspace=Path(ws)).start()

        with pytest.raises(RuntimeError, match="not shared with the Docker VM"):
            run(go())
        assert not list(Path(ws).glob(".agentd-probe-*")), "probe files must be cleaned up"


class TestExec:
    def test_exit_code_stdin_and_env(self, make_sandbox):
        async def go():
            async with make_sandbox() as sb:
                r1 = await sb.exec(["/bin/sh", "-c", "exit 7"])
                r2 = await sb.exec(["/bin/cat"], stdin=b"hello over the channel")
                r3 = await sb.exec(["/bin/sh", "-c", "echo $FOO; echo err >&2"], env={"FOO": "bar"})
                r4 = await sb.exec(["/no/such/binary"])
                return r1, r2, r3, r4

        r1, r2, r3, r4 = run(go())
        assert r1[1] == 7
        assert r2 == (b"hello over the channel", 0)
        assert r3[0].split() == [b"bar", b"err"] and r3[1] == 0
        assert r4[1] == 127

    def test_large_output_and_parallel_execs(self, make_sandbox):
        async def go():
            async with make_sandbox() as sb:
                big, rc = await sb.exec(["/bin/sh", "-c", "head -c 20000000 /dev/zero"])
                results = await asyncio.gather(*[
                    sb.exec(["/bin/sh", "-c", f"sleep 0.2; echo {i}"]) for i in range(20)
                ])
                return len(big), rc, results

        size, rc, results = run(go())
        assert (size, rc) == (20_000_000, 0)
        assert [int(out) for out, _ in results] == list(range(20))

    def test_workspace_share(self, make_sandbox):
        with workdir() as ws:
            Path(ws, "from_host.txt").write_text("hi from host")

            async def go():
                async with make_sandbox(workspace=Path(ws)) as sb:
                    # Shared at the same absolute path as on the host.
                    out, _ = await sb.exec(["/bin/cat", f"{sb.workspace_path}/from_host.txt"])
                    await sb.exec(["/bin/sh", "-c", f"echo hi from sandbox > {sb.workspace_path}/from_sandbox.txt"])
                    return out

            assert run(go()) == b"hi from host"
            assert Path(ws, "from_sandbox.txt").read_text().strip() == "hi from sandbox"
            assert not list(Path(ws).glob(".agentd-probe-*"))


class TestEndpoints:
    """Sandbox-side endpoints tunnel back over the host's one connection."""

    def test_tcp_and_unix_endpoints_reach_host_targets(self, make_sandbox):
        async def go():
            seen = []

            async def handle(reader, writer):
                data = await reader.read(1024)
                seen.append(data)
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 9\r\n\r\nfrom host")
                await writer.drain()
                writer.close()

            with workdir() as tmp:
                host_sock = str(Path(tmp) / "b.sock")
                unix_server = await asyncio.start_unix_server(handle, path=host_sock)
                tcp_server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
                tcp_port = tcp_server.sockets[0].getsockname()[1]
                endpoints = {
                    "bridge": Endpoint(("unix", "/run/agentd/bridge.sock"), ("unix", host_sock)),
                    "model": Endpoint(("tcp", "127.0.0.1", 8080), ("tcp", "127.0.0.1", tcp_port)),
                }
                async with unix_server, tcp_server, make_sandbox(endpoints=endpoints) as sb:
                    tcp_out, tcp_rc = await _py(
                        sb, "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/v1/messages').read().decode())"
                    )
                    unix_out, unix_rc = await _py(
                        sb,
                        "import socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/agentd/bridge.sock');"
                        "s.sendall(b'GET /health HTTP/1.0\\r\\n\\r\\n');print(s.recv(4096).decode())",
                    )
                return tcp_out, tcp_rc, unix_out, unix_rc, seen

        tcp_out, tcp_rc, unix_out, unix_rc, seen = run(go())
        assert tcp_rc == 0 and tcp_out.strip() == "from host", tcp_out
        assert unix_rc == 0 and "from host" in unix_out, unix_out
        assert any(b"/v1/messages" in s for s in seen) and any(b"/health" in s for s in seen)

    def test_endpoint_with_dead_host_target_fails_cleanly(self, make_sandbox):
        endpoints = {"model": Endpoint(("tcp", "127.0.0.1", 8080), ("tcp", "127.0.0.1", 1))}

        async def go():
            async with make_sandbox(endpoints=endpoints) as sb:
                return await _py(
                    sb, "import urllib.request\ntry:\n  urllib.request.urlopen('http://127.0.0.1:8080/',timeout=5)\nexcept Exception as e: print('failed', type(e).__name__)"
                )

        out, rc = run(go())
        assert rc == 0 and out.startswith("failed"), out


def test_boot_timing_report(make_sandbox, capsys):
    async def go():
        async with make_sandbox() as sb:
            t = time.monotonic()
            await sb.exec(["/bin/true"])
            return sb.timings, time.monotonic() - t

    timings, exec_s = run(go())
    with capsys.disabled():
        print(f"\n{make_sandbox.backend} timings: boot={timings['boot']:.3f}s "
              f"ready={timings['ready']:.3f}s exec(/bin/true)={exec_s * 1000:.1f}ms")
    assert timings["boot"] < 10


def test_failed_start_cleans_up(make_sandbox):
    sessions = DEFAULT_HOME / "sessions"
    before = set(sessions.iterdir()) if sessions.exists() else set()

    async def go():
        await make_sandbox(mounts={"/x": Path("/nonexistent/agentd")}).start()

    with pytest.raises(FileNotFoundError):
        run(go())
    assert (set(sessions.iterdir()) if sessions.exists() else set()) == before


def test_cannot_mount_over_system_paths(make_sandbox):
    async def go():
        await make_sandbox(mounts={"/usr/share/x": Path.home()}).start()

    with pytest.raises(ValueError, match="cannot mount"):
        run(go())


# --------------------------------------------------------------------------- #
# Executor (PTC) on each backend
# --------------------------------------------------------------------------- #

class TestExecutor:
    def test_executor_protocol_and_host_env_isolation(self, make_executor, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
        with workdir() as ws, make_executor() as ex:
            ws = Path(ws).resolve()
            out, rc = ex.execute_bash("id -un; pwd; echo ${ANTHROPIC_API_KEY:-unset}", ws)
            assert rc == 0, out
            assert out.split() == ["agent", str(ws), "unset"]

            (ws / "sub").mkdir()
            out, rc = ex.execute_python("import os; print(os.getcwd(), 6 * 7)", ws / "sub")
            assert (out, rc) == (f"{ws}/sub 42", 0)

            assert ex.create_file("notes/a.txt", "written in sandbox", ws) == "Created file: notes/a.txt"
            assert (ws / "notes" / "a.txt").read_text() == "written in sandbox"

            out, rc = ex.execute_bash("true", Path("/etc"))
            assert rc == 1 and "outside the sandbox workspace" in out

    def test_concurrent_first_calls_share_one_sandbox(self, make_executor):
        async def go(ex, ws):
            results = await asyncio.gather(*[
                ex.execute_bash_async("mkdir /tmp/agentd-once 2>/dev/null && echo created || echo exists", ws)
                for _ in range(5)
            ])
            return sorted(out for out, _ in results)

        with workdir() as ws, make_executor() as ex:
            assert asyncio.run(go(ex, Path(ws))) == ["created", "exists", "exists", "exists", "exists"]

    def test_skills_cli_reaches_host_bridge(self, make_executor, monkeypatch):
        from agentd.ptc import setup_skills_directory
        from agentd.tool_decorator import tool

        calls = []

        @tool
        def sandbox_add_numbers(a: int, b: int) -> int:
            """Add two numbers (called from inside the sandbox)."""
            calls.append((a, b))
            return a + b

        with workdir() as ws, make_executor() as ex:
            ws = Path(ws)
            # setup_skills_directory writes os.environ directly; register the
            # keys with monkeypatch first so they are removed after the test.
            monkeypatch.setenv("PTC_SKILLS_DIR", str(ws / "skills"))
            monkeypatch.setenv("MCP_BRIDGE_SOCKET", "")
            _, address, _ = asyncio.run(setup_skills_directory(
                ws / "skills", None, {}, bridge_socket_path=ex.bridge_socket_path
            ))
            assert address == str(ex.bridge_socket_path)
            out, rc = ex.execute_bash(
                "skills list && echo 'from lib.tools import sandbox_add_numbers; print(sandbox_add_numbers(a=2, b=3))' | skills exec",
                ws,
            )
            assert rc == 0, out
            assert out.splitlines()[-1] == "5", out
            assert calls == [(2, 3)]

    def test_stream_exec_streams_lines_and_kills_abandoned_commands(self, make_executor):
        async def go(ex, ws):
            await ex.ensure_session(ws)
            got = [item async for item in ex.stream_exec(["/bin/sh", "-c", "echo a; echo b >&2; printf c; exit 4"])]
            # Stop reading after the first line of a long-running command.
            gen = ex.stream_exec(["/bin/sh", "-c", "echo started; sleep 30; echo late > /tmp/late"])
            first = await gen.__anext__()
            await gen.aclose()
            await asyncio.sleep(0.5)
            ps, _ = await ex.run(ex.session.sandbox.exec(["/bin/sh", "-c", "pgrep -x sleep || echo none"]))
            return got, first, ps.decode().strip()

        with workdir() as ws, make_executor() as ex:
            got, first, ps = run(go(ex, Path(ws)))
        assert got == [("line", b"a"), ("line", b"b"), ("line", b"c"), ("exit", {"exit": 4})]
        assert first == ("line", b"started")
        assert ps == "none", "closing the stream must kill the command in the sandbox"


class TestShellSession:
    """Executor bash/python share one persistent shell: cd, env, dirs, functions."""

    def test_state_carries_across_calls(self, make_executor):
        with workdir() as ws, make_executor() as ex:
            ws = Path(ws).resolve()
            (ws / "a" / "b").mkdir(parents=True)
            assert ex.execute_bash("cd a && export FOO=bar && greet() { echo hi $1; }", ws)[1] == 0
            assert ex.execute_bash("pwd; echo $FOO; greet there", ws) == (f"{ws}/a\nbar\nhi there", 0)
            assert ex.execute_bash("pushd b >/dev/null && pwd", ws) == (f"{ws}/a/b", 0)
            assert ex.execute_bash("popd >/dev/null && pwd", ws) == (f"{ws}/a", 0)
            # Python runs in the same session: sees the shell's cwd and exports.
            assert ex.execute_python("import os; print(os.getcwd(), os.environ['FOO'])", ws) == (f"{ws}/a bar", 0)
            # A different cwd from the caller moves the shell; the same one does not.
            assert ex.execute_bash("pwd", ws / "a" / "b") == (f"{ws}/a/b", 0)

    def test_edge_cases_do_not_wedge_the_shell(self, make_executor):
        with workdir() as ws, make_executor(timeout=2) as ex:
            ws = Path(ws)
            assert ex.execute_bash("export KEEP=1; printf 'no newline'", ws) == ("no newline", 0)
            assert ex.execute_bash("cat; echo after-cat", ws) == ("after-cat", 0), "stdin must not be the control pipe"
            out, rc = ex.execute_bash("if then fi", ws)
            assert rc != 0 and "syntax error" in out
            assert ex.execute_bash("echo $KEEP; false", ws) == ("1", 1), "session survives errors"
            assert ex.execute_bash("cat <<'EOF'\nline1\nline2\nEOF", ws) == ("line1\nline2", 0)
            assert ex.execute_bash("(sleep 30 &); echo bg", ws) == ("bg", 0)

            out, rc = ex.execute_bash("sleep 10", ws)
            assert rc == 124 and "state lost" in out
            assert ex.execute_bash("echo ${KEEP:-gone}", ws) == ("gone", 0), "timeout restarts the shell"

            assert ex.execute_bash("export X=1; exit 5", ws)[1] == 5
            assert ex.execute_bash("echo ${X:-fresh}", ws) == ("fresh", 0), "exit restarts the shell"

    def test_concurrent_async_calls_serialize_in_the_session(self, make_executor):
        async def go(ex, ws):
            await ex.execute_bash_async("export N=0", ws)
            await asyncio.gather(*[ex.execute_bash_async("N=$((N+1))", ws) for _ in range(10)])
            return await ex.execute_bash_async("echo $N", ws)

        with workdir() as ws, make_executor() as ex:
            assert asyncio.run(go(ex, Path(ws))) == ("10", 0)


def test_cli_transport_disables_every_claude_code_tool():
    from agentd.llm_dispatch import _transport_argv

    argv = _transport_argv("anthropic/claude-sonnet-5", "SYSTEM", {})
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--tools") + 1] == "", "every built-in tool off"
    assert argv[argv.index("--setting-sources") + 1] == "", "no user/project settings or CLAUDE.md"
    assert "--strict-mcp-config" in argv, "no MCP servers (incl. claude.ai connectors)"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM"


# --------------------------------------------------------------------------- #
# Read-only extra mounts
# --------------------------------------------------------------------------- #

class TestReadOnlyMounts:
    def test_readable_never_writable_even_as_root(self, make_sandbox):
        with workdir() as data, workdir() as ws:
            data, ws = Path(data).resolve(), Path(ws).resolve()
            (data / "notes.txt").write_text("host data")
            (ws / "ref").mkdir()
            before = sorted(p.name for p in data.iterdir())

            async def go():
                ro = {str(data): data, "/opt/data": data, f"{ws}/ref": data}  # same path, other path, inside workspace
                async with make_sandbox(workspace=ws, read_only_mounts=ro) as sb:
                    results = {}
                    for target in ro:
                        out, rc = await sb.exec(["/bin/cat", f"{target}/notes.txt"])
                        results[f"read {target}"] = (out.strip(), rc)
                        out, rc = await sb.exec(["/bin/sh", "-c", f"echo x > {target}/new.txt"], user="root")
                        results[f"write {target}"] = (b"Read-only file system" in out, rc)
                    out, rc = await sb.exec(["/bin/sh", "-c", f"echo ok > {ws}/writable.txt && cat {ws}/writable.txt"])
                    results["workspace"] = (out.strip(), rc)
                    return results

            results = run(go())
            for key, value in results.items():
                if key.startswith("read"):
                    assert value == (b"host data", 0), (key, value)
                elif key.startswith("write"):
                    assert value[0] and value[1] != 0, (key, value)
            assert results["workspace"] == (b"ok", 0), "the workspace itself stays writable"
            assert sorted(p.name for p in data.iterdir()) == before, "nothing may be written to a read-only source"
            assert (data / "notes.txt").read_text() == "host data"

    def test_bad_mounts_are_rejected(self, make_sandbox):
        with workdir() as data:
            async def over_system():
                await make_sandbox(read_only_mounts={"/etc/x": Path(data)}).start()

            with pytest.raises(ValueError, match="cannot mount"):
                run(over_system())

        from agentd.sandbox.executor import read_only_mounts

        with pytest.raises(NotADirectoryError):
            read_only_mounts(["/nonexistent/agentd-data"])
        with pytest.raises(ValueError, match="absolute"):
            read_only_mounts({str(DEFAULT_HOME): "relative/path"})


def test_executor_mounts_option(make_executor):
    with workdir() as data, workdir() as ws:
        data = Path(data).resolve()
        (data / "model.bin").write_text("weights")
        with make_executor(mounts={str(data): "/home/agent/models"}) as ex:
            assert ex.execute_bash("cat /home/agent/models/model.bin", Path(ws)) == ("weights", 0)
            out, rc = ex.execute_bash("touch /home/agent/models/x", Path(ws))
            assert rc != 0 and "Read-only file system" in out
        assert sorted(p.name for p in data.iterdir()) == ["model.bin"]


def test_shares_never_expose_agentd_sockets_or_credentials(tmp_path, monkeypatch):
    """Mounting e.g. ~ read-only must not hand a sandbox agentd's sockets, CA key or host logins."""
    from agentd.sandbox import base

    home = tmp_path / "home"
    agentd_home = home / ".agentd"
    (agentd_home / "run").mkdir(parents=True)
    (home / ".codex").mkdir()
    monkeypatch.setattr(base.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(base, "DEFAULT_HOME", agentd_home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    ws = home / "work"
    ws.mkdir()

    def shares(**kw):
        return base.Sandbox(workspace=ws, **kw)._shares()

    assert len(shares(read_only_mounts={"/data": home / "work"})) == 2, "ordinary dirs are fine"
    assert len(shares(mounts={"/t": agentd_home / "transcripts"})) == 2, "agentd's own stores are fine"
    for bad in (home, agentd_home, agentd_home / "run", agentd_home / "run" / "x", agentd_home / "ca",
                home / ".codex", home / ".claude"):
        with pytest.raises(ValueError, match="would expose"):
            shares(read_only_mounts={"/x": bad})
    with pytest.raises(ValueError, match="would expose"):
        base.Sandbox(workspace=home)._shares()


def test_shares_never_expose_host_secrets(tmp_path, monkeypatch):
    """Mounts that would expose agentd's keys, browser profiles or other tools' credentials
    (p2claw's identity key, AGENTD_PROTECTED_PATHS) are refused; others are fine."""
    from agentd.sandbox.base import protected_host_paths

    home = Path.home()
    p2claw = (home / "Library" / "Application Support" / "p2claw") if sys.platform == "darwin" \
        else Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "p2claw"
    assert p2claw.resolve() in protected_host_paths()

    def shares(host):
        return DockerSandbox(image="x", workspace=tmp_path / "ws", read_only_mounts={str(host): str(host)})._shares()

    (tmp_path / "ws").mkdir()
    for exposing in (p2claw.parent, p2claw / "identity.key", DEFAULT_HOME / "browser", DEFAULT_HOME / "egress",
                     Path(f"/tmp/p2claw-{os.getuid()}")):
        with pytest.raises(ValueError, match="cannot share"):
            shares(exposing)
    secret_dir = tmp_path / "vault"
    secret_dir.mkdir()
    shares(tmp_path / "data")  # fine
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("P2CLAW_AGENT_RUNTIME_DIR", str(tmp_path / "p2"))
    assert {(tmp_path / "run" / "p2claw").resolve(), (tmp_path / "p2").resolve()} <= set(protected_host_paths()), \
        "p2claw's socket where Linux (or its override) puts it"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.delenv("P2CLAW_AGENT_RUNTIME_DIR")
    monkeypatch.setenv("AGENTD_PROTECTED_PATHS", f"{secret_dir}{os.pathsep}/nonexistent/x")
    with pytest.raises(ValueError, match="vault"):
        shares(tmp_path)  # contains a path the owner protected
