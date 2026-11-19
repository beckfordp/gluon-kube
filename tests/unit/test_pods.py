# SPDX-License-Identifier: MIT

"""What Kubernetes decides about a pod (GK-4.3 to GK-4.6, GK-4.9), from its documentation:
command and args against ENTRYPOINT and CMD, env precedence, the kubelet's hosts file,
ClusterIPs, pod names, crash-loop backoff, probe thresholds."""

import pytest

from gluon_kube.model import Container, ContainerPort, EnvVar, HttpGet, Probe
from gluon_kube.pods import (
    ProbeTracker,
    backoff,
    cluster_ip,
    command_line,
    container_env,
    hosts_file,
    pod_name,
    random_suffix,
    resolve_port,
    service_names,
    template_hash,
)

ENTRYPOINT, CMD = ["docker-entrypoint.sh"], ["postgres"]


@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        ((), (), ["docker-entrypoint.sh", "postgres"]),
        ((), ("-c", "wal_level=logical"), ["docker-entrypoint.sh", "-c", "wal_level=logical"]),
        (("sh", "-c", "x"), (), ["sh", "-c", "x"]),
        (("sh",), ("-c", "x"), ["sh", "-c", "x"]),
    ],
)
def test_command_and_args_replace_entrypoint_and_cmd(
    command: tuple[str, ...], args: tuple[str, ...], expected: list[str]
) -> None:
    container = Container(name="c", image="i", command=command, args=args)
    assert command_line(container, ENTRYPOINT, CMD) == expected


def test_env_precedence_manifest_over_kubernetes_over_image() -> None:
    container = Container(
        name="c", image="i", env=(EnvVar("PATH", "/mine"), EnvVar("HOSTNAME", "x"))
    )
    env = container_env({"PATH": "/image", "HOME": "/root", "LANG": "C"}, container, "pod-1", "/")
    assert env["PATH"] == "/mine"
    assert env["HOSTNAME"] == "x"
    assert env["HOME"] == "/root"
    assert env["KUBERNETES_SERVICE_HOST"] == "10.96.0.1"
    assert env["KUBERNETES_PORT_443_TCP"] == "tcp://10.96.0.1:443"
    assert (
        container_env({}, Container(name="c", image="i"), "pod-1", "/home/x")["HOME"] == "/home/x"
    )
    assert container_env({}, Container(name="c", image="i"), "p", "/", {"X": "1"})["X"] == "1"


def test_hosts_file_as_the_kubelets_then_services() -> None:
    text = hosts_file("web-abc-12345", "10.244.0.7", {"kafka": "10.96.1.2"})
    lines = text.splitlines()
    assert lines[0] == "# Kubernetes-managed hosts file."
    assert lines[1] == "127.0.0.1\tlocalhost"
    assert "10.244.0.7\tweb-abc-12345" in lines
    assert lines[-1] == "10.96.1.2\t" + " ".join(service_names("kafka"))
    assert service_names("kafka")[-1] == "kafka.gluon-local.svc.cluster.local"


def test_cluster_ips_are_stable_distinct_and_avoid_reserved_addresses() -> None:
    first = cluster_ip("user-postgres", {})
    assert first == cluster_ip("user-postgres", {})
    assert first.startswith("10.96.")
    assert first not in ("10.96.0.0", "10.96.0.1", "10.96.255.255")
    other = cluster_ip("kafka", {first: "user-postgres"})
    assert other != first
    assert cluster_ip("x", {cluster_ip("x", {}): "y"}) != cluster_ip("x", {})
    assert cluster_ip("x", {cluster_ip("x", {}): "x"}) == cluster_ip("x", {})


def test_pod_names_and_hashes() -> None:
    name = pod_name("user-postgres", "5s7crpnn59")
    prefix, suffix = name.rsplit("-", 1)
    assert prefix == "user-postgres-5s7crpnn59"
    assert len(suffix) == 5
    assert set(random_suffix(50)) <= set("bcdfghjklmnpqrstvwxz2456789")
    assert template_hash({"a": 1}) == template_hash({"a": 1}) != template_hash({"a": 2})
    assert len(template_hash("x")) == 10


@pytest.mark.parametrize(
    ("restarts", "delay"), [(0, 0), (1, 10), (2, 20), (3, 40), (5, 160), (6, 300), (20, 300)]
)
def test_backoff_doubles_from_10s_capped_at_5_minutes(restarts: int, delay: float) -> None:
    assert backoff(restarts) == delay


def test_backoff_scales_for_tests() -> None:
    assert backoff(1, base_s=1) == 1
    assert backoff(20, base_s=1) == 30


def test_named_and_numbered_ports() -> None:
    ports = (ContainerPort(8080, "http"), ContainerPort(9090, "metrics"))
    assert resolve_port(8080, ports) == 8080
    assert resolve_port("metrics", ports) == 9090
    assert resolve_port("nope", ports) is None


def test_probe_tracker_schedule_and_thresholds() -> None:
    probe = Probe(HttpGet("/", 80), initial_delay_seconds=5, period_seconds=2, failure_threshold=2)
    tracker = ProbeTracker(probe, started_at=100)
    assert not tracker.due(104)
    assert tracker.due(105)
    tracker.begin(105)
    assert not tracker.due(105.5)  # running
    tracker.record(True)
    assert tracker.passed is True
    assert not tracker.due(106)
    assert tracker.due(107)
    tracker.record(False)
    assert tracker.passed is True  # one failure isn't enough
    tracker.record(False)
    assert tracker.passed is False
    tracker.record(True)
    assert tracker.passed is True
