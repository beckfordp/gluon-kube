# SPDX-License-Identifier: MIT

"""The control loop (GK-4.2 to GK-4.6, GK-5.3, GK-5.4, GK-6.3) against a fake runtime and
an injected clock. Expectations from Kubernetes' documented behaviour: Deployments keep
their replicas and roll new templates in before old pods go; Jobs retry within
backoffLimit; init containers run in order first; restartPolicy and the kubelet's
backoff (10 s doubling) decide restarts; probes gate readiness and liveness restarts;
Services forward only to Ready pods."""

import itertools
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from gluon_kube.controller import STARTS_PER_TICK, ApplyError, Cluster, PodRecord, api_name
from gluon_kube.images import HostTools
from gluon_kube.manifests import Loaded, parse
from gluon_kube.status import pod_row, pod_status
from tests.fakes import Clock, FakeRuntime


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def cluster(tmp_path: Path, runtime: FakeRuntime, clock: Clock) -> Cluster:
    tools = HostTools(pg_bin=tmp_path / "pg" / "lib" / "postgresql" / "16" / "bin", cache=tmp_path)
    run_now: Callable[[Callable[[], None]], None] = lambda work: work()  # noqa: E731
    return Cluster(runtime, tmp_path / "state", clock, tools, spawn=run_now)


def load(text: str) -> list[Loaded]:
    return [
        parse(doc, f"t.yaml#{i}")
        for i, doc in enumerate(yaml.safe_load_all(dedent(text)), 1)
        if doc
    ]


def deployment(
    name: str = "web",
    image: str = "postgres:16-alpine",
    replicas: int = 1,
    extra: str = "",
    container: str = "",
) -> str:
    """A Deployment manifest; `extra` goes in the pod spec, `container` in the container."""
    pad = "\n" + " " * 14
    fields = pad + dedent(container).strip().replace("\n", pad) if container.strip() else ""
    return f"""\
    apiVersion: apps/v1
    kind: Deployment
    metadata: {{name: {name}, namespace: gluon-local}}
    spec:
      replicas: {replicas}
      selector: {{matchLabels: {{app: {name}}}}}
      template:
        metadata: {{labels: {{app: {name}}}}}
        spec:
          {extra or "restartPolicy: Always"}
          containers:
            - name: main
              image: {image}
              ports: [{{name: http, containerPort: 8080}}]{fields}
    """


SERVICE = """\
    apiVersion: v1
    kind: Service
    metadata: {name: web, namespace: gluon-local}
    spec:
      selector: {app: web}
      ports: [{port: 80, targetPort: http}]
    """


def ticks(cluster: Cluster, clock: Clock, seconds: float, step: float = 0.5) -> None:
    end = clock.now + seconds
    while clock.now < end:
        clock.now += step
        cluster.tick()


def only_pod(cluster: Cluster) -> PodRecord:
    pods = [p for p in cluster.pods.values() if p.deleting_since is None]
    assert len(pods) == 1, [p.name for p in pods]
    return pods[0]


# --- apply ---------------------------------------------------------------------------


def test_apply_reports_as_kubectl(cluster: Cluster) -> None:
    items = load(deployment() + "---\n" + SERVICE)
    assert cluster.apply(items) == ["deployment.apps/web created", "service/web created"]
    assert cluster.apply(items) == ["deployment.apps/web unchanged", "service/web unchanged"]
    changed = load(deployment(replicas=2))
    assert cluster.apply(changed) == ["deployment.apps/web configured"]


def test_apply_skips_what_isnt_simulated_and_refuses_the_unsupported(cluster: Cluster) -> None:
    items = load(
        """\
        kind: DaemonSet
        metadata: {name: d}
        ---
        apiVersion: v1
        kind: Service
        metadata: {name: s}
        spec: {type: NodePort, ports: [{port: 1}]}
        """
    )
    lines = cluster.apply(items)
    assert lines[0] == "DaemonSet/d skipped: not simulated by gluon-kube"
    assert lines[1].startswith("error: service/s not applied: unsupported: spec.type")


