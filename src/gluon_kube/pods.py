# SPDX-License-Identifier: MIT

"""What Kubernetes decides about a pod, as pure functions (GK-4.3 to GK-4.6, GK-4.9).

The command line from the image and the manifest, the env a container gets, the hosts
file, Service ClusterIPs, restart backoff and probe bookkeeping. Each names where the
behaviour is specified; none does I/O, so each is unit-tested with injected time.
"""

import hashlib
import ipaddress
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gluon_kube.model import Container, ContainerPort, Probe

NAMESPACE = "gluon-local"
CLUSTER_DOMAIN = "cluster.local"
POD_NETWORK = ipaddress.ip_network("10.244.0.0/24")
"""Pod IPs (Kubernetes' default pod CIDR, one node's /24); .1 is the node."""
NODE_IP = str(POD_NETWORK.network_address + 1)
SERVICE_NETWORK = ipaddress.ip_network("10.96.0.0/16")
"""ClusterIPs (Kubernetes' default Service CIDR starts at 10.96.0.0)."""
KUBERNETES_SERVICE_IP = str(SERVICE_NETWORK.network_address + 1)
"""The `kubernetes` Service's ClusterIP: the first of the range, as kubeadm gives it."""


def command_line(container: Container, entrypoint: Sequence[str], cmd: Sequence[str]) -> list[str]:
    """What a container runs (kubernetes.io "Define a Command and Arguments for a
    Container"): `command` replaces the image's ENTRYPOINT and `args` its CMD; with a
    `command` and no `args`, the CMD is not used."""
    if container.command:
        return [*container.command, *container.args]
    if container.args:
        return [*entrypoint, *container.args]
    return [*entrypoint, *cmd]


def kubernetes_env() -> dict[str, str]:
    """The `kubernetes` Service's env, which the kubelet sets in every container even with
    `enableServiceLinks: false` (seen on OrbStack: laptop-reference/cluster/inside-*.txt).
    No API server answers it here (GK-4.9)."""
    ip, port = KUBERNETES_SERVICE_IP, "443"
    return {
        "KUBERNETES_SERVICE_HOST": ip,
        "KUBERNETES_SERVICE_PORT": port,
        "KUBERNETES_SERVICE_PORT_HTTPS": port,
        "KUBERNETES_PORT": f"tcp://{ip}:{port}",
        "KUBERNETES_PORT_443_TCP": f"tcp://{ip}:{port}",
        "KUBERNETES_PORT_443_TCP_ADDR": ip,
        "KUBERNETES_PORT_443_TCP_PORT": port,
        "KUBERNETES_PORT_443_TCP_PROTO": "tcp",
    }


def container_env(
    image_env: Mapping[str, str],
    container: Container,
    pod_name: str,
    home: str,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A container's env: the image's, HOME (the runtime's, from the image's user), HOSTNAME,
    the kubernetes Service's, anything gluon-kube adds (`extra`), then the manifest's
    `env`, which wins (kubernetes.io "Define Environment Variables for a Container")."""
    env = dict(image_env)
    env.setdefault("HOME", home)
    env["HOSTNAME"] = pod_name
    env.update(kubernetes_env())
    env.update(extra or {})
    env.update({var.name: var.value for var in container.env})
    return env


def service_names(name: str) -> list[str]:
    """The names a Service is reached by from gluon-local: short, and the cluster-DNS
    forms (`<name>.<namespace>[.svc[.cluster.local]]`)."""
    return [
        name,
        f"{name}.{NAMESPACE}",
        f"{name}.{NAMESPACE}.svc",
        f"{name}.{NAMESPACE}.svc.{CLUSTER_DOMAIN}",
    ]


def hosts_file(pod_name: str, pod_ip: str, services: Mapping[str, str]) -> str:
    """A pod's /etc/hosts: the kubelet's (as OrbStack's, laptop-reference/cluster/
    inside-*.txt), then every Service by name, as gluon-kube's stand-in for cluster DNS."""
    lines = [
        "# Kubernetes-managed hosts file.",
        "127.0.0.1\tlocalhost",
        "::1\tlocalhost ip6-localhost ip6-loopback",
        "fe00::0\tip6-localnet",
        "fe00::0\tip6-mcastprefix",
        "fe00::1\tip6-allnodes",
        "fe00::2\tip6-allrouters",
        f"{pod_ip}\t{pod_name}",
        "",
        "# gluon-kube: Services by name (cluster DNS in Kubernetes).",
    ]
    lines += [f"{ip}\t{' '.join(service_names(name))}" for name, ip in sorted(services.items())]
    return "\n".join(lines) + "\n"


RESOLV_CONF = (
    f"# gluon-kube: no cluster DNS; Service names are in /etc/hosts.\n"
    f"search {NAMESPACE}.svc.{CLUSTER_DOMAIN} svc.{CLUSTER_DOMAIN} {CLUSTER_DOMAIN}\n"
    "options ndots:5\n"
)


def cluster_ip(name: str, taken: Mapping[str, str]) -> str:
    """A stable ClusterIP for Service `name`: a hash of the name into SERVICE_NETWORK, the
    next free address on a collision; never the network, the kubernetes Service's or the
    broadcast address. `taken` maps IPs already used to their Services."""
    size = SERVICE_NETWORK.num_addresses
    start = int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], "big") % size
    for offset in range(size):
        candidate = str(SERVICE_NETWORK.network_address + (start + offset) % size)
        if candidate in (
            str(SERVICE_NETWORK.network_address),
            KUBERNETES_SERVICE_IP,
            str(SERVICE_NETWORK.broadcast_address),
        ):
            continue
        if taken.get(candidate, name) == name:
            return candidate
    raise RuntimeError("no ClusterIP left")


