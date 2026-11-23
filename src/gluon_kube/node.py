# SPDX-License-Identifier: MIT

"""The node: `python -m gluon_kube.node STATE`, started by `gluon-kube cluster start`
inside its own user and network namespace (bwrap; ADR 0030 decision 3).

It owns the cluster: the network, the Service proxies, every pod, and the control loop
(`Cluster.tick` every TICK_S). The CLI talks to it over a Unix socket in the state
directory (`node.sock`), one JSON request and one JSON answer per connection (GK-4.1).
Killing it ends every pod (`--die-with-parent`); `cluster stop` asks it to stop first.
"""

import argparse
import contextlib
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from io import BufferedIOBase
from pathlib import Path
from typing import BinaryIO

import yaml

from gluon_kube.controller import ApplyError, Cluster, ContainerRecord, PodRecord
from gluon_kube.egress import EgressProxy, in_cluster
from gluon_kube.images import HOST_IMAGE, HostTools, Recipe
from gluon_kube.manifests import Loaded, ManifestError, parse
from gluon_kube.model import Deployment
from gluon_kube.network import Network
from gluon_kube.pods import NAMESPACE, random_suffix
from gluon_kube.proxy import pipe
from gluon_kube.runtime import (
    ContainerProcess,
    NodeRuntime,
    SandboxHandle,
    enter_argv,
    env_argv,
    set_up,
)
from gluon_kube.status import pod_row
from gluon_kube.supervisor import EGRESS_SOCKET

TICK_S = 0.2
SOCKET = "node.sock"

type Json = dict[str, object]


def host_ids() -> tuple[int, int]:
    """The sandbox user's uid and gid outside the node's user namespace (its uid_map)."""

    def outside(path: str) -> int:
        first = Path(path).read_text().split()
        return int(first[1]) if len(first) >= 2 else os.getuid()

    return outside("/proc/self/uid_map"), outside("/proc/self/gid_map")


def parse_documents(documents: Sequence[Mapping[str, str]]) -> list[Loaded]:
    """Manifests sent by the CLI ({source, text}) into the model."""
    items: list[Loaded] = []
    for document in documents:
        source, text = document["source"], document["text"]
        try:
            loaded = list(yaml.safe_load_all(text))
        except yaml.YAMLError as error:
            raise ManifestError(f"{source}: not valid YAML: {error}") from None
        items += [
            parse(doc, f"{source}#{index}")
            for index, doc in enumerate(loaded, start=1)
            if doc is not None
        ]
    return items


def service_resolver(cluster: Cluster) -> Callable[[str], str | None]:
    """A cluster name (a Service in any of its /etc/hosts forms) to its ClusterIP."""

    def resolve(host: str) -> str | None:
        name = host.removesuffix(".cluster.local").removesuffix(".svc")
        name = name.removesuffix(f".{NAMESPACE}")
        with cluster.lock:
            record = cluster.services.get(name)
        return record.ip if record is not None else None

    return resolve


def target(pod: PodRecord, container: ContainerRecord) -> Json:
    """How the CLI gets into a container: its pid, user, env, working directory, log."""
    recipe = container.recipe
    process = container.process
    return {
        "pod": pod.name,
        "container": container.name,
        "pid": process.pid if isinstance(process, ContainerProcess) else None,
        "sandbox": pod.sandbox.pid if isinstance(pod.sandbox, SandboxHandle) else None,
        "nested_user": isinstance(recipe, Recipe) and (recipe.uid, recipe.gid) != (0, 0),
        "env": container.env,
        "workdir": recipe.config.working_dir if isinstance(recipe, Recipe) else "/",
        "log": str(container.log),
        "state": container.state,
        "ready_for_exec": container.state == "Running"
        and isinstance(process, ContainerProcess)
        and set_up(process.pid),
    }