def test_a_jobs_template_is_immutable(cluster: Cluster) -> None:
    job = """\
    apiVersion: batch/v1
    kind: Job
    metadata: {{name: j}}
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers: [{{name: c, image: {image}}}]
    """
    assert cluster.apply(load(job.format(image="postgres:16-alpine"))) == ["job.batch/j created"]
    assert cluster.apply(load(job.format(image="other"))) == [
        'error: The Job "j" is invalid: spec.template: field is immutable'
    ]


def test_api_names() -> None:
    items = load("apiVersion: v1\nkind: Namespace\nmetadata: {name: gluon-local}\n")
    assert api_name(items[0].obj) == "namespace/gluon-local"


# --- pods ----------------------------------------------------------------------------


def test_a_deployment_gets_a_running_ready_pod_behind_its_service(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(deployment() + "---\n" + SERVICE))
    ticks(cluster, clock, 1)
    pod = only_pod(cluster)
    assert pod.name.startswith("web-")
    assert len(pod.name.split("-")) == 3
    assert runtime.started == [(pod.name, "main")]
    assert pod.ready
    assert pod_status(pod) == "Running"
    ip, ports = runtime.services["web"]
    assert ip.startswith("10.96.")
    assert ports == [(80, [(pod.ip, 8080)])]
    hosts = runtime.files[pod.hosts_path]
    assert f"{pod.ip}\t{pod.name}" in hosts
    assert (
        f"{ip}\tweb web.gluon-local web.gluon-local.svc web.gluon-local.svc.cluster.local" in hosts
    )
    row = pod_row(pod, clock.now)
    assert (row["ready"], row["status"], row["restarts"]) == ("1/1", "Running", 0)


def test_the_container_gets_the_images_command_and_kubernetes_env(cluster: Cluster) -> None:
    cluster.apply(load(deployment(container="args: [-c, wal_level=logical]")))
    cluster.tick()
    container = only_pod(cluster).containers[0]
    assert container.argv == ["docker-entrypoint.sh", "-c", "wal_level=logical"]
    assert container.env["HOSTNAME"] == only_pod(cluster).name
    assert container.env["KUBERNETES_SERVICE_HOST"] == "10.96.0.1"
    assert container.env["PGDATA"] == "/var/lib/postgresql/data"


def test_an_image_without_a_recipe_is_image_unavailable_and_never_started(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(deployment(image="redis:7-alpine") + "---\n" + SERVICE))
    ticks(cluster, clock, 2)
    pod = only_pod(cluster)
    assert pod_status(pod) == "ImageUnavailable"
    assert runtime.started == []
    assert runtime.sandboxes == []
    assert runtime.services["web"][1] == [(80, [])]


def test_a_crashing_container_restarts_with_the_kubelets_backoff(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(deployment()))
    cluster.tick()
    pod = only_pod(cluster)
    starts: list[float] = [clock.now]
    for _ in range(4):
        runtime.processes[(pod.name, "main")].code = 1
        before = len(runtime.started)
        while len(runtime.started) == before:
            clock.now += 0.5
            cluster.tick()
            if len(runtime.started) == before and clock.now - starts[-1] > 1:
                assert pod_status(pod) == "CrashLoopBackOff"
        starts.append(clock.now)
    gaps = [round(b - a) for a, b in itertools.pairwise(starts)]
    assert gaps == [0, 10, 20, 40]
    assert pod.containers[0].restarts == 4


def test_a_container_that_ran_ten_minutes_restarts_at_once(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(deployment()))
    cluster.tick()
    pod = only_pod(cluster)
    for _ in range(2):
        runtime.processes[(pod.name, "main")].code = 1
        ticks(cluster, clock, 11)
    clock.now += 601
    runtime.processes[(pod.name, "main")].code = 1
    before = len(runtime.started)
    clock.now += 0.1
    cluster.tick()
    assert len(runtime.started) == before + 1


