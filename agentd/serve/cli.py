"""``agentd serve [--config FILE] [--dir DIR]``: run the session API on Unix sockets."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from agentd.serve.app import Server
from agentd.serve.config import ServeConfig

_TCP_FLAGS = ("--host", "--port", "--bind", "--listen", "--tcp", "-p")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentd serve",
        description="Serve this box's sandboxed agent sessions on Unix sockets "
                    "(serve.sock for local callers, peers.sock for p2claw). There is no TCP mode.")
    parser.add_argument("--config", help="settings file (default: ~/.agentd/serve/config.json)")
    parser.add_argument("--dir", help="state and socket directory (default: ~/.agentd/serve)")
    parser.add_argument("--log-level", default="INFO")
    args, unknown = parser.parse_known_args(argv)
    for flag in unknown:
        if flag.split("=")[0] in _TCP_FLAGS:
            print("agentd serve has no TCP mode: it only listens on Unix sockets, so it can never be "
                  "reached from the network by mistake. Publish peers.sock to your other boxes with "
                  "`p2claw apps expose agentd --socket <path>` (a private route).", file=sys.stderr)
            return 2
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    overrides = {"dir": Path(args.dir)} if args.dir else {}
    config = ServeConfig.load(args.config, **overrides)
    asyncio.run(_serve(config))
    return 0


async def _serve(config: ServeConfig) -> None:
    server = Server(config)
    await server.start()
    print(f"agentd serve ({config.box_name})\n"
          f"  local callers: {config.serve_socket}\n"
          f"  peers:         {config.peers_socket}  (requires {config.identity_header})\n"
          f"Share with your other boxes over p2claw:\n"
          f"  p2claw apps expose agentd --socket {config.peers_socket}\n"
          f"  p2claw apps share agentd --with <peer>", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop()


if __name__ == "__main__":
    sys.exit(main())