class Node:
    """The control API over the cluster."""

    def __init__(self, cluster: Cluster, stop: Callable[[], None]) -> None:
        self.cluster = cluster
        self.stop = stop

    def handle(self, request: Json) -> Json:
        """One request: {"op": …, …}; the answer always has "ok"."""
        op = str(request.get("op"))
        handler = getattr(self, f"op_{op.replace('-', '_')}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown op {op}"}
        try:
            result: Json = handler(request)
        except (ApplyError, ManifestError, KeyError, ValueError, OSError) as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **result}

    def op_ping(self, _: Json) -> Json:
        return {"pid": os.getpid()}

    def op_apply(self, request: Json) -> Json:
        documents = request["documents"]
        assert isinstance(documents, list)
        lines = self.cluster.apply(parse_documents(documents))
        return {"lines": lines}

    def op_delete(self, request: Json) -> Json:
        kind, name = str(request["kind"]), str(request["name"])
        if kind == "Pod":
            return {"deleted": self.cluster.delete_pod(name)}
        return {"deleted": self.cluster.delete(kind, name)}

    def op_pods(self, _: Json) -> Json:
        now = self.cluster.clock()
        with self.cluster.lock:
            pods = sorted(self.cluster.pods.values(), key=lambda p: p.name)
            return {"pods": [pod_row(pod, now) for pod in pods if pod.owner_kind != "Run"]}

    def op_services(self, _: Json) -> Json:
        with self.cluster.lock:
            return {
                "services": [
                    {"name": name, "ip": record.ip, "endpoints": record.endpoints}
                    for name, record in sorted(self.cluster.services.items())
                ]
            }

    def op_objects(self, _: Json) -> Json:
        with self.cluster.lock:
            return {"objects": [[kind, name] for kind, name in sorted(self.cluster.objects)]}

    def resolve(self, ref: str) -> PodRecord:
        """`deploy/x`, `deployment/x`, `job/x` or `pod/x` (or a bare pod name): the pod, a
        Ready one first for a controller, as kubectl picks."""
        kind, _, name = ref.rpartition("/")
        kinds = {"deploy": "Deployment", "deployment": "Deployment", "job": "Job", "": "Pod"}
        kinds |= {"pod": "Pod", "deployments": "Deployment", "jobs": "Job", "pods": "Pod"}
        kind = kinds.get(kind.lower(), kind)
        if kind == "Pod":
            with self.cluster.lock:
                pod = self.cluster.pods.get(name)
            if pod is None:
                raise KeyError(f'pods "{name}" not found')
            return pod
        pods = self.cluster.pods_of(kind, name)
        if not pods:
            raise KeyError(f"no pod for {ref}")
        return next((p for p in pods if p.ready), pods[0])

    def op_target(self, request: Json) -> Json:
        pod = self.resolve(str(request["ref"]))
        wanted = request.get("container")
        with self.cluster.lock:
            containers = pod.containers if not request.get("init") else pod.init_containers
            candidates = pod.all_containers() if wanted else containers
            chosen = next((c for c in candidates if not wanted or c.name == wanted), None)
            if chosen is None:
                raise KeyError(f"container {wanted} not found in pod {pod.name}")
            return {"target": target(pod, chosen), "ip": pod.ip}

    def op_logs(self, request: Json) -> Json:
        """A container's output so far (`previous`: its last instance's), `tail` lines."""
        pod = self.resolve(str(request["ref"]))
        wanted = request.get("container")
        with self.cluster.lock:
            candidates = pod.all_containers() if wanted else pod.containers
            chosen = next((c for c in candidates if not wanted or c.name == wanted), None)
        if chosen is None:
            raise KeyError(f"container {wanted} is not valid for pod {pod.name}")
        path = chosen.log.with_suffix(".previous.log") if request.get("previous") else chosen.log
        if not path.exists():
            if request.get("previous"):
                raise KeyError(
                    f'previous terminated container "{chosen.name}" in pod "{pod.name}" not found'
                )
            return {"text": ""}
        lines = path.read_text(errors="replace").splitlines(keepends=True)
        tail = request.get("tail")
        if isinstance(tail, int) and tail >= 0:
            lines = lines[len(lines) - tail :] if tail else []
        return {"text": "".join(lines)}

    def stream_exec(self, request: Json, reader: BufferedIOBase, connection: socket.socket) -> None:
        """`exec`, as the API server streams it: the command run in the container by the
        node (from anywhere the socket reaches, pods included), stdin from the client,
        stdout and stderr back in frames (`1`/`2` + length + data), then `x` + the exit
        code. A client that goes away kills the command."""
        try:
            pod = self.resolve(str(request["ref"]))
            wanted = request.get("container")
            with self.cluster.lock:
                chosen = next((c for c in pod.containers if not wanted or c.name == wanted), None)
                sandbox, recipe = pod.sandbox, chosen.recipe if chosen else None
                process = chosen.process if chosen else None
            if chosen is None:
                raise KeyError(f"container {wanted} is not valid for pod {pod.name}")
            if not (
                isinstance(process, ContainerProcess)
                and isinstance(sandbox, SandboxHandle)
                and isinstance(recipe, Recipe)
            ):
                raise KeyError(f"container {chosen.name} in pod {pod.name} is not running")
            argv = request.get("argv")
            assert isinstance(argv, list)
        except (KeyError, AssertionError) as error:
            connection.sendall(json.dumps({"ok": False, "error": str(error)}).encode() + b"\n")
            return
        nested = (recipe.uid, recipe.gid) != (0, 0)
        command = [*enter_argv(sandbox.pid, process.pid, nested), "--"]
        command += env_argv(chosen.env, recipe.config.working_dir, [str(a) for a in argv])
        connection.sendall(b'{"ok": true}\n')
        stdin = bool(request.get("stdin"))
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        lock = threading.Lock()

        def frame(kind: bytes, data: bytes) -> None:
            with lock:
                connection.sendall(kind + len(data).to_bytes(4, "big") + data)

        def pump(stream: BinaryIO | None, kind: bytes) -> None:
            assert stream is not None
            try:
                while chunk := stream.read1(65536):  # type: ignore[attr-defined]
                    frame(kind, chunk)
            except OSError:
                child.kill()

        def feed() -> None:
            assert child.stdin is not None
            with contextlib.suppress(OSError):
                while chunk := reader.read1(65536):
                    child.stdin.write(chunk)
                    child.stdin.flush()
            with contextlib.suppress(OSError):
                child.stdin.close()

        if stdin:
            threading.Thread(target=feed, daemon=True).start()
        pumps = [
            threading.Thread(target=pump, args=(child.stdout, b"1"), daemon=True),
            threading.Thread(target=pump, args=(child.stderr, b"2"), daemon=True),
        ]
        for thread in pumps:
            thread.start()
        for thread in pumps:
            thread.join()
        code = child.wait()
        with contextlib.suppress(OSError):
            frame(b"x", str(code).encode())

    def forward_target(self, ref: str, port: int) -> tuple[str, int]:
        """Where a port-forward connects, from the node: a Service's ClusterIP and port
        (through its proxy, so to a Ready pod), or a pod's IP."""
        kind, _, name = ref.rpartition("/")
        if kind.lower() in ("svc", "service", "services"):
            with self.cluster.lock:
                record = self.cluster.services.get(name)
            if record is None:
                raise KeyError(f'services "{name}" not found')
            if port not in [p for p, _ in record.endpoints]:
                raise KeyError(f"Service {name} has no port {port}")
            return record.ip, port
        pod = self.resolve(ref)
        if pod.ip is None:
            raise KeyError(f"pod {pod.name} has no IP yet")
        return pod.ip, port

    def cluster_address(self, host: str) -> str:
        """A cluster name (a Service in any of its /etc/hosts forms) or address, as a pod
        resolves it; KeyError `not a cluster name` for anything else."""
        ip = service_resolver(self.cluster)(host)
        if ip is not None:
            return ip
        if in_cluster(host):
            return host
        raise KeyError(f"not a cluster name: {host}")

    def stream_forward(self, request: Json, connection: socket.socket) -> None:
        """`port-forward`, one connection: the node connects to the target and the two
        sockets are piped until both sides end (kubectl's, through the API server)."""
        try:
            port = int(str(request["port"]))
            if "host" in request:
                host = self.cluster_address(str(request["host"]))
            else:
                host, port = self.forward_target(str(request["ref"]), port)
            upstream = socket.create_connection((host, port), timeout=10)
        except (KeyError, ValueError, OSError) as error:
            connection.sendall(json.dumps({"ok": False, "error": str(error)}).encode() + b"\n")
            return
        upstream.settimeout(None)
        connection.sendall(b'{"ok": true}\n')
        there = threading.Thread(target=pipe, args=(connection, upstream), daemon=True)
        there.start()
        pipe(upstream, connection)
        there.join()
        upstream.close()

    def op_run(self, request: Json) -> Json:
        env = request.get("env") or {}
        assert isinstance(env, dict)
        name = f"gluon-kube-run-{random_suffix()}"
        image = str(request.get("image") or HOST_IMAGE)
        self.cluster.run_pod(
            name, image, ["sleep", "infinity"], {str(k): str(v) for k, v in env.items()}
        )
        return {"pod": name}

    def op_helm(self, request: Json) -> Json:
        """`helm upgrade --install`: the rendered release applied; helm's summary."""
        documents = request["documents"]
        assert isinstance(documents, list)
        release = str(request["release"])
        lines = self.cluster.apply(parse_documents(documents))
        errors = [line for line in lines if line.startswith("error")]
        if errors:
            raise ValueError("; ".join(errors))
        revision = self.cluster.releases.get(release, 0) + 1
        self.cluster.releases[release] = revision
        head = (
            f'Release "{release}" does not exist. Installing it now.\n'
            if revision == 1
            else f'Release "{release}" has been upgraded. Happy Helming!\n'
        )
        message = head + (
            f"NAME: {release}\nNAMESPACE: {NAMESPACE}\nSTATUS: deployed\nREVISION: {revision}"
        )
        return {"message": message, "revision": revision}

    def op_images(self, request: Json) -> Json:
        """The image a Deployment's first container runs ("" if none)."""
        obj = self.cluster.get("Deployment", str(request["name"]))
        image = obj.template.spec.containers[0].image if isinstance(obj, Deployment) else ""
        return {"image": image}

    def op_create_job(self, request: Json) -> Json:
        line = self.cluster.job_from_cron_job(str(request["cronjob"]), str(request["name"]))
        return {"line": line}

    def op_rollout_restart(self, request: Json) -> Json:
        name = str(request["name"])
        if not self.cluster.restart_rollout(name):
            raise KeyError(f'deployments.apps "{name}" not found')
        return {}

    def op_rollout_status(self, request: Json) -> Json:
        done, message = self.cluster.rollout_done(str(request["name"]))
        return {"done": done, "message": message}

    def op_job(self, request: Json) -> Json:
        record = self.cluster.job_status(str(request["name"]))
        if record is None:
            raise KeyError(f'jobs.batch "{request["name"]}" not found')
        return {"succeeded": record.succeeded, "failed": record.failed}

    def op_events(self, _: Json) -> Json:
        with self.cluster.lock:
            return {"events": list(self.cluster.events)}

    def op_shutdown(self, _: Json) -> Json:
        threading.Thread(target=self.stop, daemon=True).start()
        return {}


