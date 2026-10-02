"""Smoke test for an installed agentd wheel (run by the release workflow):
the prebuilt binaries are there, current, executable and load."""
import os
import platform
import shutil
import subprocess
import sys
import tempfile

from agentd.sandbox import prebuilt


def run(*argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


m = prebuilt.manifest()
print("manifest:", m)
if not m:
    assert not any(prebuilt.BIN.glob("agentd-*")) if prebuilt.BIN.exists() else True
    print("pure wheel: no binaries, ok")
    sys.exit(0)

net = prebuilt.BIN / ("agentd-net" if sys.platform == "darwin" else f"agentd-net-linux-{platform.machine()}")
assert os.access(net, os.X_OK), f"{net} isn't executable"
r = run(str(net))
assert r.returncode == 2 and "agentd-net" in r.stderr, (r.returncode, r.stderr)  # usage error: it runs

if sys.platform == "darwin":
    launcher = prebuilt.launcher("darwin")
    assert launcher and os.access(launcher, os.X_OK), "darwin launcher"
    ent = run("codesign", "-d", "--entitlements", "-", str(launcher))
    assert "com.apple.security.hypervisor" in ent.stdout + ent.stderr, ent
    assert "libkrun" in run("otool", "-L", str(launcher)).stdout
    assert prebuilt.launcher("linux-aarch64") and prebuilt.libkrun("aarch64"), "Colima binaries"
else:
    arch = platform.machine()
    launcher, lib = prebuilt.launcher(f"linux-{arch}"), prebuilt.libkrun(arch)
    assert launcher and lib and os.access(launcher, os.X_OK), (launcher, lib)
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(lib, os.path.join(d, "libkrun.so.1"))
        ldd = run("ldd", str(launcher), env={**os.environ, "LD_LIBRARY_PATH": d})
        assert "not found" not in ldd.stdout and "libkrun.so.1" in ldd.stdout, ldd.stdout
        import ctypes
        assert hasattr(ctypes.CDLL(os.path.join(d, "libkrun.so.1")), "krun_add_net_unixstream"), "libkrun without net"
    st = run(sys.executable, "-m", "agentd.sandbox.cli", "linux", "status")
    print(st.stdout, st.stderr)
print("ok")
