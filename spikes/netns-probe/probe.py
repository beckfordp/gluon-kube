# /// script
# requires-python = "==3.13.*"
# dependencies = ["pyroute2==0.9.7"]
# ///
# SPDX-License-Identifier: MIT

"""GK-1 feasibility probe (gluon ADR 0030 decision 3; track netns-probe_20261115).

Can the sandbox give each pod its own network and mount namespace, route between
pods through a node namespace, serve Services from a userspace proxy and resolve
their names from a per-pod /etc/hosts, all unprivileged? Each check, GK-1.1 to 1.9
(the track's spec.md) runs in its own process group with a deadline, in a work
directory under /tmp/gk1-probe.*, and prints one result line:

    ok   GK-1.N <what>
    FAIL GK-1.N <what>: <why>

followed by its evidence, indented. A summary line ends the run ("PASS: …" or
"FAIL: …"); the exit code is 1 if any check failed. The work directory is
removed, whatever happens. gluon bin/verify-complete-GK-1.sh reads this output.

Usage:
  uv run probe.py               every check
  uv run probe.py 3 4           only GK-1.3 and GK-1.4
"""

import argparse
import contextlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MARKER = "gk1-probe"
"""In the work directory's name, so every process the probe starts has it in its
command line or working directory (verify-complete-GK-1.sh criterion 5)."""

CHECK_TIMEOUT_S = 120.0
EVIDENCE_INDENT = "     | "


class CheckError(Exception):
    """A check's expectation wasn't met; the message says which and why, `evidence`
    what was seen."""

    def __init__(self, why: str, evidence: str = "") -> None:
        super().__init__(why)
        self.evidence = evidence


@dataclass(frozen=True, slots=True)
class Check:
    """One GK-1 check: its number, what it shows, and the function that shows it.

    The function gets a work directory of its own and returns its evidence (the
    commands it ran and what they printed), or raises CheckError.
    """

    number: int
    what: str
    run: Callable[[Path], str]


CAP_NET_ADMIN = 12
"""Its bit in /proc/<pid>/status CapEff (linux/capability.h)."""
IFF_UP = 0x1
"""In /sys/class/net/<if>/flags (linux/if.h)."""


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
"""A user and network namespace, uid 0 and every capability inside it, the host's
filesystem as it is. --die-with-parent: it ends with the process that started it."""