def pod_name(owner: str, template_hash: str, suffix: str | None = None) -> str:
    """A Deployment's pod name, as Kubernetes forms it: `<deployment>-<template hash>-<5
    random>`."""
    return f"{owner}-{template_hash}-{suffix or random_suffix()}"


ALPHANUMS = "bcdfghjklmnpqrstvwxz2456789"
"""Kubernetes' rand.String alphabet (no vowels, no 0/1/3)."""


def random_suffix(length: int = 5) -> str:
    """Kubernetes' random name suffix."""
    return "".join(secrets.choice(ALPHANUMS) for _ in range(length))


def template_hash(template: object) -> str:
    """A stable short hash of a pod template (Kubernetes' pod-template-hash, in spirit)."""
    digest = hashlib.sha256(repr(template).encode()).digest()
    return "".join(ALPHANUMS[b % len(ALPHANUMS)] for b in digest[:10])


BACKOFF_CAP_S = 300.0
BACKOFF_RESET_S = 600.0


def backoff(restarts: int, base_s: float = 10.0) -> float:
    """The kubelet's crash-loop delay before restart number `restarts` + 1: 10 s doubling,
    capped at 5 minutes (kubernetes.io "Pod Lifecycle": container restarts). Scaled by
    `base_s` for tests."""
    if restarts <= 0:
        return 0.0
    return min(base_s * 2.0 ** (restarts - 1), BACKOFF_CAP_S * base_s / 10.0)


def resolve_port(port: int | str, ports: Sequence[ContainerPort]) -> int | None:
    """A port given by number or by a container port's name."""
    if isinstance(port, int):
        return port
    for candidate in ports:
        if candidate.name == port:
            return candidate.container_port
    return None


@dataclass(slots=True)
class ProbeTracker:
    """A probe's schedule and outcome (kubernetes.io "Configure Liveness, Readiness and
    Startup Probes"): first run `initialDelaySeconds` after the container starts, then
    every `periodSeconds`; `successThreshold` consecutive successes make it pass,
    `failureThreshold` consecutive failures make it fail. `passed` is None until then."""

    probe: Probe
    started_at: float
    last_run: float | None = None
    successes: int = 0
    failures: int = 0
    passed: bool | None = None
    running: bool = False

    def due(self, now: float) -> bool:
        """Whether to run it now."""
        if self.running or now < self.started_at + self.probe.initial_delay_seconds:
            return False
        return self.last_run is None or now >= self.last_run + self.probe.period_seconds

    def begin(self, now: float) -> None:
        """A run starts."""
        self.running = True
        self.last_run = now

    def record(self, ok: bool) -> None:
        """A run's result."""
        self.running = False
        if ok:
            self.successes += 1
            self.failures = 0
            if self.successes >= self.probe.success_threshold:
                self.passed = True
        else:
            self.failures += 1
            self.successes = 0
            if self.failures >= self.probe.failure_threshold:
                self.passed = False
