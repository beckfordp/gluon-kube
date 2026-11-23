# SPDX-License-Identifier: MIT

"""The simulated cluster for real (GK-4): a node in unprivileged namespaces, pods with
their own IPs, Services by name, probes, restarts, exec and run, and a teardown that
leaves nothing. Expectations from gluon-local's manifests and Kubernetes' documented
behaviour; one cluster for the module (it takes a few seconds to start)."""

import http.server
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

GLUON = Path(__file__).resolve().parents[4]
LOCAL_INFRA = GLUON / "infra" / "k8s" / "local-infra"
REPO = Path(__file__).resolve().parents[2]
GK = Path(sys.executable).parent / "gluon-kube"

pytestmark = [
    pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bwrap"),
    pytest.mark.skipif(shutil.which("pg_ctl") is None, reason="needs PostgreSQL 16"),
    pytest.mark.skipif(not LOCAL_INFRA.is_dir(), reason="needs a gluon checkout"),
]


def gk(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    """The gluon-kube command, as a user runs it."""
    done = subprocess.run([str(GK), *args], capture_output=True, text=True, timeout=timeout)
    if check and done.returncode != 0:
        raise AssertionError(
            f"gluon-kube {' '.join(args)}: {done.returncode}\n{done.stdout}{done.stderr}"
        )
    return done


def pods() -> dict[str, list[str]]:
    """`get pods` rows by name: [ready, status, restarts, age]."""
    lines = gk("get", "pods").stdout.splitlines()[1:]
    return {line.split()[0]: line.split()[1:] for line in lines}


def pod_of(prefix: str) -> str:
    names = [name for name in pods() if name.startswith(prefix + "-")]
    assert len(names) == 1, names
    return names[0]


def environ(pid: str) -> list[bytes]:
    """A process's env entries (none if it has gone)."""
    try:
        return Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return []


def until(condition: object, what: str, timeout: float = 60) -> None:
    """Polls `condition()` until true, or fails naming `what`."""
    assert callable(condition)
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}: {pods()}")
        time.sleep(0.3)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A running cluster (backoff base 1 s) in a state directory of its own."""
    state = tmp_path_factory.mktemp("gk") / "state"
    previous = os.environ.get("GLUON_KUBE_STATE")
    os.environ["GLUON_KUBE_STATE"] = str(state)
    try:
        gk("cluster", "start", "--backoff-base", "1")
        yield state
    finally:
        gk("cluster", "stop", check=False)
        if previous is None:
            os.environ.pop("GLUON_KUBE_STATE", None)
        else:
            os.environ["GLUON_KUBE_STATE"] = previous


def test_postgres_runs_as_gluon_local_runs_it_reached_by_name(cluster: Path) -> None:
    gk("apply", "-f", str(LOCAL_INFRA / "postgres-user.yaml"))
    gk("wait", "--for=condition=Ready", "pod", "--all", "--timeout=60s")
    query = "select current_user, current_database(), current_setting('wal_level')"
    done = gk(
        "run",
        "--env",
        "PGPASSWORD=user",
        "--",
        "psql",
        "-h",
        "user-postgres",
        "-U",
        "user",
        "user",
        "-tAc",
        query,
    )
    assert done.stdout.strip() == "user|user|logical"
    fqdn = gk(
        "run",
        "--env",
        "PGPASSWORD=user",
        "--",
        "psql",
        "-h",
        "user-postgres.gluon-local.svc.cluster.local",
        "-U",
        "user",
        "user",
        "-tAc",
        "select 1",
    )
    assert fqdn.stdout.strip() == "1"
    wrong = gk(
        "run",
        "--env",
        "PGPASSWORD=wrong",
        "--",
        "psql",
        "-h",
        "user-postgres",
        "-U",
        "user",
        "user",
        "-c",
        "select 1",
        check=False,
    )
    assert wrong.returncode != 0
    assert "password authentication failed" in wrong.stderr


def test_exec_is_the_containers_user_env_and_names(cluster: Path) -> None:
    name = pod_of("user-postgres")
    assert gk("exec", "deploy/user-postgres", "--", "id", "-u").stdout == "70\n"
    assert gk("exec", "deploy/user-postgres", "--", "pg_isready", "-U", "user").returncode == 0
    env = gk("exec", f"pod/{name}", "--", "env").stdout.splitlines()
    assert f"HOSTNAME={name}" in env
    assert "KUBERNETES_SERVICE_HOST=10.96.0.1" in env
    assert "POSTGRES_DB=user" in env
    hosts = gk("exec", "deploy/user-postgres", "--", "cat", "/etc/hosts").stdout
    assert hosts.startswith("# Kubernetes-managed hosts file.\n")
    assert f"\t{name}\n" in hosts
    assert gk("exec", "deploy/user-postgres", "--", "hostname").stdout == f"{name}\n"
    assert "PostgreSQL init process complete" in gk("logs", "deploy/user-postgres").stdout


def test_a_killed_container_restarts_in_the_same_pod(cluster: Path) -> None:
    name = pod_of("user-postgres")
    # This pod's postmaster: its env has the pod's name (other clusters may be running).
    mine = [
        pid
        for pid in subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", "^postgres "], capture_output=True, text=True
        ).stdout.split()
        if f"HOSTNAME={name}".encode() in environ(pid)
    ]
    assert len(mine) == 1, mine
    os.kill(int(mine[0]), 9)
    until(lambda: pods()[name][2] == "1" and pods()[name][0] == "1/1", "a restart, Ready again")
    assert "ready to accept connections" in gk("logs", f"pod/{name}", "--previous").stdout
    again = gk(
        "run",
        "--env",
        "PGPASSWORD=user",
        "--",
        "psql",
        "-h",
        "user-postgres",
        "-U",
        "user",
        "user",
        "-tAc",
        "select 1",
    )
    assert again.stdout.strip() == "1"


def test_an_image_with_no_recipe_is_image_unavailable(cluster: Path, tmp_path: Path) -> None:
    manifest = tmp_path / "no-recipe.yaml"
    manifest.write_text(
        (LOCAL_INFRA / "redis-cart.yaml")
        .read_text()
        .replace("redis:7-alpine", "apicurio/apicurio-registry:3.3.2")
    )
    gk("apply", "-f", str(manifest))
    until(lambda: pods()[pod_of("cart-redis")][1] == "ImageUnavailable", "ImageUnavailable")
    assert "<none>" in gk("get", "svc", "cart-redis").stdout
    refused = gk("run", "--", "sh", "-c", "exec 3<>/dev/tcp/cart-redis/6379", check=False)
    assert refused.returncode != 0
    gk("delete", "-f", str(manifest))


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="needs redis-server")
def test_redis_from_gluon_locals_manifest_answers_by_its_service_name(cluster: Path) -> None:
    gk("apply", "-f", str(LOCAL_INFRA / "redis-cart.yaml"))
    until(lambda: pods()[pod_of("cart-redis")][1] == "Running", "Running")
    until(lambda: pods()[pod_of("cart-redis")][0] == "1/1", "Ready (redis-cli ping)")
    answer = gk("run", "--", "redis-cli", "-h", "cart-redis", "ping")
    assert answer.stdout.strip() == "PONG"
    gk("delete", "-f", str(LOCAL_INFRA / "redis-cart.yaml"))


def test_http_probes_gate_the_service_and_a_crash_loops(cluster: Path, tmp_path: Path) -> None:
    manifest = tmp_path / "web.yaml"
    manifest.write_text(
        """\
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
        - name: web
          image: postgres:16-alpine
          command: [python3, -m, http.server, "8080"]
          ports: [{name: http, containerPort: 8080}]
          readinessProbe: {httpGet: {path: /, port: http}, periodSeconds: 1}
          livenessProbe: {tcpSocket: {port: 8080}, periodSeconds: 1}
