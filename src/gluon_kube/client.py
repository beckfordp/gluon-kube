# SPDX-License-Identifier: MIT

"""The CLI's side of the cluster (GK-4.1, GK-4.2, GK-4.8): where its state lives, starting
and stopping the node, requests over the node's socket, and getting into a container
(`exec`, `run`) with nsenter from outside every namespace (GK-1.7).

The state directory is `$GLUON_KUBE_STATE`, else `/tmp/gluon-kube-<uid>` (GK-4.1: gone
with the sandbox, and never under the workspace, so never exported). `cluster stop`
removes it.
"""

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

from gluon_kube.egress import HttpProxy
from gluon_kube.node import SOCKET

type Json = dict[str, object]

START_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 30.0
REQUEST_TIMEOUT_S = 120.0


class ClusterError(Exception):
    """The cluster isn't there, or refused a request; the message says what to do."""


def state_dir() -> Path:
    """Where the cluster's state lives."""
    explicit = os.environ.get("GLUON_KUBE_STATE")
    return Path(explicit) if explicit else Path(f"/tmp/gluon-kube-{os.getuid()}")


def request(op: str, state: Path | None = None, **fields: object) -> Json:
    """One request to the node; its answer, or ClusterError."""
    path = (state or state_dir()) / SOCKET
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(REQUEST_TIMEOUT_S)
            connection.connect(str(path))
            connection.sendall(json.dumps({"op": op, **fields}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError):
        raise ClusterError("no cluster running: gluon-kube cluster start") from None
    answer = json.loads(data)
    if not isinstance(answer, dict):
        raise ClusterError(f"bad answer from the node: {data!r}")
    if not answer.get("ok"):
        raise ClusterError(str(answer.get("error")))
    return answer


def running(state: Path | None = None) -> bool:
    """Whether a node answers."""
    try:
        request("ping", state)
    except (ClusterError, OSError):
        return False
    return True


def start(state: Path | None = None, backoff_base_s: float = 10.0) -> int:
    """Starts the supervisor and its node (detached: they outlive this process) and waits
    for the node to answer; the supervisor's pid."""
    state = state or state_dir()
    if running(state):
        raise ClusterError(f"a cluster is already running (state {state})")
    shutil.rmtree(state, ignore_errors=True)
    state.mkdir(parents=True)
    log = (state / "node.log").open("ab")
    # The supervisor (outside the node's namespaces) starts the node and relays its
    # egress; the node ends with it.
    argv = [sys.executable, "-m", "gluon_kube.supervisor", str(state)]
    argv += ["--backoff-base", str(backoff_base_s)]
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
    )
    log.close()
    (state / "node.pid").write_text(f"{process.pid}\n")
    deadline = time.monotonic() + START_TIMEOUT_S
    while not running(state):
        if process.poll() is not None:
            tail = (state / "node.log").read_text(errors="replace")[-2000:]
            raise ClusterError(f"the node exited ({process.returncode}):\n{tail}")
        if time.monotonic() > deadline:
            process.kill()
            raise ClusterError(f"the node didn't answer in {START_TIMEOUT_S:.0f} s")
        time.sleep(0.1)
    return process.pid


def stop(state: Path | None = None) -> bool:
    """Stops the node (every pod with it) and removes the state; False if none ran."""
    state = state or state_dir()
    pid_file = state / "node.pid"
    if not pid_file.exists():
        shutil.rmtree(state, ignore_errors=True)
        return False
    pid = int(pid_file.read_text())
    with contextlib.suppress(ClusterError, OSError):
        request("shutdown", state)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
    shutil.rmtree(state, ignore_errors=True)
    return True


