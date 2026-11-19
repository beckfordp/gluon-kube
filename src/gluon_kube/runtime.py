# SPDX-License-Identifier: MIT

"""Pods and containers as processes in namespaces (ADR 0030 decision 3; GK-4.2 to GK-4.5).

Runs inside the node namespace (uid 0 with every capability in a user namespace the
sandbox user owns). A pod is a *sandbox*: a `sleep` in its own network, UTS, IPC and pid
namespaces, with the pod's IP and hostname, like Kubernetes' pause container. Each
container joins the sandbox's network, UTS and IPC namespaces (`nsenter`) and gets its
own mount and pid namespace from a nested bwrap: its root (`rootfs`), its image's user
(a user namespace of its own when that isn't root), the env Kubernetes gives it, and its
command as pid 1 (`--as-pid-1`, as Docker runs an entrypoint), so SIGTERM reaches it.
"""

import contextlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gluon_kube.controller import ContainerRecord, PodRecord
from gluon_kube.images import Recipe
from gluon_kube.model import ConfigMapVolume, ExecAction, HttpGet, Probe, TcpSocket
from gluon_kube.network import Network
from gluon_kube.pods import POD_NETWORK, RESOLV_CONF, resolve_port
from gluon_kube.proxy import PortProxy
from gluon_kube.rootfs import Bind, Mount, root_args

KILL_WAIT_S = 10.0


def child_pid(info_fd: int) -> int:
    """The pid of bwrap's command, from its --info-fd JSON (written over several lines;
    bwrap closes the fd once written)."""
    data = b""
    with os.fdopen(info_fd, "rb") as info:
        while chunk := info.read1(4096):
            data += chunk
            with contextlib.suppress(json.JSONDecodeError):
                return int(json.loads(data)["child-pid"])
    raise OSError(f"bwrap wrote no child pid: {data!r}")


def spawn(argv: Sequence[str], log: Path | None) -> tuple[subprocess.Popen[bytes], int]:
    """Starts a bwrap command line with an --info-fd; the process and its command's pid."""
    info_r, info_w = os.pipe()
    args = [arg if arg != "{info}" else str(info_w) for arg in argv]
    out = log.open("ab") if log is not None else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            pass_fds=(info_w,),
            start_new_session=True,
        )
    finally:
        os.close(info_w)
        if log is not None and not isinstance(out, int):
            out.close()
    try:
        return process, child_pid(info_r)
    except OSError:
        process.kill()
        process.wait()
        raise


@dataclass(slots=True)
class SandboxHandle:
    """A pod's namespaces: the pause process, its pid, the pod's IP and veth slot."""

    ip: str
    pid: int
    slot: int
    process: subprocess.Popen[bytes]


@dataclass(slots=True)
class ContainerProcess:
    """A container: bwrap (`process`) and its command (pid 1 inside, `pid` outside)."""

    process: subprocess.Popen[bytes]
    pid: int

    def poll(self) -> int | None:
        """The command's exit code (bwrap passes it on), or None while it runs."""
        return self.process.poll()

    def terminate(self) -> None:
        """SIGTERM to the command, as Kubernetes stops a container."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        """SIGKILL to the command (pid 1: the kernel ends its whole pid namespace) and
        bwrap; waits for them to go, so a restart finds its ports free."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=KILL_WAIT_S)


def enter_argv(sandbox: int, pid: int, nested_user: bool, outside: bool = False) -> list[str]:
    """nsenter into a container as its user: first the pod sandbox's network, UTS and IPC
    namespaces (owned by the node's user namespace, which a caller from `outside` the
    node joins too, as its root), then the container's mount and pid namespaces (and its
    own user namespace when it has one). One nsenter can't do both: once in a container's
    user namespace, the node's namespaces are out of reach."""
    first = ["nsenter", "-t", str(sandbox)]
    if outside:
        first += ["--user", "--preserve-credentials"]
    first += ["--net", "--uts", "--ipc", "--"]
    second = ["nsenter", "-t", str(pid), "--mount", "--pid"]
    if nested_user:
        second += ["--user", "--preserve-credentials"]
    return [*first, *second]


