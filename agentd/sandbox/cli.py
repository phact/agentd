"""agentd-sandbox: check and set up agentd's sandbox backends.

    agentd-sandbox status                       # which backends are ready
    agentd-sandbox colima status [--profile P] [--image NAME [--image-dir DIR]]   # read-only
    agentd-sandbox colima setup  [--profile P] [--image NAME [--image-dir DIR]]
                                 [--cpus N] [--memory GiB] [--disk GiB] [--dry-run] [--yes [--recreate]]
    agentd-sandbox colima images [--profile P]   # images built in the VM
    agentd-sandbox linux status  [--image NAME [--image-dir DIR]]   # native libkrun on Linux (read-only)
    agentd-sandbox linux setup   [--image NAME [--image-dir DIR]] [--dry-run] [--yes]

``--image-dir`` is a directory with a Dockerfile; ``--image`` names the image
(default: the directory's name, or ``agents``, agentd's built-in image). It
may build FROM another agentd image (``FROM agentd-sandbox-agents``), which is
built first. Afterwards, ``--image NAME`` alone rebuilds it from the same
directory when it changes; use it with ``KrunExecutor(colima=True, image=NAME)``.

``setup`` shows its plan and asks before changing anything. ``--dry-run``
only shows the plan. ``--yes`` approves non-destructive steps without
asking; deleting and recreating a VM additionally needs ``--recreate``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from agentd.sandbox import colima

    parser = argparse.ArgumentParser(prog="agentd-sandbox", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show which sandbox backends are ready")
    col = sub.add_parser("colima", help="libkrun inside a Colima VM (nested virtualization)")
    col_sub = col.add_subparsers(dest="action", required=True)
    images = col_sub.add_parser("images")
    images.add_argument("--profile", default=colima.PROFILE)
    for name in ("status", "setup"):
        p = col_sub.add_parser(name)
        p.add_argument("--profile", default=colima.PROFILE)
        p.add_argument("--image", help="image name (default: --image-dir's name, else 'agents')")
        p.add_argument("--image-dir", help="directory with the image's Dockerfile")
        if name == "setup":
            p.add_argument("--cpus", type=int, default=colima.DEFAULT_CPUS)
            p.add_argument("--memory", type=int, default=colima.DEFAULT_MEMORY_GIB, help="GiB")
            p.add_argument("--disk", type=int, default=colima.DEFAULT_DISK_GIB, help="GiB")
            p.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
            p.add_argument("--yes", action="store_true", help="approve non-destructive steps without asking")
            p.add_argument("--recreate", action="store_true",
                           help="with --yes: also allow deleting and recreating the VM")
    lin = sub.add_parser("linux", help="native libkrun on a Linux host (KVM)")
    lin_sub = lin.add_subparsers(dest="action", required=True)
    for name in ("status", "setup"):
        p = lin_sub.add_parser(name)
        p.add_argument("--image", help="image name (default: --image-dir's name, else 'agents')")
        p.add_argument("--image-dir", help="directory with the image's Dockerfile")
        if name == "setup":
            p.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
            p.add_argument("--yes", action="store_true", help="approve the plan without asking")
    args = parser.parse_args(argv)
    if args.command == "linux":
        return _linux(args)

    if args.command == "status":
        from agentd.sandbox.executor import colima_available, docker_available, krun_available

        for name, ok in (("krun (native)", krun_available()), ("krun (colima)", colima_available()),
                         ("docker", docker_available())):
            print(f"{name:15} {'ready' if ok else 'not set up'}")
        return 0
    if args.action == "images":
        state = colima.vm_state(args.profile)
        built = {k: v for k, v in (state.get("rootfs") or {}).items() if isinstance(v, dict)}
        if not built:
            print(f"no images built in Colima profile {args.profile!r}")
            return 0
        for name, rec in sorted(built.items()):
            print(f"{name:16} {rec.get('dir')}  (python {rec.get('python')})")
        return 0
    if args.image is None:
        args.image = Path(args.image_dir).expanduser().resolve().name if args.image_dir else "agents"
    if args.action == "status":
        st = colima.status(args.profile, image=args.image, image_dir=args.image_dir)
        print(st.report())
        return 0 if st.ready else 1
    if args.dry_run:
        st = colima.status(args.profile, image=args.image, image_dir=args.image_dir)
        steps = colima.plan(st, cpus=args.cpus, memory_gib=args.memory, disk_gib=args.disk, image=args.image)
        print(st.report())
        for i, step in enumerate(steps, 1):
            print(f"{i}. {'[DESTRUCTIVE] ' if step.destructive else ''}{step.description}")
            for command in step.commands:
                print(f"     {command}")
        return 0
    approve = None
    if args.yes:
        def approve(steps):
            if any(step.destructive for step in steps) and not args.recreate:
                print("The plan deletes and recreates the VM; pass --recreate as well to allow that.",
                      file=sys.stderr)
                return False
            return True
    try:
        st = colima.setup(args.profile, cpus=args.cpus, memory_gib=args.memory, disk_gib=args.disk,
                          image=args.image, image_dir=args.image_dir, approve=approve)
    except (RuntimeError, colima.ColimaNotReady) as e:
        print(e, file=sys.stderr)
        return 1
    return 0 if st.ready else 1


def _linux(args) -> int:
    from agentd.sandbox import linux

    image = args.image or (Path(args.image_dir).expanduser().resolve().name if args.image_dir else "agents")
    st = linux.status(image, args.image_dir)
    if args.action == "status":
        print(st.report())
        return 0 if st.ready else 1
    if args.dry_run:
        print(st.report())
        for i, step in enumerate(linux.plan(st, image=image, image_dir=args.image_dir), 1):
            print(f"{i}. {step.description}")
            for command in step.commands:
                print(f"     {command}")
        return 0
    try:
        st = linux.setup(image=image, image_dir=args.image_dir, approve=(lambda steps: True) if args.yes else None)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    return 0 if st.ready else 1


if __name__ == "__main__":
    sys.exit(main())