def alive(pid: int) -> bool:
    """Whether `pid` is a live process (not a zombie)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def running_target(ref: str, container: str | None, timeout_s: float) -> Json:
    """A container of `ref` once it runs (its command exec'd); ClusterError if it doesn't
    in time."""
    deadline = time.monotonic() + timeout_s
    while True:
        answer = request("target", ref=ref, container=container)
        target = answer["target"]
        assert isinstance(target, dict)
        if target.get("ready_for_exec"):
            return target
        if time.monotonic() > deadline:
            raise ClusterError(f"{ref}: container not running ({target.get('state')})")
        time.sleep(0.2)


def read_exactly(connection: socket.socket, size: int) -> bytes:
    """`size` bytes from a socket (fewer only if it closed)."""
    data = b""
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def stream(
    ref: str,
    container: str | None,
    argv: Sequence[str],
    stdin: BinaryIO | None,
    stdout: BinaryIO,
    stderr: BinaryIO,
    state: Path | None = None,
) -> int:
    """`exec` through the node (as kubectl through the API server): `argv` runs in the
    container; `stdin` (if given) is sent, stdout and stderr come back; its exit code."""
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.connect(str((state or state_dir()) / SOCKET))
    except (FileNotFoundError, ConnectionRefusedError):
        raise ClusterError("no cluster running: gluon-kube cluster start") from None
    with connection:
        header: dict[str, object] = {"op": "exec", "ref": ref, "container": container}
        header |= {"argv": list(argv), "stdin": stdin is not None}
        connection.sendall(json.dumps(header).encode() + b"\n")
        line = b""
        while not line.endswith(b"\n"):
            chunk = connection.recv(1)
            if not chunk:
                raise ClusterError("the node closed the connection")
            line += chunk
        answer = json.loads(line)
        if not answer.get("ok"):
            raise ClusterError(str(answer.get("error")))

        def feed() -> None:
            assert stdin is not None
            with contextlib.suppress(OSError):
                while chunk := stdin.read1(65536):  # type: ignore[attr-defined]
                    connection.sendall(chunk)
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_WR)

        if stdin is not None:
            threading.Thread(target=feed, daemon=True).start()
        else:
            connection.shutdown(socket.SHUT_WR)
        while True:
            head = read_exactly(connection, 5)
            if len(head) < 5:
                raise ClusterError("the exec stream ended without an exit code")
            data = read_exactly(connection, int.from_bytes(head[1:], "big"))
            if head[:1] == b"x":
                return int(data)
            sink = stdout if head[:1] == b"1" else stderr
            sink.write(data)
            sink.flush()


def execute(
    ref: str, container: str | None, argv: Sequence[str], stdin: bool, timeout_s: float = 60.0
) -> int:
    """`exec`: runs `argv` in a running container; stdin passed with -i."""
    running_target(ref, container, timeout_s)
    return stream(
        ref,
        container,
        argv,
        sys.stdin.buffer if stdin else None,
        sys.stdout.buffer,
        sys.stderr.buffer,
    )


def run(argv: Sequence[str], env: Mapping[str, str], image: str | None = None) -> int:
    """`run`: `argv` in a throwaway pod with the cluster's names (stdin passed through),
    then the pod is gone."""
    answer = request("run", env=dict(env), image=image)
    pod = str(answer["pod"])
    try:
        running_target(f"pod/{pod}", None, START_TIMEOUT_S)
        return stream(
            f"pod/{pod}", None, argv, sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer
        )
    finally:
        with contextlib.suppress(ClusterError, OSError):
            request("delete", kind="Pod", name=pod)


def open_forward(
    ref: str, port: int, state: Path | None = None, host: str | None = None
) -> socket.socket:
    """A connection through the node to `ref`'s `port`, or to cluster name `host`'s
    (ClusterError if refused)."""
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.connect(str((state or state_dir()) / SOCKET))
    except (FileNotFoundError, ConnectionRefusedError):
        connection.close()
        raise ClusterError("no cluster running: gluon-kube cluster start") from None
    target: dict[str, object] = {"host": host} if host is not None else {"ref": ref}
    connection.sendall(json.dumps({"op": "forward", "port": port, **target}).encode() + b"\n")
    line = b""
    while not line.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk:
            break
        line += chunk
    answer = json.loads(line or b"{}")
    if not answer.get("ok"):
        connection.close()
        raise ClusterError(str(answer.get("error", "the node closed the connection")))
    return connection


def port_forward(
    ref: str, mappings: Sequence[tuple[int, int]], address: str, stop: threading.Event
) -> None:
    """`port-forward`: each local ADDRESS:LOCAL forwarded to `ref`'s REMOTE through the
    node (ADR 0030 decision 3: from outside, through the node's socket), until `stop`."""
    from gluon_kube.proxy import splice

    for _, remote in mappings:
        open_forward(ref, remote).close()  # the target exists and answers
    listeners = []
    for local, remote in mappings:
        listener = socket.create_server((address, local), backlog=64)
        listeners.append(listener)
        print(f"Forwarding from {address}:{listener.getsockname()[1]} -> {remote}", flush=True)

        def serve(listener: socket.socket = listener, remote: int = remote) -> None:
            while True:
                try:
                    client, _ = listener.accept()
                except OSError:
                    return
                try:
                    upstream = open_forward(ref, remote)
                except (ClusterError, OSError) as error:
                    print(f"Handling connection for {remote}: {error}", file=sys.stderr)
                    client.close()
                    continue
                splice(client, upstream)

        threading.Thread(target=serve, daemon=True).start()
    stop.wait()
    for listener in listeners:
        listener.close()


class ClusterNamesProxy(HttpProxy):
    """`gkube proxy`: the cluster's names outside the cluster. An HTTP proxy on the
    sandbox's localhost: a cluster name (a Service in any of its /etc/hosts forms, as a
    pod resolves it) or a cluster address is reached through the node, as from a pod, on
    the Service's own port; anything else is connected directly, so a browser pointed at
    it still reaches everything else."""

    name = "gluon-kube proxy"

    def __init__(self, address: tuple[str, int], state: Path | None = None) -> None:
        self.state = state
        super().__init__(address)

    def open(self, host: str, port: int) -> socket.socket:
        """Through the node for the cluster's names, else directly."""
        try:
            return open_forward("", port, self.state, host=host)
        except ClusterError as error:
            if "not a cluster name" not in str(error):
                raise OSError(str(error)) from None
        upstream = socket.create_connection((host, port), timeout=15)
        upstream.settimeout(None)
        return upstream
