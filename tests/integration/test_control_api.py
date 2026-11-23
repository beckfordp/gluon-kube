# SPDX-License-Identifier: MIT

"""The CLI's verbs through the node's control socket (GK-4.1, GK-4.6, GK-4.8, GK-6.3),
with the node's API served in-process over a real Unix socket and a cluster on a fake
runtime: what `apply`, `get`, `logs`, `delete`, `wait` and `rollout` print and return, as
kubectl's do. The real runtime is test_cluster.py's."""

import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gluon_kube import client
from gluon_kube.cli import main
from gluon_kube.controller import Cluster
from gluon_kube.images import HostTools
from gluon_kube.node import SOCKET, Node, serve
from tests.fakes import FakeRuntime

GLUON = Path(__file__).resolve().parents[4]
POSTGRES_USER = GLUON / "infra" / "k8s" / "local-infra" / "postgres-user.yaml"
REDIS = GLUON / "infra" / "k8s" / "local-infra" / "redis-cart.yaml"
needs_gluon = pytest.mark.skipif(not POSTGRES_USER.exists(), reason="needs a gluon checkout")


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Cluster, FakeRuntime]]:
    """A node API on a fake runtime, ticking in the background; GLUON_KUBE_STATE set."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("GLUON_KUBE_STATE", str(state))
    pg_bin = tmp_path / "usr" / "lib" / "postgresql" / "16" / "bin"
    runtime = FakeRuntime()
    tools = HostTools(pg_bin=pg_bin, cache=tmp_path / "cache", uid=1000, gid=1000, home=tmp_path)
    cluster = Cluster(runtime, state, time.monotonic, tools, backoff_base_s=0.1)
    stop = threading.Event()
    server = serve(Node(cluster, stop.set), state / SOCKET)

    def loop() -> None:
        while not stop.is_set():
            cluster.tick()
            stop.wait(0.05)

    threading.Thread(target=loop, daemon=True).start()
    yield cluster, runtime
    stop.set()
    server.shutdown()
    server.server_close()


def out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


@needs_gluon
def test_apply_get_logs_and_delete(
    node: tuple[Cluster, FakeRuntime], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, _runtime = node
    assert main(["apply", "-f", str(POSTGRES_USER)]) == 0
    assert out(capsys).splitlines() == [
        "deployment.apps/user-postgres created",
        "service/user-postgres created",
    ]
    assert main(["wait", "--for=condition=Ready", "pod", "--all", "--timeout=10s"]) == 0
    assert main(["get", "pods"]) == 0
    lines = out(capsys).splitlines()
    assert lines[0].split() == ["NAME", "READY", "STATUS", "RESTARTS", "AGE"]
    name, ready, status, restarts, _ = lines[1].split()
    assert (ready, status, restarts) == ("1/1", "Running", "0")
    assert main(["get", "pods", "-o", "name"]) == 0
    assert out(capsys) == f"pod/{name}\n"
    assert main(["get", "pods", name, "-o", "wide"]) == 0
    assert "10.244.0." in out(capsys)
    assert main(["get", "svc"]) == 0
    assert "user-postgres   ClusterIP   10.96." in out(capsys)
    assert main(["get", "deploy"]) == 0
    assert out(capsys).splitlines() == ["NAME", "user-postgres"]
    assert main(["wait", "--for=condition=Available", "deploy/user-postgres"]) == 0

    pod = cluster.pods[name]
    pod.containers[0].log.write_text("one\ntwo\n")
    assert main(["logs", "deploy/user-postgres"]) == 0
    assert out(capsys) == "one\ntwo\n"
    assert main(["logs", f"pod/{name}", "--tail", "1"]) == 0
    assert out(capsys) == "two\n"
    assert main(["logs", name, "-c", "postgres", "--previous"]) == 1
    assert "previous terminated container" in capsys.readouterr().err

    assert main(["delete", "deployment", "user-postgres"]) == 0
    assert out(capsys) == 'deployment "user-postgres" deleted\n'
    assert main(["delete", "deploy/user-postgres"]) == 1
    assert "NotFound" in capsys.readouterr().err
    assert main(["delete", "deploy/user-postgres", "--ignore-not-found"]) == 0
    assert main(["delete", "-f", str(POSTGRES_USER), "--ignore-not-found"]) == 0
    assert 'service "user-postgres" deleted' in out(capsys)


@needs_gluon
def test_image_unavailable_shows_and_waits_time_out(
    node: tuple[Cluster, FakeRuntime], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["apply", "-f", str(REDIS)]) == 0
    time.sleep(0.3)
    assert main(["get", "pods"]) == 0
    assert "ImageUnavailable" in out(capsys)
    assert main(["wait", "--for=condition=Ready", "pod", "--all", "--timeout=1s"]) == 1
    assert "timed out waiting for the condition" in capsys.readouterr().err
    assert main(["wait", "--for=condition=Ready", "deploy/cart-redis", "--timeout=0"]) == 1


def test_rollout_restart_and_status(
    node: tuple[Cluster, FakeRuntime],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "web.yaml"
    manifest.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web}\nspec:\n"
        "  selector: {matchLabels: {app: web}}\n  template:\n    metadata: {labels: {app: web}}\n"
        "    spec: {containers: [{name: web, image: postgres:16-alpine}]}\n"
    )
    assert main(["apply", "-f", str(manifest)]) == 0
    assert main(["rollout", "status", "deployment/web", "--timeout=10s"]) == 0
    assert 'deployment "web" successfully rolled out' in out(capsys)
    cluster, runtime = node
    for process in runtime.processes.values():
        process.graceful = True  # stops when asked
    first = {p.name for p in cluster.pods_of("Deployment", "web")}
    assert main(["rollout", "restart", "deployment/web"]) == 0
    assert out(capsys) == "deployment.apps/web restarted\n"
    assert main(["rollout", "status", "deploy/web", "--timeout=20s"]) == 0
    assert {p.name for p in cluster.pods_of("Deployment", "web")}.isdisjoint(first)
    assert main(["rollout", "status", "deploy/missing", "--timeout=0"]) == 1


def test_jobs_wait_for_complete(
    node: tuple[Cluster, FakeRuntime], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    manifest = tmp_path / "job.yaml"
    manifest.write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata: {name: once}\nspec:\n  template:\n"
        "    spec:\n      restartPolicy: OnFailure\n"
        "      containers: [{name: c, image: postgres:16-alpine}]\n"
    )
    cluster, runtime = node
    assert main(["apply", "-f", str(manifest)]) == 0
    assert main(["wait", "--for=condition=complete", "job/once", "--timeout=0"]) == 1
    time.sleep(0.3)
    pod = cluster.pods_of("Job", "once")[0]
    runtime.processes[(pod.name, "c")].code = 0
    assert main(["wait", "--for=condition=complete", "job/once", "--timeout=10s"]) == 0
    assert main(["get", "jobs"]) == 0
    assert out(capsys).splitlines()[-1] == "once"


def test_refusals_and_errors(
    node: tuple[Cluster, FakeRuntime], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: v1\nkind: Service\nmetadata: {name: s}\nspec: {type: NodePort}\n")
    assert main(["apply", "-f", str(bad)]) == 1
    assert "unsupported: spec.type" in capsys.readouterr().err
    assert main(["get", "widgets"]) == 1
    assert "doesn't have a resource type" in capsys.readouterr().err
    assert main(["get", "pods", "nope"]) == 1
    assert main(["get", "jobs", "nope"]) == 1
    assert main(["logs", "pod/nope"]) == 1
    assert 'pods "nope" not found' in capsys.readouterr().err
    assert main(["exec", "deploy/nope", "--", "true"]) == 1
    assert main(["get", "pods", "-n", "default"]) == 0
    assert main(["get", "pods", "-n", "other"]) == 1
    assert main(["wait", "--for=condition=Ready", "svc/x"]) == 1
    assert main(["wait", "--for=condition=Ready", "deploy", "--all"]) == 1
    assert main(["rollout", "restart", "svc/x"]) == 1
    assert main(["rollout", "restart", "deploy/x"]) == 1
    assert main(["delete", "x"]) == 1
    assert client.request("ping")["pid"]
    with pytest.raises(client.ClusterError, match="unknown op"):
        client.request("frobnicate")


def test_cluster_status_and_no_cluster(
    node: tuple[Cluster, FakeRuntime],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert main(["cluster", "status"]) == 0
    assert out(capsys).startswith("cluster running: state ")
    monkeypatch.setenv("GLUON_KUBE_STATE", str(tmp_path / "none"))
    assert main(["cluster", "status"]) == 1
    assert out(capsys) == "no cluster running\n"
    assert main(["cluster", "stop"]) == 0
    assert out(capsys) == "no cluster was running\n"
    assert main(["get", "pods"]) == 1
    assert "no cluster running" in capsys.readouterr().err


def fake_gluon(tmp_path: Path) -> Path:
    """A gluon tree for `up`: local-infra with a Postgres, the real chart, values files,
    a Rust port whose Dockerfile's build is `cargo --version`, and a Scala service."""
    root = tmp_path / "gluon"
    infra = root / "infra" / "k8s" / "local-infra"
    infra.mkdir(parents=True)
    (infra / "namespace.yaml").write_text(
        "apiVersion: v1\nkind: Namespace\nmetadata: {name: gluon-local}\n"
    )
    (infra / "postgres-user.yaml").write_text(POSTGRES_USER.read_text())
    (infra / "switch-reset.sql").write_text("select 1;\n")
    shutil.copytree(GLUON / "infra" / "k8s" / "gluon", root / "infra" / "k8s" / "gluon")
    local = root / "environments" / "local"
    local.mkdir(parents=True)
    for name in ("user-service", "user-service-rs"):
        shutil.copy(GLUON / "environments" / "local" / f"{name}.values.yaml", local)
    (root / "services" / "user-service").mkdir(parents=True)
    port = root / "services" / "user-service-rs"
    (port / "target" / "release").mkdir(parents=True)
    (port / "target" / "release" / "user-service").write_text("binary")
    (port / "Dockerfile").write_text(
        "FROM rust AS build\nWORKDIR /src/services/user-service-rs\nRUN cargo --version\n"
        "FROM debian\nCOPY --from=build /src/services/user-service-rs/target/release/"
        'user-service /usr/local/bin/user-service\nENTRYPOINT ["/usr/local/bin/user-service"]\n'
    )
    return root


