"""A stand-in for fnox with a locked vault, for tests (agentd.fnox's contract).

Its directory holds ``fake-fnox.json``: ``{"password": ..., "secrets": {NAME: value},
"daemon": true}`` (a null value: the item was deleted from the vault). Like fnox with its daemon and a vault provider such as Enpass:

  * ``--non-interactive get NAME``: the cached value, or empty output (exit 0) while locked;
  * ``get NAME``: prompts for the master password on the terminal, caches NAME
    (only NAME) and prints it; a wrong password fails ("Could not unlock the vault");
  * an undefined NAME fails; ``config-files`` lists ``fnox.toml``.

Every call appends its argv and FNOX_* variables to ``calls.jsonl``.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(sys.argv[0]).resolve().parent


def main() -> int:
    conf = json.loads((HERE / "fake-fnox.json").read_text())
    cache_file = HERE / "cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    args = sys.argv[1:]
    with open(HERE / "calls.jsonl", "a") as log:
        log.write(json.dumps({"argv": args, "fnox_env": sorted(k for k in os.environ if k.startswith("FNOX_"))}) + "\n")
    interactive = "--non-interactive" not in args
    args = [a for a in args if a != "--non-interactive"]
    if args[:1] == ["-P"]:
        args = args[2:]
    if args[:1] == ["config-files"]:
        print(HERE / "fnox.toml")
        return 0
    if args == ["daemon", "clear"]:
        cache_file.unlink(missing_ok=True)
        return 0
    if args[:1] != ["get"] or len(args) != 2:
        return 2
    name = args[1]
    if name not in conf["secrets"]:
        print(f"Error: secret {name} not found", file=sys.stderr)
        return 1
    if name in cache:
        print(cache[name])
        return 0
    if not interactive:
        return 0  # locked: no value, like fnox
    tty = os.open("/dev/tty", os.O_RDWR)
    os.write(tty, b"Fake master password for vault: ")
    typed = b""
    while not typed.endswith(b"\n"):
        chunk = os.read(tty, 1)
        if not chunk:
            break
        typed += chunk
    os.close(tty)
    typed = typed.decode().rstrip("\r\n")
    if typed != conf["password"]:
        print("Error: fnox::provider::auth_failed\n\n  × Enpass: authentication failed: Could not unlock the vault\n"
              "  help: Check the master password", file=sys.stderr)
        return 1
    if conf["secrets"][name] is None:
        print(f"Error: fnox::provider::secret_not_found\n\n  × Enpass: secret '{name}' not found\n"
              "  help: No item with that title in the vault", file=sys.stderr)
        return 1
    if conf.get("daemon", True):
        cache[name] = conf["secrets"][name]
        cache_file.write_text(json.dumps(cache))
    print(conf["secrets"][name])
    return 0


if __name__ == "__main__":
    sys.exit(main())