def test_liveness_failure_restarts_and_readiness_gates_the_service(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    probes = """\
    readinessProbe: {httpGet: {path: /ready, port: http}, periodSeconds: 1, failureThreshold: 1}
    livenessProbe: {httpGet: {path: /health, port: 8080}, periodSeconds: 1, failureThreshold: 2}
    """
    cluster.apply(load(deployment(container=probes) + "---\n" + SERVICE))
    ticks(cluster, clock, 2)
    pod = only_pod(cluster)
    assert pod.ready
    assert runtime.services["web"][1] == [(80, [(pod.ip, 8080)])]
    runtime.answers["main"] = False
    ticks(cluster, clock, 1.5)
    assert not pod.ready
    assert runtime.services["web"][1] == [(80, [])]
    ticks(cluster, clock, 2)
    assert pod.containers[0].restarts == 1
    assert any("liveness probe failed" in e for e in cluster.events)


def test_the_startup_probe_holds_liveness_and_readiness(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    probes = """\
    startupProbe: {httpGet: {path: /h, port: 8080}, periodSeconds: 2, failureThreshold: 3}
    livenessProbe: {httpGet: {path: /h, port: 8080}, periodSeconds: 1, failureThreshold: 1}
    """
    runtime.answers["main"] = False
    cluster.apply(load(deployment(container=probes)))
    ticks(cluster, clock, 3)
    pod = only_pod(cluster)
    assert not pod.containers[0].started
    assert pod.containers[0].restarts == 0
    assert not pod.ready
    ticks(cluster, clock, 4)
    assert pod.containers[0].restarts == 1
    assert any("startup probe failed" in e for e in cluster.events)


def test_exec_probe_with_an_initial_delay(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    probe = "readinessProbe: {exec: {command: ['true']}, initialDelaySeconds: 5, periodSeconds: 2}"
    cluster.apply(load(deployment(container=probe)))
    ticks(cluster, clock, 4)
    assert not only_pod(cluster).ready
    ticks(cluster, clock, 2)
    assert only_pod(cluster).ready


def test_a_new_template_rolls_in_before_the_old_pod_goes(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(deployment()))
    ticks(cluster, clock, 1)
    old = only_pod(cluster)
    assert cluster.restart_rollout("web")
    cluster.tick()
    live = [p for p in cluster.pods.values() if p.deleting_since is None]
    assert len(live) == 2
    assert old in live
    done, message = cluster.rollout_done("web")
    assert not done
    assert "1 old replicas are pending termination" in message
    ticks(cluster, clock, 1)
    assert old.deleting_since is not None
    assert runtime.processes[(old.name, "main")].terminated
    runtime.processes[(old.name, "main")].code = 0
    ticks(cluster, clock, 1)
    assert old.name not in cluster.pods
    assert old.name in runtime.stopped
    assert cluster.rollout_done("web") == (True, 'deployment "web" successfully rolled out')
    assert not cluster.restart_rollout("missing")
    assert cluster.rollout_done("missing")[0] is False


def test_a_pod_that_ignores_sigterm_is_killed_after_the_grace_period(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(deployment()))
    ticks(cluster, clock, 1)
    pod = only_pod(cluster)
    assert cluster.delete("Deployment", "web")
    ticks(cluster, clock, 29)
    assert pod.name in cluster.pods
    assert pod_status(pod) == "Terminating"
    ticks(cluster, clock, 3)
    assert runtime.processes[(pod.name, "main")].killed
    assert pod.name not in cluster.pods
    assert not cluster.delete("Deployment", "web")


def test_replicas_scale_up_and_down(cluster: Cluster, clock: Clock) -> None:
    cluster.apply(load(deployment(replicas=2)))
    ticks(cluster, clock, 1)
    assert len(cluster.pods_of("Deployment", "web")) == 2
    cluster.apply(load(deployment(replicas=1)))
    ticks(cluster, clock, 1)
    assert sum(1 for p in cluster.pods_of("Deployment", "web") if p.deleting_since is None) == 1


def test_deleting_a_pod_makes_the_deployment_replace_it(cluster: Cluster, clock: Clock) -> None:
    cluster.apply(load(deployment()))
    ticks(cluster, clock, 1)
    first = only_pod(cluster)
    assert cluster.delete_pod(first.name)
    assert not cluster.delete_pod("nope")
    cluster.tick()
    assert only_pod(cluster) is not first


def test_a_missing_config_map_holds_the_pod_until_applied(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    volumes = "volumes: [{name: cfg, configMap: {name: settings}}]"
    cluster.apply(
        load(deployment(extra=volumes, container="volumeMounts: [{name: cfg, mountPath: /cfg}]"))
    )
    ticks(cluster, clock, 1)
    pod = only_pod(cluster)
    assert runtime.sandboxes == []
    assert pod_status(pod) == "Pending"
    assert pod.message == 'configmap "settings" not found'
    config_map = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings}\ndata: {a.json: '{}'}\n"
    )
    cluster.apply(load(config_map))
    ticks(cluster, clock, 1)
    assert runtime.sandboxes == [pod.name]
    assert (pod.directory / "volumes" / "cfg" / "a.json").read_text() == "{}"
    changed = config_map.replace("'{}'", "'[]'")
    cluster.apply(load(changed))
    assert runtime.files[pod.directory / "volumes" / "cfg" / "a.json"] == "[]"


def test_service_removal_rewrites_hosts(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(deployment() + "---\n" + SERVICE))
    ticks(cluster, clock, 1)
    pod = only_pod(cluster)
    assert cluster.delete("Service", "web")
    assert runtime.removed == ["web"]
    assert "\tweb " not in runtime.files[pod.hosts_path]


# --- init containers and Jobs ----------------------------------------------------------


JOB = """\
    apiVersion: batch/v1
    kind: Job
    metadata: {{name: register}}
    spec:
      backoffLimit: 2
      template:
        spec:
          restartPolicy: {policy}
          initContainers: [{{name: first, image: postgres:16-alpine}}]
          containers: [{{name: main, image: postgres:16-alpine}}]
    """


def test_init_containers_run_first_then_the_job_completes(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(JOB.format(policy="OnFailure")))
    cluster.tick()
    pod = only_pod(cluster)
    assert runtime.started == [(pod.name, "first")]
    assert pod_status(pod) == "Init:0/1"
    runtime.processes[(pod.name, "first")].code = 0
    ticks(cluster, clock, 0.5)
    assert runtime.started[-1] == (pod.name, "main")
    runtime.processes[(pod.name, "main")].code = 0
    ticks(cluster, clock, 1)
    assert pod.phase == "Succeeded"
    assert pod_status(pod) == "Completed"
    record = cluster.job_status("register")
    assert record is not None
    assert record.succeeded
    assert any("complete" in e for e in cluster.events)


def test_a_failing_job_on_failure_gives_up_after_backoff_limit_restarts(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(JOB.format(policy="OnFailure")))
    cluster.tick()
    pod = only_pod(cluster)
    runtime.processes[(pod.name, "first")].code = 0
    ticks(cluster, clock, 0.5)
    for _ in range(3):
        runtime.processes[(pod.name, "main")].code = 1
        ticks(cluster, clock, 25)
    record = cluster.job_status("register")
    assert record is not None
    assert record.failed
    assert any("BackoffLimitExceeded" in e for e in cluster.events)


def test_a_failing_job_never_makes_new_pods_until_the_limit(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    cluster.apply(load(JOB.format(policy="Never")))
    failed: list[PodRecord] = []
    for _ in range(200):
        ticks(cluster, clock, 1)
        live = [p for p in cluster.pods_of("Job", "register") if p.phase in ("Pending", "Running")]
        if live and (live[0].name, "first") in runtime.processes:
            runtime.processes[(live[0].name, "first")].code = 3
            ticks(cluster, clock, 1)
            assert live[0].phase == "Failed"
            assert pod_status(live[0]) == "Error"
            failed.append(live[0])
    assert len(failed) == 3  # backoffLimit 2: two retries after the first failure
    record = cluster.job_status("register")
    assert record is not None
    assert record.failed
    assert record.failures == 3


def test_a_failing_init_container_crash_loops_under_always(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    text = deployment(extra="initContainers: [{name: first, image: postgres:16-alpine}]")
    cluster.apply(load(text))
    cluster.tick()
    pod = only_pod(cluster)
    runtime.processes[(pod.name, "first")].code = 1
    ticks(cluster, clock, 1)
    runtime.processes[(pod.name, "first")].code = 1
    ticks(cluster, clock, 1)
    assert pod_status(pod) == "Init:CrashLoopBackOff"


# --- run pods and shutdown -------------------------------------------------------------


def test_run_pods_and_shutdown(cluster: Cluster, runtime: FakeRuntime, clock: Clock) -> None:
    cluster.apply(load(deployment() + "---\n" + SERVICE))
    name = cluster.run_pod("gluon-kube-run-x", "gluon-kube/host", ["sleep", "infinity"], {"A": "1"})
    ticks(cluster, clock, 1)
    run = cluster.pods[name]
    assert run.containers[0].env["A"] == "1"
    assert run.spec.restart_policy == "Never"
    assert run.ready
    cluster.shutdown()
    assert cluster.pods == {}
    assert runtime.services == {}
    assert all(p.killed for p in runtime.processes.values())


def test_images_that_fetch_get_the_egress_proxy(cluster: Cluster, clock: Clock) -> None:
    text = deployment(image="curlimages/curl:8.11.1")
    tools = HostTools(ca_bundle=Path("/etc/hostname"))
    cluster.tools = tools
    cluster.apply(load(text))
    cluster.tick()
    env = only_pod(cluster).containers[0].env
    assert env["HTTPS_PROXY"] == "http://10.244.0.1:3128"
    assert env["http_proxy"] == "http://10.244.0.1:3128"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


def test_a_job_from_a_cron_job_runs_now(cluster: Cluster, clock: Clock) -> None:
    cron = """\
    apiVersion: batch/v1
    kind: CronJob
    metadata: {name: cleanup}
    spec:
      schedule: "0 * * * *"
      jobTemplate:
        spec:
          template:
            spec:
              restartPolicy: OnFailure
              containers: [{name: c, image: postgres:16-alpine}]
    """
    cluster.apply(load(cron))
    ticks(cluster, clock, 1)
    assert cluster.pods == {}
    assert cluster.cron_jobs()[0].schedule == "0 * * * *"
    assert (
        cluster.job_from_cron_job("cleanup", "cleanup-manual") == "job.batch/cleanup-manual created"
    )
    ticks(cluster, clock, 1)
    assert len(cluster.pods_of("Job", "cleanup-manual")) == 1
    with pytest.raises(ApplyError, match="already exists"):
        cluster.job_from_cron_job("cleanup", "cleanup-manual")
    with pytest.raises(ApplyError, match="not found"):
        cluster.job_from_cron_job("nope", "x")
    assert not cluster.namespace_applied()


def test_a_mass_start_is_spread_over_passes(
    cluster: Cluster, runtime: FakeRuntime, clock: Clock
) -> None:
    """A pass starts at most STARTS_PER_TICK sandboxes and containers, so the node's API,
    which waits for a pass's lock, keeps answering while local-infra's pods start."""
    cluster.apply(load(deployment(replicas=5)))
    cluster.tick()
    assert len(runtime.sandboxes) + len(runtime.started) == STARTS_PER_TICK
    ticks(cluster, clock, 3)
    assert len(runtime.started) == 5
    assert all(p.ready for p in cluster.pods_of("Deployment", "web"))