---
apiVersion: v1
kind: Service
metadata: {name: web}
spec:
  selector: {app: web}
  ports: [{port: 80, targetPort: http}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: crash}
spec:
  selector: {matchLabels: {app: crash}}
  template:
    metadata: {labels: {app: crash}}
    spec:
      containers: [{name: crash, image: postgres:16-alpine, command: [sh, -c, "exit 3"]}]
"""
    )
    gk("apply", "-f", str(manifest))
    gk("wait", "--for=condition=Ready", "deploy/web", "--timeout=60s")
    page = gk("run", "--", "curl", "-sf", "http://web/")
    assert "Directory listing" in page.stdout
    forward = subprocess.Popen(
        [str(GK), "port-forward", "svc/web", "0:80"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert forward.stdout is not None
        line = forward.stdout.readline()
        assert line.startswith("Forwarding from 127.0.0.1:"), line
        port = int(line.split(":", 2)[1].split()[0])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as answer:
            assert "Directory listing" in answer.read().decode()
    finally:
        forward.terminate()
        forward.wait(timeout=10)
    proxy = subprocess.Popen([str(GK), "proxy", "--port", "0"], stdout=subprocess.PIPE, text=True)
    outside = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler
    )
    threading.Thread(target=outside.serve_forever, daemon=True).start()
    try:
        assert proxy.stdout is not None
        line = proxy.stdout.readline()
        assert line.startswith("Serving the cluster's names on 127.0.0.1:"), line
        port = int(line.split("127.0.0.1:", 1)[1].split()[0])
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})
        )
        for url in ("http://web/", "http://web.gluon-local.svc.cluster.local/"):
            with opener.open(url, timeout=10) as answer:
                assert "Directory listing" in answer.read().decode()
        outside_url = f"http://127.0.0.1:{outside.server_address[1]}/"
        with opener.open(outside_url, timeout=10) as answer:
            assert answer.status == 200  # not a cluster name: connected directly
        with pytest.raises(urllib.error.HTTPError) as unknown:
            opener.open("http://no-such-service.invalid/", timeout=10)
        assert unknown.value.code == 502
    finally:
        outside.shutdown()
        proxy.terminate()
        proxy.wait(timeout=10)
    refused = gk("port-forward", "svc/nope", "1234", check=False)
    assert refused.returncode == 1
    assert 'services "nope" not found' in refused.stderr
    until(
        lambda: (
            pods()[pod_of("crash")][1] in ("CrashLoopBackOff", "Error")
            and int(pods()[pod_of("crash")][2]) >= 2
        ),
        "a crash loop",
    )
    gk("delete", "-f", str(manifest))


def test_the_gk1_probe_still_passes() -> None:
    """GK-1's feasibility probe, kept as GK-4's first integration test (plan GK-4.2)."""
    probe = REPO / "spikes" / "netns-probe" / "probe.py"
    done = subprocess.run(
        [sys.executable, str(probe), "1", "2", "3", "4", "9"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.splitlines()[-1].startswith("PASS")


def test_stop_leaves_nothing(cluster: Path) -> None:
    """Last: `cluster stop` ends every process and removes the state directory."""
    gk("cluster", "stop")

    def leftovers() -> list[str]:
        ps = subprocess.run(["ps", "-eo", "stat=,args="], capture_output=True, text=True).stdout
        return [
            line for line in ps.splitlines() if str(cluster) in line and not line.startswith("Z")
        ]

    until(lambda: not leftovers(), "every process to go", timeout=15)
    assert not cluster.exists()
    assert gk("cluster", "status", check=False).returncode == 1