def set_up(pid: int) -> bool:
    """Whether a container's process has finished bwrap's setup and exec'd its command:
    bwrap reports the pid before the child has built its mount namespace, and entering it
    earlier finds a half-made root."""
    try:
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
    except OSError:
        return False
    return bool(argv0) and Path(argv0.decode(errors="replace")).name != "bwrap"


def env_argv(env: dict[str, str], workdir: str, argv: Sequence[str]) -> list[str]:
    """`env -i` with a container's env and working directory, then `argv`."""
    return ["env", "-i", "-C", workdir, *(f"{k}={v}" for k, v in env.items()), *argv]


class NodeRuntime:
    """The controller's Runtime, for real (inside the node namespace)."""

    def __init__(self, state: Path, network: Network) -> None:
        self.state = state
        self.network = network
        self.slots: set[int] = set()
        self.next_slot = 0
        self.proxies: dict[tuple[str, int], PortProxy] = {}
        self.lock = threading.Lock()
        (state / "resolv.conf").write_text(RESOLV_CONF)

    # --- sandboxes ------------------------------------------------------------------

    def _slot(self) -> int:
        """The next free pod address (host part 2..254), round robin, so an IP isn't
        reused right after its pod went (host-local IPAM's behaviour)."""
        with self.lock:
            hosts = POD_NETWORK.num_addresses - 3
            for offset in range(hosts):
                slot = (self.next_slot + offset) % hosts
                if slot not in self.slots:
                    self.slots.add(slot)
                    self.next_slot = slot + 1
                    return slot
        raise OSError("no pod IP left")

    def start_sandbox(self, pod: PodRecord) -> SandboxHandle:
        """The pause process in new network, UTS, IPC and pid namespaces, wired up."""
        (pod.directory / "hostname").write_text(pod.name + "\n")
        argv = [
            "bwrap",
            "--unshare-net",
            "--unshare-uts",
            "--unshare-ipc",
            "--unshare-pid",
            "--hostname",
            pod.name,
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--info-fd",
            "{info}",
            "--",
            "sleep",
            "infinity",
        ]
        slot = self._slot()
        process, pid = spawn(argv, None)
        ip = str(POD_NETWORK.network_address + 2 + slot)
        try:
            self.network.wire(slot, pid, ip)
        except Exception:
            process.kill()
            process.wait()
            self.slots.discard(slot)
            raise
        return SandboxHandle(ip, pid, slot, process)

    def stop_sandbox(self, pod: PodRecord) -> None:
        """Ends the pause process (its namespaces go with it) and unwires the pod."""
        sandbox = pod.sandbox
        if not isinstance(sandbox, SandboxHandle):
            return
        sandbox.process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            sandbox.process.wait(timeout=KILL_WAIT_S)
        self.network.unwire(sandbox.slot)
        with self.lock:
            self.slots.discard(sandbox.slot)

    # --- containers -----------------------------------------------------------------

    def mounts(self, pod: PodRecord, container: ContainerRecord, recipe: Recipe) -> list[Mount]:
        """What a container's root has on top of the host's: the image's files (writable
        ones copied fresh, a new container's writable layer), the pod's volumes, and the
        kubelet's /etc/hosts, /etc/hostname and /etc/resolv.conf."""
        result: list[Mount] = list(recipe.mounts)
        layers = pod.directory / "layers" / container.name
        shutil.rmtree(layers, ignore_errors=True)
        for index, copy in enumerate(recipe.copies):
            target = layers / str(index)
            if copy.source.is_dir():
                shutil.copytree(copy.source, target, symlinks=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(copy.source, target)
            result.append(Bind(target, copy.target, writable=True))
        volumes = {volume.name: volume for volume in pod.spec.volumes}
        for mount in container.spec.volume_mounts:
            volume = volumes[mount.name]
            read_only = mount.read_only or isinstance(volume.source, ConfigMapVolume)
            source = pod.directory / "volumes" / mount.name
            result.append(Bind(source, mount.mount_path, writable=not read_only))
        result += [
            Bind(pod.hosts_path, "/etc/hosts"),
            Bind(pod.directory / "hostname", "/etc/hostname"),
            Bind(self.state / "resolv.conf", "/etc/resolv.conf"),
        ]
        return result

    def start_container(self, pod: PodRecord, container: ContainerRecord) -> ContainerProcess:
        """The container's bwrap, joined to the pod's sandbox, logging to its log file."""
        recipe = container.recipe
        if not isinstance(recipe, Recipe) or not isinstance(pod.sandbox, SandboxHandle):
            raise OSError(f"{pod.name}/{container.name} can't start")
        argv = ["nsenter", "-t", str(pod.sandbox.pid), "--net", "--uts", "--ipc", "--"]
        argv += ["bwrap", "--unshare-pid", "--as-pid-1", "--die-with-parent"]
        if (recipe.uid, recipe.gid) != (0, 0):
            argv += ["--unshare-user", "--uid", str(recipe.uid), "--gid", str(recipe.gid)]
        argv += root_args(self.mounts(pod, container, recipe))
        argv.append("--clearenv")
        for key, value in container.env.items():
            argv += ["--setenv", key, value]
        argv += ["--chdir", recipe.config.working_dir, "--info-fd", "{info}", "--"]
        argv += container.argv
        container.log.parent.mkdir(parents=True, exist_ok=True)
        process, pid = spawn(argv, container.log)
        return ContainerProcess(process, pid)

    # --- probes ---------------------------------------------------------------------

    def probe(self, pod: PodRecord, container: ContainerRecord, probe: Probe) -> bool:
        """One run of a probe against a running container, within its timeout."""
        handler, timeout = probe.handler, float(probe.timeout_seconds)
        ports = container.spec.ports
        match handler:
            case HttpGet():
                port = resolve_port(handler.port, ports)
                if port is None or pod.ip is None:
                    return False
                connection = http.client.HTTPConnection(pod.ip, port, timeout=timeout)
                try:
                    connection.request("GET", handler.path, headers={"User-Agent": "kube-probe"})
                    status = connection.getresponse().status
                except (OSError, http.client.HTTPException):
                    return False
                finally:
                    connection.close()
                return 200 <= status < 400
            case TcpSocket():
                port = resolve_port(handler.port, ports)
                if port is None or pod.ip is None:
                    return False
                try:
                    socket.create_connection((pod.ip, port), timeout=timeout).close()
                except OSError:
                    return False
                return True
            case ExecAction():
                process = container.process
                recipe = container.recipe
                if not isinstance(process, ContainerProcess) or not isinstance(recipe, Recipe):
                    return False
                sandbox = pod.sandbox
                if not isinstance(sandbox, SandboxHandle) or not set_up(process.pid):
                    return False
                nested = (recipe.uid, recipe.gid) != (0, 0)
                argv = [*enter_argv(sandbox.pid, process.pid, nested), "--"]
                argv += env_argv(container.env, recipe.config.working_dir, handler.command)
                try:
                    done = subprocess.run(argv, capture_output=True, timeout=timeout)
                except subprocess.TimeoutExpired:
                    return False
                return done.returncode == 0

    # --- Services -------------------------------------------------------------------

    def set_service(
        self, name: str, ip: str, ports: Sequence[tuple[int, Sequence[tuple[str, int]]]]
    ) -> None:
        """The ClusterIP on the node, a proxy per port."""
        self.network.add_address(ip)
        for port, endpoints in ports:
            proxy = self.proxies.get((ip, port))
            if proxy is None:
                proxy = self.proxies[(ip, port)] = PortProxy(ip, port)
            proxy.set_endpoints(endpoints)

    def remove_service(self, name: str, ip: str) -> None:
        """Its proxies closed, its ClusterIP removed."""
        for (proxy_ip, port), proxy in list(self.proxies.items()):
            if proxy_ip == ip:
                proxy.close()
                del self.proxies[(proxy_ip, port)]
        self.network.remove_address(ip)

    def write_file(self, path: Path, text: str) -> None:
        """In place: a bind mount of it sees the new content (GK-1.5)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as file:
            file.seek(0)
            file.truncate()
            file.write(text)