@needs_gluon
def test_up_runs_k8s_local_ups_sequence(
    node: tuple[Cluster, FakeRuntime],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster, runtime = node
    runtime.graceful = True  # containers stop on SIGTERM, as servers do
    gluon = fake_gluon(tmp_path)
    cluster.tools = HostTools(pg_bin=cluster.tools.pg_bin, cache=cluster.tools.cache, gluon=gluon)
    monkeypatch.setenv("GLUON_KUBE_GLUON", str(gluon))
    monkeypatch.delenv("USER_SERVICE_IMPL", raising=False)
    assert main(["up", "user-service"]) == 0
    out = out_and_err(capsys)
    for heading in (
        "== Applying namespace + shared local infra ==",
        "== Waiting for shared infra to be ready (up to 3m) ==",
        "== Building user-service image from services/user-service-rs (gluon-kube build) ==",
        "== Deploying user-service (helm release) ==",
        'Release "user-service" does not exist. Installing it now.',
        'deployment "user-service" successfully rolled out',
        "== Registering Debezium outbox connectors after the migrations (up to 2m) ==",
        "== Waiting for every outbox connector to be RUNNING (up to 2m) ==",
        "== Done. Check status with: gluon-kube get pods ==",
    ):
        assert heading in out, out
    assert "connector registration didn't complete" in out  # no Job in this tree
    assert cluster.get("Deployment", "user-service") is not None
    assert (
        main(
            [
                "helm",
                "upgrade",
                "--install",
                "user-service",
                str(gluon / "infra" / "k8s" / "gluon"),
                "-f",
                str(gluon / "environments" / "local" / "user-service-rs.values.yaml"),
            ]
        )
        == 0
    )
    assert 'Release "user-service" has been upgraded. Happy Helming!' in out_and_err(capsys)
    monkeypatch.setenv("USER_SERVICE_IMPL", "other")
    assert main(["up", "user-service"]) == 1
    assert "USER_SERVICE_IMPL must be scala or rust" in out_and_err(capsys)
    monkeypatch.setenv("USER_SERVICE_IMPL", "rust")
    assert main(["up", "no-such-service"]) == 1
    assert main(["build", "no-such-rs"]) == 2


def out_and_err(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err
