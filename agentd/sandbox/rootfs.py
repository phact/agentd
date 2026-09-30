"""Build sandbox base images.

    python -m agentd.sandbox.rootfs build DOCKERFILE_DIR NAME   # docker build + export
    python -m agentd.sandbox.rootfs export IMAGE NAME           # export an existing image

``build`` produces both forms of the image: the Docker image
``agentd-sandbox-NAME`` (what :class:`~agentd.sandbox.executor.DockerExecutor`
runs) and a libkrun base directory (what ``KrunExecutor`` boots).

Unpacking an image as an unprivileged user loses file ownership (everything
ends up owned by the host user). libkrun's macOS virtiofs reads a file's
sandbox-visible owner and mode from the ``user.containers.override_stat``
xattr (``uid:gid:0mode``), so the exporter records each entry's real
ownership from the image there. Device nodes are skipped (the VM has its own
devtmpfs). Images are built with the ``agent`` user at the host user's
uid/gid, so files shared from the host line up without remapping at runtime.

The directory lands in ``$AGENTD_HOME/rootfs/NAME`` (default
``~/.agentd/rootfs/NAME``) and is shared read-only by every microVM booted
from it.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from agentd.sandbox.base import DEFAULT_HOME

XATTR_KEY = b"user.containers.override_stat"
_XATTR_NOFOLLOW = 0x0001  # macOS <sys/xattr.h>


def _xattr_setter():
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.setxattr
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]

        def set_stat(path: str, value: bytes, is_link: bool) -> None:
            if fn(os.fsencode(path), XATTR_KEY, value, len(value), 0, _XATTR_NOFOLLOW if is_link else 0) != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), path)
        return set_stat

    def set_stat(path: str, value: bytes, is_link: bool) -> None:
        if not is_link:  # Linux forbids user.* xattrs on symlinks
            os.setxattr(path, XATTR_KEY, value, follow_symlinks=False)
    return set_stat


def export_image(image: str, name: str, home: Path = DEFAULT_HOME) -> Path:
    """Export ``image`` to ``home/rootfs/name`` with ownership xattrs."""
    dest = home / "rootfs" / name
    tmp = dest.with_name(f"{name}.tmp.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    set_stat = _xattr_setter()
    cid = subprocess.run(["docker", "create", image], check=True, capture_output=True, text=True).stdout.strip()
    seen: dict[str, str] = {}
    collisions: list[str] = []
    try:
        proc = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if member.ischr() or member.isblk() or member.isfifo():
                    continue
                key = member.name.lower()
                if key in seen and seen[key] != member.name:
                    collisions.append(member.name)
                seen[key] = member.name
                tar.extract(member, tmp, set_attrs=False, filter="fully_trusted")
                path = tmp / member.name
                if not member.issym():
                    # Keep entries readable/writable for the host user doing the export;
                    # the sandbox sees the real mode from the xattr.
                    os.chmod(path, (member.mode & 0o7777) | (0o700 if member.isdir() else 0o600))
                value = f"{member.uid}:{member.gid}:0{member.mode & 0o7777:o}".encode()
                set_stat(str(path), value, member.issym())
        if proc.wait() != 0:
            raise RuntimeError(f"docker export {cid} failed")
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True)
    if collisions:
        print(f"warning: {len(collisions)} paths differ only by case and collided on this "
              f"filesystem, e.g. {collisions[:3]}", file=sys.stderr)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return dest


def build_image(context: Path, name: str, home: Path = DEFAULT_HOME) -> Path:
    image = f"agentd-sandbox-{name}"
    subprocess.run([
        "docker", "build", "-t", image,
        "--build-arg", f"AGENT_UID={os.getuid()}", "--build-arg", f"AGENT_GID={os.getgid()}",
        str(context),
    ], check=True)
    return export_image(image, name, home)


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in ("build", "export"):
        sys.exit(__doc__)
    cmd, src, name = sys.argv[1:]
    dest = build_image(Path(src), name) if cmd == "build" else export_image(src, name)
    print(dest)


if __name__ == "__main__":
    main()
