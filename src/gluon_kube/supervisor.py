# SPDX-License-Identifier: MIT

"""The node's parent outside its namespaces (GK-5.3): `python -m gluon_kube.supervisor
STATE`, started by `gluon-kube cluster start`.

It starts the node (bwrap: its own user and network namespace, GK-1.1) as its child and
lives as long as it does, and gives the cluster a way out: pods have no route beyond the
node (no NAT without iptables), so the node's egress proxy (`egress.py`) hands each
outside connection to this process over a Unix socket in the state directory
(`egress.sock`), and it connects from the sandbox's own network.

The relay's protocol: the node sends `HOST PORT\\n`; the supervisor connects and answers
`ok\\n` (then the bytes flow both ways) or `error <why>\\n`.
"""

import argparse
import contextlib
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from gluon_kube.proxy import splice

EGRESS_SOCKET = "egress.sock"
CONNECT_TIMEOUT_S = 15.0

NODE_NAMESPACE = (
    "bwrap",
    "--unshare-user",
    "--unshare-net",
    "--uid",
    "0",
    "--gid",
    "0",
    "--cap-add",
    "ALL",
    "--dev-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "--die-with-parent",
)
"""The node: uid 0 with every capability in a user and network namespace of its own, the
host's filesystem as it is (GK-1.1); it ends with this process."""


def read_line(connection: socket.socket, limit: int = 1024) -> str:
    """One line from a socket (the relay's header)."""
    data = b""
    while not data.endswith(b"\n") and len(data) < limit:
        chunk = connection.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode(errors="replace").strip()


def relay(client: socket.socket) -> None:
    """One relayed connection: the header, the connect, then both ways."""
    try:
        host, port = read_line(client).rsplit(" ", 1)
        upstream = socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT_S)
    except (OSError, ValueError) as error:
        with contextlib.suppress(OSError):
            client.sendall(f"error {error}\n".encode())
        client.close()
        return
    upstream.settimeout(None)
    client.sendall(b"ok\n")
    splice(client, upstream)


def serve_egress(path: Path) -> socket.socket:
    """The relay's listener, served in a thread."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(64)

    def accept() -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=relay, args=(client,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener


def main(argv: Sequence[str] | None = None) -> int:
    """Serves egress and runs the node until it exits; its exit code."""
    parser = argparse.ArgumentParser(prog="gluon-kube supervisor")
    parser.add_argument("state", type=Path)
    parser.add_argument("--backoff-base", type=float, default=10.0)
    args = parser.parse_args(argv)
    listener = serve_egress(args.state / EGRESS_SOCKET)
    node_argv = [*NODE_NAMESPACE, sys.executable, "-m", "gluon_kube.node", str(args.state)]
    node_argv += ["--backoff-base", str(args.backoff_base)]
    node = subprocess.Popen(node_argv, stdin=subprocess.DEVNULL)
    signal.signal(signal.SIGTERM, lambda *_: node.terminate())
    code = node.wait()
    listener.close()
    with contextlib.suppress(FileNotFoundError):
        (args.state / EGRESS_SOCKET).unlink()
    return code


if __name__ == "__main__":
    sys.exit(main())