def serve(node: Node, path: Path) -> socketserver.ThreadingUnixStreamServer:
    """The control socket: a JSON request line in, a JSON answer line out."""

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline()
            try:
                request = json.loads(line)
                if isinstance(request, dict) and request.get("op") == "exec":
                    node.stream_exec(request, self.rfile, self.request)
                    return
                if isinstance(request, dict) and request.get("op") == "forward":
                    node.stream_forward(request, self.request)
                    return
                answer = node.handle(request) if isinstance(request, dict) else {"ok": False}
            except json.JSONDecodeError as error:
                answer = {"ok": False, "error": f"bad request: {error}"}
            except Exception:  # an answer, never a dead connection: the node stays up
                answer = {"ok": False, "error": traceback.format_exc()}
            self.wfile.write(json.dumps(answer).encode() + b"\n")

    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the node until asked to stop (or SIGTERM)."""
    parser = argparse.ArgumentParser(prog="gluon-kube node")
    parser.add_argument("state", type=Path)
    parser.add_argument("--backoff-base", type=float, default=10.0)
    args = parser.parse_args(argv)
    state: Path = args.state
    network = Network()
    runtime = NodeRuntime(state, network)
    uid, gid = host_ids()
    # Everything discovered (a new tool can't be dropped here), with the node's own ids.
    tools = replace(HostTools.discover(), uid=uid, gid=gid, cache=state / "images", state=state)
    cluster = Cluster(runtime, state, time.monotonic, tools, args.backoff_base)
    stopping = threading.Event()
    node = Node(cluster, stopping.set)
    socket_path = state / SOCKET
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()
    server = serve(node, socket_path)
    egress = EgressProxy(service_resolver(cluster), state / EGRESS_SOCKET)
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    print(f"node up: pid {os.getpid()}, state {state}", flush=True)
    while not stopping.is_set():
        try:
            cluster.tick()
        except Exception:  # logged; the loop, and with it every pod, keeps going
            traceback.print_exc()
        stopping.wait(TICK_S)
    server.shutdown()
    egress.close()
    cluster.shutdown()
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()
    print("node down", flush=True)
    return 0


__all__ = ["SandboxHandle", "main"]

if __name__ == "__main__":
    sys.exit(main())