def node(script: str, work: Path) -> str:
    """Runs a shell script inside a fresh node namespace; returns its output."""
    done = subprocess.run(
        [*NODE_NAMESPACE, "sh", "-c", script],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if done.returncode != 0:
        raise CheckError(f"node namespace failed (exit {done.returncode})", done.stderr)
    return done.stdout


def interfaces(proc_net_dev: str) -> list[str]:
    """The interface names in a /proc/net/dev listing."""
    return [line.split(":", 1)[0].strip() for line in proc_net_dev.splitlines()[2:]]


def node_namespace(work: Path) -> str:
    """GK-1.1: a user and network namespace where we hold CAP_NET_ADMIN, lo up, and
    none of the host's interfaces visible."""
    host = interfaces(Path("/proc/net/dev").read_text())
    out = node(
        "grep CapEff /proc/self/status; echo flags $(cat /sys/class/net/lo/flags);"
        " echo ifaces $(tail -n +3 /proc/net/dev | cut -d: -f1)",
        work,
    )
    evidence = f"host interfaces: {' '.join(host)}\nnode:\n{out}"
    fields = dict(line.split(None, 1) for line in out.splitlines() if line.strip())
    cap_eff = int(fields.get("CapEff:", "0"), 16)
    lo_flags = int(fields.get("flags", "0"), 16)
    inside = fields.get("ifaces", "").split()
    problems = []
    if not cap_eff >> CAP_NET_ADMIN & 1:
        problems.append(f"CAP_NET_ADMIN not in CapEff {cap_eff:#x}")
    if not lo_flags & IFF_UP:
        problems.append(f"lo not up (flags {lo_flags:#x})")
    if inside != ["lo"]:
        problems.append(f"interfaces inside are {inside}, not just lo")
    if problems:
        raise CheckError("; ".join(problems), evidence)
    return evidence


def in_node(scenario: str, work: Path, timeout_s: float = 90.0) -> str:
    """Runs `scenario` (SCENARIOS) in a fresh node namespace; returns its evidence."""
    argv = [*NODE_NAMESPACE, sys.executable, str(Path(__file__).resolve())]
    argv += ["--in-node", scenario, "--work", str(work)]
    done = subprocess.run(
        argv,
        cwd=work,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_s,
    )
    passed, why, evidence = outcome(done.returncode, done.stdout)
    if not passed:
        raise CheckError(why, evidence)
    return evidence


def pod_networking(work: Path) -> str:
    """GK-1.2: from the node, a pod's network namespace wired to a node bridge by a
    veth pair, with its own IP and a default route; the pod reaches the node over TCP."""
    return in_node("pod-networking", work)


POD_NETWORK = "10.244.0"
"""Kubernetes' default pod CIDR, 10.244.0.0/24 here; the node's bridge is .1."""
BRIDGE = "cbr0"
POD_NAMESPACE = ("bwrap", "--unshare-net", "--unshare-pid", "--die-with-parent")
"""A pod: its own network, mount and pid namespace, started from the node (nested bwrap),
on a root of its own (pod_root). The pid namespace makes the pod one unit, as a
container is: when its first process goes, the kernel ends every process in it, daemons
too (pg_ctl's server outlived a pod without it, GK-1.8); --die-with-parent ends the pod
with the node."""


def pod_root(host_root: Path = Path("/")) -> list[str]:
    """bwrap arguments for a pod's root: a tmpfs with the host's top-level entries bound
    into it (symlinks recreated, /dev and /proc as usual, /tmp its own), so a pod can have paths the
    host doesn't, such as /connectors, without writing to the host's /."""
    args: list[str] = []
    for entry in sorted(host_root.iterdir()):
        target = f"/{entry.name}"
        if entry.is_symlink():
            args += ["--symlink", os.readlink(entry), target]
        elif entry.name == "dev":
            args += ["--dev-bind", "/dev", "/dev"]
        elif entry.name == "proc":
            args += ["--proc", "/proc"]
        elif entry.name == "tmp":
            args += ["--tmpfs", "/tmp"]  # a container's /tmp is its own
        elif os.access(entry, os.R_OK | os.X_OK if entry.is_dir() else os.R_OK):
            args += ["--bind", str(entry), target]
    return args


@dataclass(frozen=True, slots=True)
class Mount:
    """A host file or directory at an absolute path in one pod (a ConfigMap volume is
    read-only, an emptyDir writable), or with no source an empty tmpfs: needed as the
    parent of a target the host's directory lacks (/var/lib/postgresql/data), as a
    missing mount point can't be created in a directory bound from the host."""

    source: Path | None
    target: str
    writable: bool = False

    def args(self) -> list[str]:
        """Its bwrap arguments."""
        if self.source is None:
            return ["--tmpfs", self.target]
        return ["--bind" if self.writable else "--ro-bind", str(self.source), self.target]


@dataclass(slots=True)
class Pod:
    """A pod the node started: its name, IP, bwrap process and the pid inside."""

    name: str
    ip: str
    process: subprocess.Popen[bytes]
    pid: int
    log: Path

    def output(self) -> str:
        """What the pod's command printed so far."""
        return self.log.read_text(errors="replace")


class Node:
    """The node side (inside the node namespace): a bridge, and pods wired to it.

    Kubernetes' shape (ADR 0030 decision 3, revised): each pod its own network
    namespace and IP, one veth pair per pod onto the node's bridge, the node as the
    pods' default gateway. Everything is configured from the node over netlink
    (pyroute2), since the image has no `ip`.
    """

    def __init__(self, work: Path) -> None:
        from pyroute2 import IPRoute  # type: ignore[import-untyped]  # no type information

        self.work = work
        self.ipr = IPRoute()
        self.pods: list[Pod] = []
        self.ipr.link("add", ifname=BRIDGE, kind="bridge")
        self.bridge = self.ipr.link_lookup(ifname=BRIDGE)[0]
        self.ipr.addr("add", index=self.bridge, address=f"{POD_NETWORK}.1", prefixlen=24)
        self.ipr.link("set", index=self.bridge, state="up")

    def start_pod(
        self,
        name: str,
        host: int,
        command: Sequence[str],
        *,
        mounts: Sequence[Mount] = (),
        hosts: Path | None = None,
        env: Mapping[str, str] | None = None,
        uid: int | None = None,
    ) -> Pod:
        """Starts `command` in a new pod at POD_NETWORK.<host>, once its network is set,
        with `mounts`, `hosts` as its /etc/hosts, `env` added, and, given a `uid`, as
        that user in a user namespace of its own (as an image's USER)."""
        ready = self.work / f"{name}.ready"
        log = self.work / f"{name}.log"
        info_r, info_w = os.pipe()
        wait_then_run = f'while [ ! -e "{ready}" ]; do sleep 0.05; done; exec "$@"'
        layout = pod_root()
        layout += Mount(self.work, str(self.work), writable=True).args()  # the probe's files
        for mount in mounts:
            layout += mount.args()
        if hosts is not None:
            layout += Mount(hosts, "/etc/hosts").args()
        for key, value in (env or {}).items():
            layout += ["--setenv", key, value]
        if uid is not None:
            layout += ["--unshare-user", "--uid", str(uid), "--gid", str(uid)]
        bwrap = [*POD_NAMESPACE, *layout, "--info-fd", str(info_w), "--chdir", str(self.work)]
        with log.open("wb") as out:
            process = subprocess.Popen(
                [*bwrap, "sh", "-c", wait_then_run, name, *command],
                cwd=self.work,
                stdout=out,
                stderr=subprocess.STDOUT,
                pass_fds=(info_w,),
            )
        os.close(info_w)
        pid = child_pid(info_r)
        pod = Pod(name, f"{POD_NETWORK}.{host}", process, pid, log)
        self._wire(pod)
        ready.touch()
        self.pods.append(pod)
        return pod

    def _wire(self, pod: Pod) -> None:
        """A veth pair: the node end on the bridge, the other end the pod's eth0."""
        outside, inside = f"veth-{pod.name}"[:15], f"tmp-{pod.name}"[:15]
        self.ipr.link("add", ifname=outside, kind="veth", peer=inside)
        self.ipr.link("set", index=self.ipr.link_lookup(ifname=inside)[0], net_ns_pid=pod.pid)
        end = self.ipr.link_lookup(ifname=outside)[0]
        self.ipr.link("set", index=end, master=self.bridge, state="up")
        in_namespace_of(pod.pid, inside, pod.ip)

    def stop(self) -> None:
        """Ends every pod."""
        for pod in self.pods:
            pod.process.kill()
            pod.process.wait()


def child_pid(info_fd: int) -> int:
    """The pid inside the namespace, from bwrap's --info-fd JSON (written over
    several lines; bwrap closes the fd once it's written)."""
    data = b""
    with os.fdopen(info_fd, "rb") as info:
        while chunk := info.read1(4096):
            data += chunk
            with contextlib.suppress(json.JSONDecodeError):
                return int(json.loads(data)["child-pid"])
    raise CheckError(f"bwrap wrote no child pid: {data!r}")


def configure_pod(interface: str, ip: str) -> None:
    """Inside a pod's network namespace: eth0 with its IP, lo up, the node as gateway."""
    from pyroute2 import IPRoute

    ipr = IPRoute()
    index = ipr.link_lookup(ifname=interface)[0]
    ipr.link("set", index=index, ifname="eth0")
    ipr.addr("add", index=index, address=ip, prefixlen=24)
    ipr.link("set", index=index, state="up")
    ipr.link("set", index=ipr.link_lookup(ifname="lo")[0], state="up")
    ipr.route("add", dst="0.0.0.0/0", gateway=f"{POD_NETWORK}.1")


def in_namespace_of(pid: int, interface: str, ip: str) -> None:
    """Configures a pod from a child process that joins `pid`'s network namespace.

    A child process, not a fork: the node runs threads (the Service proxies), and
    fork() in a threaded process can deadlock.
    """
    argv = [sys.executable, str(Path(__file__).resolve()), "--configure-pod", str(pid)]
    # 60 s: the helper's pyroute2 import alone takes ~1.7 s, more on a busy node.
    done = subprocess.run([*argv, interface, ip], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise CheckError(f"configuring the namespace of pid {pid} failed", done.stderr)


def configure_pod_in(pid: int, interface: str, ip: str) -> int:
    """The --configure-pod side: join `pid`'s network namespace, then configure_pod."""
    os.setns(os.open(f"/proc/{pid}/ns/net", os.O_RDONLY), os.CLONE_NEWNET)
    configure_pod(interface, ip)
    return 0


def finish(pod: Pod, timeout_s: float, also: Pod | None = None) -> None:
    """Waits for `pod` to exit; if it doesn't, a CheckError with its output (and
    `also`'s, the pod it was talking to)."""
    try:
        pod.process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        evidence = f"pod {pod.name}:\n{pod.output()}"
        if also is not None:
            evidence += f"pod {also.name}:\n{also.output()}"
        raise CheckError(f"pod {pod.name} didn't finish in {timeout_s:.0f} s", evidence) from None


def wait_for(condition: Callable[[], bool], what: str, timeout_s: float = 10.0) -> None:
    """Polls `condition` until it holds, or raises CheckError naming `what`."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise CheckError(f"timed out waiting for {what}")
        time.sleep(0.05)


def scenario_pod_networking(work: Path) -> str:
    """GK-1.2 inside the node: one pod connects to a listener on the node's bridge IP."""
    node = Node(work)
    try:
        with socket.socket() as server:
            server.bind((f"{POD_NETWORK}.1", 9000))
            server.listen()
            server.settimeout(10)
            pod = node.start_pod(
                "a",
                2,
                [
                    "sh",
                    "-c",
                    "echo interfaces $(tail -n +3 /proc/net/dev | cut -d: -f1);"
                    " python3 -c 'import socket; s = socket.create_connection("
                    f'("{POD_NETWORK}.1", 9000), timeout=5); print("pod got:", s.recv(64).decode())'
                    "'",
                ],
            )
            try:
                connection, peer = server.accept()
            except TimeoutError:
                pod.process.wait(timeout=10)
                raise CheckError("no connection from the pod", f"pod a:\n{pod.output()}") from None
            with connection:
                connection.sendall(b"hello from node")
        pod.process.wait(timeout=10)
        evidence = f"node accepted a connection from {peer[0]}\npod a:\n{pod.output()}"
        if peer[0] != pod.ip:
            raise CheckError(f"the connection came from {peer[0]}, not {pod.ip}", evidence)
        if "pod got: hello from node" not in pod.output():
            raise CheckError("the pod didn't get the node's answer", evidence)
        if "interfaces lo eth0" not in pod.output():
            raise CheckError("the pod's interfaces aren't lo and eth0", evidence)
        return evidence
    finally:
        node.stop()


def same_port(work: Path) -> str:
    """GK-1.3: two pods both listen on 0.0.0.0:8080; a third reaches each at its own IP."""
    return in_node("same-port", work)


NAME_SERVER = """\
import socket, sys
name, port = sys.argv[1], int(sys.argv[2])
with socket.socket() as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen()
    print(f"{name} listening on 0.0.0.0:{port}", flush=True)
    while True:
        connection, _ = server.accept()
        with connection:
            connection.sendall(name.encode())
"""
"""A pod's workload: answers every connection with its own name, on 0.0.0.0:<port>."""

ASK = """\
import socket, sys, time
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    deadline, answer = time.monotonic() + 10, None
    while answer is None:
        try:
            with socket.create_connection((host, int(port)), timeout=2) as s:
                answer = s.recv(64).decode()
        except OSError as error:
            if time.monotonic() > deadline:
                answer = f"error {error}"
            else:
                time.sleep(0.1)
    print(f"{target} answered {answer}", flush=True)
"""
"""A client pod: connects to each host:port given (retrying until it listens) and
prints what answered."""


def python_pod(work: Path, name: str, source: str, *args: str) -> list[str]:
    """The command line for a pod running `source` (written to the work dir) with `args`."""
    script = work / f"{name}.py"
    script.write_text(source)
    return ["python3", str(script), *args]


def scenario_same_port(work: Path) -> str:
    """GK-1.3 inside the node: pods a and b on 0.0.0.0:8080, pod c asks each pod IP."""
    node = Node(work)
    try:
        a = node.start_pod("a", 2, python_pod(work, "serve-a", NAME_SERVER, "a", "8080"))
        b = node.start_pod("b", 3, python_pod(work, "serve-b", NAME_SERVER, "b", "8080"))
        c = node.start_pod("c", 4, python_pod(work, "ask", ASK, f"{a.ip}:8080", f"{b.ip}:8080"))
        c.process.wait(timeout=30)
        evidence = "\n".join(f"pod {p.name} ({p.ip}):\n{p.output()}" for p in (a, b, c))
        for pod in (a, b):
            if f"{pod.ip}:8080 answered {pod.name}" not in c.output():
                raise CheckError(f"{pod.ip}:8080 didn't answer {pod.name}", evidence)
        return evidence
    finally:
        node.stop()


def service_proxy(work: Path) -> str:
    """GK-1.4: a ClusterIP served by a userspace proxy in the node forwards to a pod, and
    refuses connections once it has no endpoint."""
    return in_node("service-proxy", work)


SERVICE_NETWORK = "10.96.0"
"""Kubernetes' default Service CIDR starts at 10.96.0.0; ClusterIPs live on the node."""


class ServiceProxy:
    """A Service's ClusterIP:port, served in the node (kube-proxy's userspace mode).

    Each connection is forwarded to an endpoint (a ready pod's IP:port). With no
    endpoint the proxy stops listening, so a caller's connect is refused, as with a
    Kubernetes Service that has no ready pods.
    """

    def __init__(self, node: Node, cluster_ip: str, port: int) -> None:
        # On the node's lo: any local address is delivered locally whichever interface
        # a packet arrives on. (This kernel has no `dummy` link type: EOPNOTSUPP.)
        index = node.ipr.link_lookup(ifname="lo")[0]
        node.ipr.addr("add", index=index, address=cluster_ip, prefixlen=32)
        self.address = (cluster_ip, port)
        self.endpoints: list[tuple[str, int]] = []
        self.listener: socket.socket | None = None
        self.forwarded = 0

    def set_endpoints(self, endpoints: Sequence[tuple[str, int]]) -> None:
        """Forward to `endpoints` from now on; none means refuse."""
        self.endpoints = list(endpoints)
        if self.endpoints and self.listener is None:
            self.listener = socket.create_server(self.address, reuse_port=False)
            threading.Thread(target=self._accept, args=(self.listener,), daemon=True).start()
        elif not self.endpoints and self.listener is not None:
            # shutdown, not just close: it stops the listen and wakes the blocked
            # accept() (close alone leaves the kernel socket listening until then).
            with contextlib.suppress(OSError):
                self.listener.shutdown(socket.SHUT_RDWR)
            self.listener.close()
            self.listener = None

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            endpoints = self.endpoints
            if not endpoints:
                client.close()
                continue
            endpoint = endpoints[self.forwarded % len(endpoints)]
            self.forwarded += 1
            threading.Thread(target=self._forward, args=(client, endpoint), daemon=True).start()

    def _forward(self, client: socket.socket, endpoint: tuple[str, int]) -> None:
        try:
            upstream = socket.create_connection(endpoint, timeout=5)
        except OSError:
            client.close()
            return
        for source, sink in ((client, upstream), (upstream, client)):
            threading.Thread(target=pipe, args=(source, sink), daemon=True).start()


def pipe(source: socket.socket, sink: socket.socket) -> None:
    """Copies `source` to `sink` until `source` ends, then ends `sink`'s sending side."""
    with contextlib.suppress(OSError):
        while data := source.recv(65536):
            sink.sendall(data)
    with contextlib.suppress(OSError):
        sink.shutdown(socket.SHUT_WR)


ASK_SERVICE = """\
import pathlib, socket, sys, time
service, work = sys.argv[1], pathlib.Path(sys.argv[2])
host, port = service.rsplit(":", 1)
deadline, answer = time.monotonic() + 10, None
while answer is None:
    try:
        with socket.create_connection((host, int(port)), timeout=2) as s:
            answer = s.recv(64).decode()
    except OSError as error:
        if time.monotonic() > deadline:
            answer = f"error {error}"
        else:
            time.sleep(0.1)
print(f"with an endpoint: {service} answered {answer}", flush=True)
(work / "asked").touch()
while not (work / "removed").exists():
    time.sleep(0.05)
started = time.monotonic()
try:
    socket.create_connection((host, int(port)), timeout=5).close()
    result = "connected"
except ConnectionRefusedError:
    result = "refused"
except OSError as error:
    result = f"error {error}"
print(f"without one: {result} after {time.monotonic() - started:.3f} s", flush=True)
"""
"""Pod c: asks the Service, waits for the node to remove its endpoint, connects again."""


def scenario_service_proxy(work: Path) -> str:
    """GK-1.4 inside the node: Service 10.96.0.10:8080 to pod a; then no endpoint."""
    node = Node(work)
    try:
        a = node.start_pod("a", 2, python_pod(work, "serve-a", NAME_SERVER, "a", "8080"))
        service = ServiceProxy(node, f"{SERVICE_NETWORK}.10", 8080)
        service.set_endpoints([(a.ip, 8080)])
        target = f"{SERVICE_NETWORK}.10:8080"
        c = node.start_pod("c", 4, python_pod(work, "ask", ASK_SERVICE, target, str(work)))
        wait_for(lambda: (work / "asked").exists(), "pod c's first answer", 20)
        service.set_endpoints([])
        (work / "removed").touch()
        c.process.wait(timeout=15)
        evidence = f"pod c ({c.ip}):\n{c.output()}"
        if f"with an endpoint: {target} answered a" not in c.output():
            raise CheckError(f"{target} didn't answer as pod a", evidence)
        if "without one: refused" not in c.output():
            raise CheckError(f"{target} wasn't refused once it had no endpoint", evidence)
        return evidence
    finally:
        node.stop()


def names(work: Path) -> str:
    """GK-1.5: a per-pod /etc/hosts maps Service names to ClusterIPs for glibc, Rust and
    Java; rewritten in place, the new address is seen by the running pod."""
    return in_node("names", work)


def mounts(work: Path) -> str:
    """GK-1.6: a directory and a file mounted at absolute paths in one pod only; the rest
    of the filesystem as on the host."""
    return in_node("mounts", work)


RESOLVE_RS = """\
use std::io::Read;
use std::net::{TcpStream, ToSocketAddrs};
fn main() {
    let target = std::env::args().nth(1).expect("host:port");
    let address = target.to_socket_addrs().expect("resolves").next().expect("an address");
    let mut answer = String::new();
    TcpStream::connect(address).expect("connects").read_to_string(&mut answer).expect("reads");
    println!("{answer} from {address}");
}
"""
"""Rust's resolver (ToSocketAddrs, getaddrinfo underneath), as the Rust ports use."""

RESOLVE_JAVA = """\
import java.io.InputStream;
import java.net.InetAddress;
import java.net.Socket;
public class Resolve {
    public static void main(String[] args) throws Exception {
        InetAddress address = InetAddress.getByName(args[0]);
        try (Socket socket = new Socket(address, Integer.parseInt(args[1]))) {
            InputStream in = socket.getInputStream();
            System.out.println(new String(in.readAllBytes()) + " from " + address);
        }
    }
}
"""
"""Java's resolver (InetAddress), as Kafka and Kafka Connect use."""

ASK_BY_NAME = """\
set -u
name=user-service
fqdn=user-service.gluon-local.svc.cluster.local
echo "glibc: $(getent hosts $name)"
echo "glibc fqdn: $(getent hosts $fqdn)"
echo "curl: $(curl -s --http0.9 --max-time 5 http://$name:8080/)"
echo "rust: $(./resolve $name:8080 2>&1)"
echo "java: $(java Resolve.java $name 8080 2>&1)"
echo "java fqdn: $(java Resolve.java $fqdn 8080 2>&1)"
touch asked
while [ ! -e rewritten ]; do sleep 0.05; done
echo "after rewrite glibc: $(getent hosts $name)"
echo "after rewrite curl: $(curl -s --http0.9 --max-time 5 http://$name:8080/)"
"""
"""Pod c: resolves the Service by name every way the workloads do, then again after the
node rewrites its /etc/hosts."""


def hosts_file(entries: Mapping[str, str]) -> str:
    """An /etc/hosts: localhost, then each Service's ClusterIP with its short name and
    `<name>.gluon-local.svc.cluster.local`."""
    lines = ["127.0.0.1 localhost"]
    lines += [f"{ip} {name} {name}.gluon-local.svc.cluster.local" for name, ip in entries.items()]
    return "\n".join(lines) + "\n"


def rewrite_in_place(path: Path, text: str) -> None:
    """Rewrites a file keeping its inode, so a bind mount of it shows the new content (a
    rename would leave the pod looking at the old file)."""
    with path.open("r+") as file:
        file.truncate(0)
        file.write(text)


def scenario_names(work: Path) -> str:
    """GK-1.5 inside the node: user-service -> 10.96.0.10 (pod a), then rewritten to
    10.96.0.11 (pod b) while pod c runs."""
    rustc = subprocess.run(
        ["rustc", "-O", "-o", str(work / "resolve"), "-"],
        input=RESOLVE_RS,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if rustc.returncode != 0:
        raise CheckError("rustc failed to build the resolver", rustc.stderr)
    (work / "Resolve.java").write_text(RESOLVE_JAVA)
    node = Node(work)
    try:
        a = node.start_pod("a", 2, python_pod(work, "serve-a", NAME_SERVER, "a", "8080"))
        b = node.start_pod("b", 3, python_pod(work, "serve-b", NAME_SERVER, "b", "8080"))
        first = ServiceProxy(node, f"{SERVICE_NETWORK}.10", 8080)
        first.set_endpoints([(a.ip, 8080)])
        second = ServiceProxy(node, f"{SERVICE_NETWORK}.11", 8080)
        second.set_endpoints([(b.ip, 8080)])
        hosts = work / "hosts-c"
        hosts.write_text(hosts_file({"user-service": f"{SERVICE_NETWORK}.10"}))
        (work / "ask.sh").write_text(ASK_BY_NAME)
        c = node.start_pod("c", 4, ["sh", str(work / "ask.sh")], hosts=hosts)
        wait_for(lambda: (work / "asked").exists(), "pod c's first answers", 60)
        rewrite_in_place(hosts, hosts_file({"user-service": f"{SERVICE_NETWORK}.11"}))
        (work / "rewritten").touch()
        c.process.wait(timeout=30)
        out = c.output()
        first_hosts = hosts_file({"user-service": f"{SERVICE_NETWORK}.10"})
        evidence = f"pod c's /etc/hosts, first:\n{first_hosts}pod c ({c.ip}):\n{out}"
        expected = [
            "glibc: 10.96.0.10      user-service user-service.gluon-local.svc.cluster.local",
            # getent prints the whole hosts entry: the canonical name first.
            "glibc fqdn: 10.96.0.10      user-service user-service.gluon-local.svc.cluster.local",
            "curl: a",
            "rust: a from 10.96.0.10:8080",
            "java: a from user-service/10.96.0.10",
            "java fqdn: a from user-service.gluon-local.svc.cluster.local/10.96.0.10",
            "after rewrite glibc: 10.96.0.11",
            "after rewrite curl: b",
        ]
        missing = [line for line in expected if line not in out]
        if missing:
            raise CheckError(f"pod c didn't show: {missing}", evidence)
        return evidence
    finally:
        node.stop()


def scenario_mounts(work: Path) -> str:
    """GK-1.6 inside the node: a ConfigMap-like directory at /connectors and a file at
    /jmx-config/metrics.yaml in pod a only; pod b, same command, doesn't see them."""
    connectors = work / "connectors"
    connectors.mkdir()
    (connectors / "user-outbox-connector.json").write_text('{"name": "user"}\n')
    metrics = work / "metrics.yaml"
    metrics.write_text("rules: []\n")
    look = [
        "sh",
        "-c",
        "echo connectors: $(ls /connectors 2>&1);"
        " echo jmx-config: $(cat /jmx-config/metrics.yaml 2>&1);"
        " echo usr: $(ls /usr/bin/python3 2>&1); echo workspace: $(ls -d /workspace 2>&1);"
        " touch /connectors/written 2>&1 | sed 's/^/write: /'",
    ]
    node = Node(work)
    try:
        mounted = [Mount(connectors, "/connectors"), Mount(metrics, "/jmx-config/metrics.yaml")]
        a = node.start_pod("a", 2, look, mounts=mounted)
        b = node.start_pod("b", 3, look)
        a.process.wait(timeout=15)
        b.process.wait(timeout=15)
        evidence = f"pod a (mounts):\n{a.output()}pod b (none):\n{b.output()}"
        problems = []
        if "connectors: user-outbox-connector.json" not in a.output():
            problems.append("pod a doesn't see /connectors")
        if "jmx-config: rules: []" not in a.output():
            problems.append("pod a doesn't see /jmx-config/metrics.yaml")
        if "Read-only file system" not in a.output():
            problems.append("pod a could write to its read-only /connectors")
        if "user-outbox-connector.json" in b.output() or "rules: []" in b.output():
            problems.append("pod b sees pod a's mounts")
        for pod in (a, b):
            out = pod.output()
            if "usr: /usr/bin/python3" not in out or "workspace: /workspace" not in out:
                problems.append(f"pod {pod.name} doesn't see the host's /usr and /workspace")
        if Path("/connectors").exists() or Path("/jmx-config").exists():
            problems.append("the mounts leaked onto the node's /")
        if problems:
            raise CheckError("; ".join(problems), evidence)
        return evidence
    finally:
        node.stop()


def start_in_node(scenario: str, work: Path) -> subprocess.Popen[str]:
    """Starts `scenario` in a fresh node namespace, in the background."""
    argv = [*NODE_NAMESPACE, sys.executable, str(Path(__file__).resolve())]
    argv += ["--in-node", scenario, "--work", str(work)]
    return subprocess.Popen(
        argv, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def node_result(node: subprocess.Popen[str], timeout_s: float = 60.0) -> str:
    """Waits for a background scenario; its evidence, or CheckError."""
    output, _ = node.communicate(timeout=timeout_s)
    passed, why, evidence = outcome(node.returncode, output)
    if not passed:
        raise CheckError(why, evidence)
    return evidence


class PortForward:
    """The outside end of a port-forward: 127.0.0.1:<port> on the host, each connection
    piped to the node's relay on a Unix socket (path sockets cross network namespaces)."""

    def __init__(self, relay: Path) -> None:
        self.relay = relay
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            upstream = socket.socket(socket.AF_UNIX)
            try:
                upstream.connect(str(self.relay))
            except OSError:
                client.close()
                continue
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=pipe, args=(source, sink), daemon=True).start()

    def close(self) -> None:
        """Stops listening; the port is free again."""
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()


def getting_in(work: Path) -> str:
    """GK-1.7: from outside every namespace, nsenter into a pod (its /etc/hosts, the
    Service by name), and a port-forward to a Service through a Unix socket."""
    node = start_in_node("getting-in", work)
    lines: list[str] = []
    try:
        wait_for(
            lambda: (work / "outside.json").exists() or node.poll() is not None,
            "the node to be ready for outside",
            60,
        )
        if node.poll() is not None:
            return node_result(node)
        pod_c = int(json.loads((work / "outside.json").read_text())["pod_c"])
        # --pid too, as kubectl exec: the pod's /proc belongs to its pid namespace.
        enter = ["nsenter", "-t", str(pod_c), "--user", "--net", "--mount", "--pid"]
        enter += ["--preserve-credentials", "--"]
        for command in (
            ["cat", "/etc/hosts"],
            ["getent", "hosts", "user-service"],
            ["curl", "-s", "--http0.9", "--max-time", "5", "http://user-service:8080/"],
            ["sh", "-c", "echo interfaces $(tail -n +3 /proc/net/dev | cut -d: -f1)"],
        ):
            done = subprocess.run([*enter, *command], capture_output=True, text=True, timeout=20)
            lines.append(f"$ nsenter … {' '.join(command)}  (exit {done.returncode})")
            lines += (done.stdout + done.stderr).rstrip("\n").splitlines()
        forward = PortForward(work / "relay.sock")
        curl = ["curl", "-s", "--http0.9", "--max-time", "5", f"http://127.0.0.1:{forward.port}/"]
        done = subprocess.run(curl, capture_output=True, text=True, timeout=20)
        lines.append(f"$ {' '.join(curl)}  (outside; exit {done.returncode})")
        lines.append(f"port-forward answered: {done.stdout}")
        forward.close()
        port_free = socket.socket().connect_ex(("127.0.0.1", forward.port)) != 0
        lines.append(f"port {forward.port} free after close: {port_free}")
    finally:
        (work / "outside-done").touch()
    node_evidence = node_result(node)
    evidence = "\n".join(lines) + "\n" + node_evidence
    text = "\n".join(lines)
    expected = [
        "10.96.0.10 user-service user-service.gluon-local.svc.cluster.local",
        "10.96.0.10      user-service",
        "interfaces lo eth0",
        "port-forward answered: a",
        "free after close: True",
    ]
    missing = [e for e in expected if e not in text]
    if text.count("exit 0)") != 5:
        missing.append("every command exiting 0")
    if missing:
        raise CheckError(f"from outside, didn't see: {missing}", evidence)
    return evidence


def scenario_getting_in(work: Path) -> str:
    """GK-1.7 inside the node: Service user-service -> pod a; pod c waiting (its pid for
    nsenter); a relay from a Unix socket to the Service (the port-forward's node end)."""
    node = Node(work)
    try:
        a = node.start_pod("a", 2, python_pod(work, "serve-a", NAME_SERVER, "a", "8080"))
        service = ServiceProxy(node, f"{SERVICE_NETWORK}.10", 8080)
        service.set_endpoints([(a.ip, 8080)])
        hosts = work / "hosts-c"
        hosts.write_text(hosts_file({"user-service": f"{SERVICE_NETWORK}.10"}))
        c = node.start_pod("c", 4, ["sleep", "120"], hosts=hosts)
        relay = socket.socket(socket.AF_UNIX)
        relay.bind(str(work / "relay.sock"))
        relay.listen()
        threading.Thread(target=serve_relay, args=(relay, service.address), daemon=True).start()
        (work / "outside.json").write_text(json.dumps({"pod_c": c.pid}))
        wait_for(lambda: (work / "outside-done").exists(), "the outside checks", 60)
        relay.close()
        return f"node: pod a {a.ip}, pod c {c.ip} (pid {c.pid}), relay {work / 'relay.sock'}"
    finally:
        node.stop()


def serve_relay(relay: socket.socket, target: tuple[str, int]) -> None:
    """The node end of a port-forward: each Unix-socket connection piped to `target`."""
    while True:
        try:
            client, _ = relay.accept()
        except OSError:
            return
        try:
            upstream = socket.create_connection(target, timeout=5)
        except OSError:
            client.close()
            continue
        for source, sink in ((client, upstream), (upstream, client)):
            threading.Thread(target=pipe, args=(source, sink), daemon=True).start()


def real_workload(work: Path) -> str:
    """GK-1.8: PostgreSQL 16 in a pod, set up as the postgres:16-alpine image does, reached
    from another pod as user-postgres:5432."""
    return in_node("postgres", work, timeout_s=110)


POSTGRES_PASSWD = "postgres:x:70:70:PostgreSQL:/var/lib/postgresql:/bin/sh\n"
POSTGRES_GROUP = "postgres:x:70:\n"
"""The image's own user and group (postgres:16-alpine: 70:70)."""

POSTGRES_ENTRYPOINT = """\
set -eu
PGDATA=/var/lib/postgresql/data
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  printf '%s\\n' "$POSTGRES_PASSWORD" > /run/postgresql/pwfile
  initdb -D "$PGDATA" -U "$POSTGRES_USER" --pwfile=/run/postgresql/pwfile \\
    --auth-local=trust --auth-host=scram-sha-256 > /dev/null
  rm /run/postgresql/pwfile
  echo "listen_addresses = '*'" >> "$PGDATA/postgresql.conf"
  echo "host all all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
  pg_ctl -D "$PGDATA" -o "-c listen_addresses='' -k /run/postgresql" -w start > /dev/null
  createdb -h /run/postgresql -U "$POSTGRES_USER" "$POSTGRES_DB"
  pg_ctl -D "$PGDATA" -m fast -w stop > /dev/null
  echo "initialised $PGDATA as $(id -u):$(id -g)"
fi
exec postgres -D "$PGDATA" -k /run/postgresql,/tmp "$@"
"""
"""postgres:16-alpine's entrypoint, as far as gluon-local uses it (the image's documented
POSTGRES_USER/PASSWORD/DB; listen on all addresses; password logins over TCP)."""

PSQL = """\
set -u
for i in $(seq 1 100); do
  out="$(PGPASSWORD=user psql -h user-postgres -U user user -tAc \\
    "select current_user, current_database(), current_setting('wal_level')" 2>&1)" && break
  sleep 0.2
done
echo "psql: $out"
echo "wrong password: $(PGPASSWORD=wrong psql -h user-postgres -U user user -tAc 'select 1' 2>&1)"
"""
"""Pod c: connects to user-postgres by Service name, retrying until it is up."""


def scenario_postgres(work: Path) -> str:
    """GK-1.8 inside the node: postgres-user.yaml's pod (uid 70, emptyDir data, args) behind
    Service user-postgres; pod c runs psql against it; the readiness probe's command run
    in pod a as `exec` would."""
    manifest_env = {"POSTGRES_DB": "user", "POSTGRES_USER": "user", "POSTGRES_PASSWORD": "user"}
    data = work / "data"
    data.mkdir()
    sockets = work / "run-postgresql"
    sockets.mkdir()
    (work / "entrypoint.sh").write_text(POSTGRES_ENTRYPOINT)
    # The image's own user (postgres, 70:70): initdb refuses a uid it can't look up.
    passwd, group = work / "passwd", work / "group"
    passwd.write_text(Path("/etc/passwd").read_text() + POSTGRES_PASSWD)
    group.write_text(Path("/etc/group").read_text() + POSTGRES_GROUP)
    node = Node(work)
    try:
        a = node.start_pod(
            "a",
            2,
            ["sh", str(work / "entrypoint.sh"), "-c", "wal_level=logical"],
            mounts=[
                Mount(None, "/var/lib/postgresql"),
                Mount(data, "/var/lib/postgresql/data", writable=True),
                Mount(sockets, "/run/postgresql", writable=True),
                Mount(passwd, "/etc/passwd"),
                Mount(group, "/etc/group"),
            ],
            env=manifest_env,
            uid=70,
        )
        service = ServiceProxy(node, f"{SERVICE_NETWORK}.10", 5432)
        service.set_endpoints([(a.ip, 5432)])
        hosts = work / "hosts-c"
        hosts.write_text(hosts_file({"user-postgres": f"{SERVICE_NETWORK}.10"}))
        (work / "psql.sh").write_text(PSQL)
        c = node.start_pod("c", 4, ["sh", str(work / "psql.sh")], hosts=hosts)
        finish(c, 60, also=a)
        enter = ["nsenter", "-t", str(a.pid), "--user", "--net", "--mount", "--pid"]
        probe = subprocess.run(
            [*enter, "--preserve-credentials", "--", "pg_isready", "-U", "user"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        readiness = f"readiness (exec pg_isready -U user in pod a): exit {probe.returncode} "
        readiness += probe.stdout.strip()
        evidence = f"pod a ({a.ip}, uid 70):\n{a.output()}pod c ({c.ip}):\n{c.output()}{readiness}"
        problems = []
        if "psql: user|user|logical" not in c.output():
            problems.append("psql by Service name didn't answer user|user|logical")
        if "password authentication failed" not in c.output():
            problems.append("a wrong password wasn't refused")
        if "initialised /var/lib/postgresql/data as 70:70" not in a.output():
            problems.append("Postgres didn't initialise as uid 70")
        if probe.returncode != 0:
            problems.append("the readiness command failed")
        if problems:
            raise CheckError("; ".join(problems), evidence)
        return evidence
    finally:
        node.stop()


DAEMON_MARKER = "31415926"
"""`sleep 31415926`: a daemonised process (setsid, as pg_ctl's server) in a pod, found by
its command line."""


def processes_holding(work: Path) -> list[str]:
    """Every process whose command line names `work` or the daemon marker, or whose
    working directory is in `work`: what a teardown must leave none of."""
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            cwd = os.readlink(proc / "cwd")
        except OSError:
            continue
        daemon = command.startswith(f"sleep {DAEMON_MARKER}")
        if str(work) in command or daemon or cwd.startswith(str(work)):
            found.append(f"{proc.name}: {command.strip()[:100]}")
    return found


def teardown(work: Path) -> str:
    """GK-1.9: killing the node ends every pod process, daemons included; the relay, the
    port-forward's port and the work directory are released."""
    node = start_in_node("teardown", work)
    lines: list[str] = []
    try:
        wait_for(
            lambda: (work / "outside.json").exists() or node.poll() is not None,
            "the node to start its pods",
            60,
        )
        if node.poll() is not None:
            return node_result(node)
        forward = PortForward(work / "relay.sock")
        curl = ["curl", "-s", "--http0.9", "--max-time", "5", f"http://127.0.0.1:{forward.port}/"]
        before = subprocess.run(curl, capture_output=True, text=True, timeout=20).stdout
        lines.append(f"before: port-forward answered {before!r}")
        running = processes_holding(work)
        lines.append(f"before: {len(running)} processes hold the work dir or the marker:")
        lines += [f"  {p}" for p in running]
        node.kill()  # SIGKILL to the node's outer bwrap: no chance to clean up
        node.wait(timeout=10)
        lines.append("SIGKILL to the node")
        with contextlib.suppress(CheckError):
            wait_for(lambda: not processes_holding(work), "the pods to end", 10)
        left = processes_holding(work)
        lines.append(f"after: {len(left)} processes left" + "".join(f"\n  {p}" for p in left))
        relay = socket.socket(socket.AF_UNIX)
        relay_refused = relay.connect_ex(str(work / "relay.sock")) != 0
        relay.close()
        lines.append(f"after: the relay refuses connections: {relay_refused}")
        forward.close()
        port_free = socket.socket().connect_ex(("127.0.0.1", forward.port)) != 0
        lines.append(f"after: port {forward.port} free: {port_free}")
        for entry in list(work.iterdir()):
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        work_empty = not any(work.iterdir())
        lines.append(f"after: the work directory's contents removed: {work_empty}")
    finally:
        with contextlib.suppress(OSError):
            node.kill()
    evidence = "\n".join(lines)
    problems = []
    if before != "a":
        problems.append("the port-forward didn't answer before the kill")
    if len(running) < 4:
        problems.append("fewer than the pods' processes were running before the kill")
    if left:
        problems.append(f"{len(left)} processes outlived the node")
    if not (relay_refused and port_free and work_empty):
        problems.append("the relay, the port or the work directory wasn't released")
    if problems:
        raise CheckError("; ".join(problems), evidence)
    return evidence


def scenario_teardown(work: Path) -> str:
    """GK-1.9 inside the node: a server pod, a pod with a daemonised child, the relay;
    then it waits to be killed."""
    node = Node(work)
    a = node.start_pod("a", 2, python_pod(work, "serve-a", NAME_SERVER, "a", "8080"))
    daemon = f"setsid sleep {DAEMON_MARKER} < /dev/null > /dev/null 2>&1 & exec sleep 300"
    node.start_pod("b", 3, ["sh", "-c", daemon])
    service = ServiceProxy(node, f"{SERVICE_NETWORK}.10", 8080)
    service.set_endpoints([(a.ip, 8080)])
    relay = socket.socket(socket.AF_UNIX)
    relay.bind(str(work / "relay.sock"))
    relay.listen()
    threading.Thread(target=serve_relay, args=(relay, service.address), daemon=True).start()
    wait_for(lambda: bool(processes_holding(Path("/nonexistent"))), "the daemon to start", 10)
    (work / "outside.json").write_text(json.dumps({"pods": [p.pid for p in node.pods]}))
    time.sleep(120)  # killed by the check long before this
    return "not killed"


SCENARIOS: dict[str, Callable[[Path], str]] = {
    "pod-networking": scenario_pod_networking,
    "same-port": scenario_same_port,
    "service-proxy": scenario_service_proxy,
    "names": scenario_names,
    "mounts": scenario_mounts,
    "getting-in": scenario_getting_in,
    "postgres": scenario_postgres,
    "teardown": scenario_teardown,
}

"""What runs inside the node namespace (`--in-node`), by name."""


def not_written(_work: Path) -> str:
    """A check whose probe isn't written yet."""
    raise CheckError("not written")


CHECKS: tuple[Check, ...] = (
    Check(1, "node namespace: CAP_NET_ADMIN, lo up, no host interfaces", node_namespace),
    Check(2, "pod networking without ip: veth, pod IP, route, pod to node", pod_networking),
    Check(3, "same port, two pods: each pod IP answers with its own name", same_port),
    Check(4, "Service proxy: ClusterIP to a pod; refused with no endpoint", service_proxy),
    Check(5, "names: per-pod /etc/hosts for glibc, Rust and Java; rewrite seen", names),
    Check(6, "per-pod mounts at absolute paths, seen by that pod only", mounts),
    Check(7, "getting in: nsenter into a pod; port-forward from outside", getting_in),
    Check(8, "a real workload: PostgreSQL in a pod, psql by Service name", real_workload),
    Check(9, "teardown: no process, port or work directory left", teardown),
)


def run_one(run: Callable[[Path], str], work: Path) -> int:
    """Runs a check or a scenario in this process; 0 if it passed.

    Prints the evidence, then a final "REASON: …" line if it failed.
    """
    try:
        print(run(work))
    except CheckError as failure:
        if failure.evidence:
            print(failure.evidence)
        print(f"REASON: {failure}")
        return 1
    return 0


def run_isolated(check: Check, root: Path) -> tuple[bool, str, str]:
    """Runs `check` in a child process in its own session, killed at the deadline.

    Returns (passed, why, evidence). Everything the check started is in the
    child's process group, so a timeout ends all of it.
    """
    work = root / f"check-{check.number}"
    work.mkdir()
    argv = [sys.executable, str(Path(__file__).resolve()), "--child", str(check.number)]
    argv += ["--work", str(work)]
    child = subprocess.Popen(
        argv,
        cwd=work,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = child.communicate(timeout=CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        kill_group(child.pid)
        output, _ = child.communicate()
        return False, f"no result within {CHECK_TIMEOUT_S:.0f} s", output
    finally:
        kill_group(child.pid)
    return outcome(child.returncode, output)


def outcome(returncode: int, output: str) -> tuple[bool, str, str]:
    """(passed, why, evidence) from run_one's output and exit code."""
    lines = output.rstrip("\n").splitlines()
    if returncode == 0:
        return True, "", "\n".join(lines)
    if lines and lines[-1].startswith("REASON: "):
        return False, lines[-1].removeprefix("REASON: "), "\n".join(lines[:-1])
    return False, f"crashed (exit {returncode})", "\n".join(lines)


def kill_group(group: int) -> None:
    """SIGKILL to a process group, if anything is left in it."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)


def versions() -> list[tuple[str, str]]:
    """What the results depend on: kernel, bwrap, Python."""
    bwrap = shutil.which("bwrap")
    bwrap_version = (
        subprocess.run([bwrap, "--version"], capture_output=True, text=True).stdout.strip()
        if bwrap
        else "not installed"
    )
    return [
        ("Kernel", platform.release()),
        ("bwrap", bwrap_version),
        ("Python", platform.python_version()),
    ]


def main(argv: Sequence[str]) -> int:
    """Runs the selected checks and prints their results and a summary."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checks", nargs="*", type=int, help="check numbers (default: all)")
    parser.add_argument("--child", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--in-node", help=argparse.SUPPRESS)
    parser.add_argument("--configure-pod", nargs=3, help=argparse.SUPPRESS)
    parser.add_argument("--work", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child is not None:
        return run_one(next(c for c in CHECKS if c.number == args.child).run, args.work)
    if args.configure_pod is not None:
        pid, interface, ip = args.configure_pod
        return configure_pod_in(int(pid), interface, ip)
    if args.in_node is not None:
        scenario = SCENARIOS.get(args.in_node, not_written)
        return run_one(scenario, args.work)

    selected = [c for c in CHECKS if not args.checks or c.number in args.checks]
    for name, version in versions():
        print(f"{name}: {version}")
    root = Path(tempfile.mkdtemp(prefix=f"{MARKER}."))
    failed = 0
    try:
        for check in selected:
            passed, why, evidence = run_isolated(check, root)
            if passed:
                print(f"ok   GK-1.{check.number} {check.what}")
            else:
                failed += 1
                print(f"FAIL GK-1.{check.number} {check.what}: {why}")
            for line in evidence.splitlines():
                print(f"{EVIDENCE_INDENT}{line}")
            sys.stdout.flush()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if failed:
        print(f"FAIL: {failed} of {len(selected)} checks failed")
        return 1
    print(f"PASS: {len(selected)} of {len(selected)} checks ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
