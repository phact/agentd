"""Inside the Colima VM: start ``agentd-krun`` and join our stdio to its vsock socket.

    python3 colima_relay.py --sock PATH [--timeout SECONDS] [--require FILE]... -- LAUNCHER [ARG]...

The host runs this through ``colima ssh``, so the host-held ssh process is the
sandbox's one connection, as with the native backend's host-dialed vsock.
The relay:

  1. checks every ``--require`` file exists (proof that each shared host
     directory really is visible in the VM), else exits 3 with a message;
  2. starts the launcher (the microVM) in its own process group;
  3. connects to the launcher's vsock socket, retrying until sandboxd answers
     with its HELLO frame, and forwards that frame;
  4. pumps bytes both ways until either side closes;
  5. kills the launcher if the host side goes away (EOF, SIGHUP, SIGTERM).

Stdlib only; it runs on the VM's python3.
"""
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

HEADER = struct.Struct(">IBI")  # mux frame header: stream id, type, length
HELLO = 1


START = time.monotonic()


def log(msg):
    sys.stderr.write(f"agentd relay [{time.monotonic() - START:5.2f}s]: {msg}\n")
    sys.stderr.flush()


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def connect(path, proc, timeout):
    """Connect and read sandboxd's HELLO; libkrun drops connections until it listens."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None, None
        if os.path.exists(path):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(path)
                s.settimeout(2)
                header = read_exact(s, HEADER.size)
                if header is not None:
                    _, ftype, length = HEADER.unpack(header)
                    payload = read_exact(s, length) if length else b""
                    if ftype == HELLO and payload is not None:
                        s.settimeout(None)
                        return s, header + payload
            except OSError:
                pass
            s.close()
        time.sleep(0.02)
    return None, None


def net_pump(path):
    """--net-pump PATH: listen on PATH in the VM for the launcher's network card
    (libkrun unixstream) and pump its frames to and from our stdio, which the
    host connects to agentd-net. Exits when either side goes away, including
    the host leaving before the launcher ever connects."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    connected = threading.Event()
    holder = {}

    def from_host():
        # Read stdin from the start: EOF means the host is gone, connected or not.
        try:
            while True:
                data = os.read(0, 65536)
                if not data:
                    break
                connected.wait()
                holder["conn"].sendall(data)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        os._exit(0)

    threading.Thread(target=from_host, daemon=True).start()
    log("listening")
    conn, _ = server.accept()
    server.close()
    os.unlink(path)
    holder["conn"] = conn
    connected.set()
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
            os.write(1, data)
    except OSError:
        pass
    os._exit(0)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--net-pump"]:
        net_pump(args[1])
        return
    if "--" not in args:
        sys.exit("usage: colima_relay.py --sock PATH [--require FILE]... -- LAUNCHER [ARG]...")
    split = args.index("--")
    opts, launcher = args[:split], args[split + 1:]
    sock_path, requires, timeout = None, [], 60.0
    for flag, value in zip(opts[::2], opts[1::2]):
        if flag == "--sock":
            sock_path = value
        elif flag == "--timeout":
            timeout = float(value)
        elif flag == "--require":
            requires.append(value)
    for path in requires:
        if not os.path.exists(path):
            directory = os.path.dirname(path)
            log(f"{directory} is not shared with the Colima VM (keep it under $HOME, or add a Colima mount)")
            sys.exit(3)

    # libkrun's file server holds a descriptor per open file in shared
    # directories; ssh sessions start with a soft limit of 1024.
    try:
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except (ImportError, ValueError, OSError) as e:
        log(f"could not raise the open files limit: {e}")
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    proc = subprocess.Popen(launcher, stdin=subprocess.DEVNULL, stdout=2, stderr=2, start_new_session=True)
    log("launcher started")

    def kill_launcher(*_):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def on_signal(signum, _frame):
        kill_launcher()
        os._exit(128 + signum)

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)

    conn, hello = connect(sock_path, proc, timeout=timeout)
    if conn is None:
        if proc.poll() is not None:
            log(f"the launcher exited with code {proc.returncode} before the sandbox came up")
            sys.exit(proc.returncode or 1)
        kill_launcher()
        proc.wait()
        log(f"the sandbox did not boot within {timeout:.0f}s (is the Colima VM busy? "
            "more CPUs/memory or a longer boot_timeout help)")
        sys.exit(124)
    os.write(1, hello)
    log("sandbox is up")

    def host_to_vm():
        try:
            while True:
                data = os.read(0, 65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        # The host went away: tear the sandbox down.
        kill_launcher()
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    threading.Thread(target=host_to_vm, daemon=True).start()
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
            os.write(1, data)
    except OSError:
        pass
    kill_launcher()
    code = proc.wait()
    sys.exit(0 if code in (0, -signal.SIGKILL) else code)


if __name__ == "__main__":
    main()
