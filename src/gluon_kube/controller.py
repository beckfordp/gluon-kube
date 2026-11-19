# SPDX-License-Identifier: MIT

"""The simulated cluster's control loop (GK-4.2 to GK-4.6, GK-5.3, GK-5.4, GK-6.3).

Holds the applied objects and the pods and Services made from them, and on every `tick`
moves the observed state towards the desired one, as Kubernetes' controllers and kubelet
do: Deployments keep their pods (a changed template rolls them: new pods first, old ones
removed once the new are Ready); Jobs run a pod to completion within `backoffLimit`;
pods run their init containers in order, then their containers, restarting them as
`restartPolicy` says with the kubelet's backoff; probes gate readiness and restart on
liveness failure; Services forward to their Ready pods' IPs.

Every process, namespace and socket is the `Runtime`'s (`runtime.py`); this module
decides, so it is unit-tested against a fake runtime with an injected clock.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from gluon_kube.images import HostTools, Recipe, Unavailable, recipe_for
from gluon_kube.manifests import Loaded
from gluon_kube.model import (
    ConfigMap,
    ConfigMapVolume,
    Container,
    CronJob,
    Deployment,
    Job,
    KubeObject,
    Namespace,
    NotSimulated,
    ObjectMeta,
    PodSpec,
    Probe,
    Service,
)
from gluon_kube.pods import (
    BACKOFF_RESET_S,
    NODE_IP,
    ProbeTracker,
    backoff,
    cluster_ip,
    command_line,
    container_env,
    hosts_file,
    pod_name,
    random_suffix,
    resolve_port,
    template_hash,
)
from gluon_kube.report import kind_of, name_of

GRACE_S = 30.0
STARTS_PER_TICK = 3
"""Sandboxes and containers started in one pass: each is slow (a pod's network is wired
by a helper process), and the API waits for the lock a pass holds, so a mass start
(local-infra's 16 pods) is spread over passes and the node keeps answering."""
EGRESS_PROXY = f"http://{NODE_IP}:3128"
"""The node's egress proxy (egress.py), for images that fetch from the internet."""


def egress_env() -> dict[str, str]:
    """The proxy env curl and most HTTP clients honour (both spellings)."""
    env = {"HTTPS_PROXY": EGRESS_PROXY, "HTTP_PROXY": EGRESS_PROXY}
    env |= {key.lower(): value for key, value in env.items()}
    env |= {"NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
    return env


"""terminationGracePeriodSeconds' default: SIGTERM, then SIGKILL this much later."""


class Process(Protocol):
    """A running container."""

    pid: int

    def poll(self) -> int | None:
        """Its exit code, or None while it runs."""

    def terminate(self) -> None:
        """SIGTERM to its main process."""

    def kill(self) -> None:
        """SIGKILL to all of it."""


class Sandbox(Protocol):
    """A pod's shared namespaces (network, UTS, IPC) and IP."""

    ip: str
    pid: int


class Runtime(Protocol):
    """What runs things: namespaces, processes, probes, Service proxies."""

    def start_sandbox(self, pod: "PodRecord") -> Sandbox:
        """The pod's namespaces, wired to the node; its IP."""

    def stop_sandbox(self, pod: "PodRecord") -> None:
        """Ends the pod's namespaces and unwires it."""

    def start_container(self, pod: "PodRecord", container: "ContainerRecord") -> Process:
        """Starts a container of the pod."""

    def probe(self, pod: "PodRecord", container: "ContainerRecord", probe: Probe) -> bool:
        """Runs a probe once (blocking, within its timeout); True if it passed."""

    def set_service(
        self, name: str, ip: str, ports: Sequence[tuple[int, Sequence[tuple[str, int]]]]
    ) -> None:
        """Serves a Service's ClusterIP: each port forwarded to its endpoints (none:
        refused)."""

    def remove_service(self, name: str, ip: str) -> None:
        """Stops serving a Service."""

    def write_file(self, path: Path, text: str) -> None:
        """Writes a file a pod has mounted, in place (GK-1.5: a bind mount sees a rewrite,
        not a replacement)."""


type ContainerState = Literal["Waiting", "Running", "Terminated"]


@dataclass(slots=True)
class ContainerRecord:
    """A container of a pod and what the kubelet knows of it."""

    spec: Container
    init: bool
    recipe: Recipe | Unavailable
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    state: ContainerState = "Waiting"
    reason: str = "ContainerCreating"
    exit_code: int | None = None
    restarts: int = 0
    crashes: int = 0
    """Restarts since the backoff last reset (the backoff's count, not the one shown)."""
    process: Process | None = None
    started_at: float | None = None
    next_start: float = 0.0
    started: bool = False
    ready: bool = False
    probes: dict[str, ProbeTracker] = field(default_factory=dict)
    stopping_since: float | None = None
    log: Path = Path("/dev/null")

    @property
    def name(self) -> str:
        """The container's name."""
        return self.spec.name


type Phase = Literal["Pending", "Running", "Succeeded", "Failed"]


@dataclass(slots=True)
class PodRecord:
    """A pod: its spec, owner, place on disk, sandbox and containers."""

    name: str
    owner_kind: str
    owner_name: str
    template_hash: str
    spec: PodSpec
    labels: Mapping[str, str]
    created: float
    directory: Path
    init_containers: list[ContainerRecord]
    containers: list[ContainerRecord]
    sandbox: Sandbox | None = None
    phase: Phase = "Pending"
    deleting_since: float | None = None
    grace_s: float = GRACE_S
    volumes_ready: bool = False
    message: str = ""

    @property
    def ip(self) -> str | None:
        """The pod's IP, once it has one."""
        return self.sandbox.ip if self.sandbox else None

    @property
    def hosts_path(self) -> Path:
        """Its /etc/hosts on the host."""
        return self.directory / "hosts"

    def all_containers(self) -> list[ContainerRecord]:
        """Init containers, then containers."""
        return [*self.init_containers, *self.containers]

    @property
    def ready(self) -> bool:
        """Kubernetes' Ready condition: running, every container ready, not terminating."""
        return (
            self.deleting_since is None
            and self.phase == "Running"
            and all(c.state == "Running" and c.ready for c in self.containers)
        )


@dataclass(slots=True)
class JobRecord:
    """A Job's progress: failed pods counted once each."""

    failures: int = 0
    counted: set[str] = field(default_factory=set)
    succeeded: bool = False
    failed: bool = False
    completed_at: float | None = None
    next_pod: float = 0.0


@dataclass(slots=True)
class ServiceRecord:
    """A Service's ClusterIP and what it forwards to."""

    ip: str
    endpoints: list[tuple[int, list[tuple[str, int]]]] = field(default_factory=list)


class ApplyError(Exception):
    """An object the cluster refuses (as the API server would)."""


API_NAMES = {
    "Deployment": "deployment.apps",
    "Job": "job.batch",
    "CronJob": "cronjob.batch",
    "Service": "service",
    "ConfigMap": "configmap",
    "Namespace": "namespace",
}
"""kubectl's `<resource>.<group>` in apply's output."""


def api_name(obj: KubeObject) -> str:
    """`deployment.apps/user-postgres`, as kubectl names an object."""
    return f"{API_NAMES.get(kind_of(obj), kind_of(obj).lower())}/{name_of(obj)}"


class Cluster:
    """The simulated gluon-local: objects, pods, Services; `tick` reconciles them.

    Thread-safe: the node's control server and its loop share it under one lock.
    """

    def __init__(
        self,
        runtime: Runtime,
        state: Path,
        clock: Callable[[], float],
        tools: HostTools | None = None,
        backoff_base_s: float = 10.0,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.state = state
        self.clock = clock
        self.tools = tools or HostTools()
        self.backoff_base_s = backoff_base_s
        self.spawn = spawn or (lambda work: threading.Thread(target=work, daemon=True).start())
        self.lock = threading.RLock()
        self.objects: dict[tuple[str, str], KubeObject] = {}
        self.pods: dict[str, PodRecord] = {}
        self.jobs: dict[str, JobRecord] = {}
        self.services: dict[str, ServiceRecord] = {}
        self.releases: dict[str, int] = {}
        """Helm releases installed, by name: their revision."""
        self.restarts: dict[str, str] = {}
        """`rollout restart` stamps by Deployment (Kubernetes' restartedAt annotation)."""
        self.events: list[str] = []
        self.starts_left = STARTS_PER_TICK

    # --- the API --------------------------------------------------------------------

    def apply(self, items: Sequence[Loaded]) -> list[str]:
        """Stores each object (kubectl apply): `<api name> created|configured|unchanged`,
        or an `error:` line for what isn't simulated or can't change."""
        lines = []
        with self.lock:
            for item in items:
                obj = item.obj
                if isinstance(obj, NotSimulated):
                    lines.append(f"{obj.kind}/{obj.name} skipped: not simulated by gluon-kube")
                    continue
                if not item.supported:
                    notes = "; ".join(
                        f"{n.path} ({n.detail})" for n in item.notes if n.status == "unsupported"
                    )
                    lines.append(f"error: {api_name(obj)} not applied: unsupported: {notes}")
                    continue
                key = (kind_of(obj), name_of(obj))
                old = self.objects.get(key)
                if old == obj:
                    lines.append(f"{api_name(obj)} unchanged")
                    continue
                if isinstance(obj, Job) and isinstance(old, Job):
                    lines.append(
                        f'error: The Job "{obj.meta.name}" is invalid: spec.template: '
                        "field is immutable"
                    )
                    continue
                self.objects[key] = obj
                lines.append(f"{api_name(obj)} {'configured' if old else 'created'}")
                if isinstance(obj, ConfigMap):
                    self._refresh_config_map(obj)
        return lines

    def delete(self, kind: str, name: str) -> bool:
        """Deletes an object and what it owns; False if there was none."""
        with self.lock:
            obj = self.objects.pop((kind, name), None)
            if obj is None:
                return False
            now = self.clock()
            for pod in self.pods.values():
                if (pod.owner_kind, pod.owner_name) == (kind, name):
                    self._delete_pod(pod, now)
            if kind == "Job":
                self.jobs.pop(name, None)
            if kind == "Service" and name in self.services:
                record = self.services.pop(name)
                self.runtime.remove_service(name, record.ip)
                self._rewrite_hosts()
            return True

    def get(self, kind: str, name: str) -> KubeObject | None:
        """An applied object."""
        with self.lock:
            return self.objects.get((kind, name))

    def run_pod(self, name: str, image: str, argv: Sequence[str], env: Mapping[str, str]) -> str:
        """A pod outside any controller (`gluon-kube run`): one container, never restarted."""
        container = Container(name=name, image=image, command=tuple(argv))
        spec = PodSpec(containers=(container,), restart_policy="Never")
        with self.lock:
            pod = self._new_pod(name, "Run", name, "run", spec, {"run": name})
            pod.grace_s = 1.0  # `sleep` as pid 1 ignores SIGTERM
            for record in pod.containers:
                record.env.update(env)
            return pod.name

    def job_from_cron_job(self, cron_job: str, name: str) -> str:
        """`create job NAME --from=cronjob/CRON`: a Job from the CronJob's job template, run
        now (CronJob schedules aren't simulated; ADR 0030 decision 5)."""
        with self.lock:
            source = self.objects.get(("CronJob", cron_job))
            if not isinstance(source, CronJob):
                raise ApplyError(f'cronjobs.batch "{cron_job}" not found')
            if ("Job", name) in self.objects:
                raise ApplyError(f'jobs.batch "{name}" already exists')
            job = Job(
                ObjectMeta(name=name, namespace=source.meta.namespace),
                source.job.template,
                source.job.backoff_limit,
            )
            self.objects[("Job", name)] = job
            return f"job.batch/{name} created"

    def delete_pod(self, name: str) -> bool:
        """Deletes a pod (its controller makes another, as `kubectl delete pod` does)."""
        with self.lock:
            pod = self.pods.get(name)
            if pod is None:
                return False
            self._delete_pod(pod, self.clock())
            return True

    def restart_rollout(self, name: str) -> bool:
        """`rollout restart`: a new template (Kubernetes changes an annotation); the
        Deployment's pods roll."""
        with self.lock:
            if not isinstance(self.objects.get(("Deployment", name)), Deployment):
                return False
            self.restarts[name] = f"{self.clock():.6f}"
            return True

    # --- views ------------------------------------------------------------------------

    def pods_of(self, kind: str, name: str) -> list[PodRecord]:
        """The pods an object owns, oldest first, terminating ones last."""
        with self.lock:
            owned = [p for p in self.pods.values() if (p.owner_kind, p.owner_name) == (kind, name)]
        return sorted(owned, key=lambda p: (p.deleting_since is not None, p.created))

    def job_status(self, name: str) -> JobRecord | None:
        """A Job's progress."""
        with self.lock:
            return self.jobs.get(name)

    def rollout_done(self, name: str) -> tuple[bool, str]:
        """`rollout status`: every pod of the current template Ready and no old pod left."""
        with self.lock:
            obj = self.objects.get(("Deployment", name))
            if not isinstance(obj, Deployment):
                return False, f'deployments.apps "{name}" not found'
            current = self.deployment_hash(obj)
            pods = self.pods_of("Deployment", name)
            new = [p for p in pods if p.template_hash == current and p.deleting_since is None]
            old = [p for p in pods if p.template_hash != current or p.deleting_since is not None]
            ready = sum(1 for p in new if p.ready)
            if old:
                return False, (
                    f'Waiting for deployment "{name}" rollout to finish: {len(old)} old '
                    "replicas are pending termination..."
                )
            if ready < obj.replicas:
                return False, (
                    f'Waiting for deployment "{name}" rollout to finish: {ready} of '
                    f"{obj.replicas} updated replicas are available..."
                )
            return True, f'deployment "{name}" successfully rolled out'

    def deployment_hash(self, obj: Deployment) -> str:
        """The template hash of a Deployment's current pods (restarts included)."""
        return template_hash((obj.template, self.restarts.get(obj.meta.name)))

    # --- the loop ---------------------------------------------------------------------

    def tick(self) -> None:
        """One pass of every controller and the kubelet."""
        with self.lock:
            now = self.clock()
            self.starts_left = STARTS_PER_TICK
            for obj in list(self.objects.values()):
                if isinstance(obj, Deployment):
                    self._deployment(obj, now)
                elif isinstance(obj, Job):
                    self._job(obj, now)
            for pod in list(self.pods.values()):
                self._kubelet(pod, now)
            self._services()

    def shutdown(self) -> None:
        """Kills every container and sandbox at once (the node is stopping)."""
        with self.lock:
            for pod in list(self.pods.values()):
                for container in pod.all_containers():
                    if container.process is not None and container.process.poll() is None:
                        container.process.kill()
                if pod.sandbox is not None:
                    self.runtime.stop_sandbox(pod)
                    pod.sandbox = None
            for name, record in list(self.services.items()):
                self.runtime.remove_service(name, record.ip)
            self.services.clear()
            self.pods.clear()

    # --- controllers ------------------------------------------------------------------

    def _deployment(self, obj: Deployment, now: float) -> None:
        """Keeps `replicas` pods of the current template; rolls old ones out once the new
        are Ready (RollingUpdate, surge first)."""
        current = self.deployment_hash(obj)
        pods = [p for p in self.pods_of("Deployment", obj.meta.name) if p.deleting_since is None]
        new = [p for p in pods if p.template_hash == current]
        old = [p for p in pods if p.template_hash != current]
        for _ in range(obj.replicas - len(new)):
            self._new_pod(
                pod_name(obj.meta.name, current),
                "Deployment",
                obj.meta.name,
                current,
                obj.template.spec,
                obj.template.labels,
            )
        for extra in new[obj.replicas :]:
            self._delete_pod(extra, now)
        if old and sum(1 for p in new if p.ready) >= obj.replicas:
            for pod in old:
                self._delete_pod(pod, now)

    def _job(self, obj: Job, now: float) -> None:
        """One pod at a time until one succeeds, or failures exceed backoffLimit."""
        record = self.jobs.setdefault(obj.meta.name, JobRecord())
        if record.succeeded or record.failed:
            return
        pods = [p for p in self.pods_of("Job", obj.meta.name) if p.deleting_since is None]
        if any(p.phase == "Succeeded" for p in pods):
            record.succeeded, record.completed_at = True, now
            self.events.append(f"job/{obj.meta.name} complete")
            return
        for pod in pods:
            if pod.phase == "Failed" and pod.name not in record.counted:
                record.counted.add(pod.name)
                record.failures += 1
                record.next_pod = now + backoff(record.failures, self.backoff_base_s)
        # Retries: failed pods, and container restarts under OnFailure (kubernetes.io
        # "Jobs": pod backoff failure policy).
        retries = record.failures + sum(
            c.restarts for p in pods if p.phase != "Failed" for c in p.all_containers()
        )
        if retries > obj.backoff_limit:
            record.failed = True
            self.events.append(f"job/{obj.meta.name} failed: BackoffLimitExceeded")
            for pod in pods:
                if pod.phase != "Failed":
                    self._stop(pod)
                    pod.phase = "Failed"
            return
        if not any(p.phase in ("Pending", "Running") for p in pods) and now >= record.next_pod:
            self._new_pod(
                f"{obj.meta.name}-{random_suffix()}",
                "Job",
                obj.meta.name,
                "job",
                obj.template.spec,
                {**obj.template.labels, "job-name": obj.meta.name},
            )

    def _new_pod(
        self,
        name: str,
        owner_kind: str,
        owner_name: str,
        template: str,
        spec: PodSpec,
        labels: Mapping[str, str],
    ) -> PodRecord:
        directory = self.state / "pods" / name
        directory.mkdir(parents=True, exist_ok=True)

        def record(container: Container, init: bool) -> ContainerRecord:
            recipe = recipe_for(container.image, self.tools)
            result = ContainerRecord(container, init, recipe)
            result.log = directory / f"{container.name}.log"
            if isinstance(recipe, Recipe):
                result.argv = command_line(container, recipe.config.entrypoint, recipe.config.cmd)
                extra = egress_env() if recipe.egress else None
                result.env = container_env(recipe.image_env(), container, name, recipe.home, extra)
            else:
                result.reason = "ImageUnavailable"
            return result

        pod = PodRecord(
            name=name,
            owner_kind=owner_kind,
            owner_name=owner_name,
            template_hash=template,
            spec=spec,
            labels=dict(labels),
            created=self.clock(),
            directory=directory,
            init_containers=[record(c, True) for c in spec.init_containers],
            containers=[record(c, False) for c in spec.containers],
        )
        self.pods[name] = pod
        return pod

    def _delete_pod(self, pod: PodRecord, now: float) -> None:
        if pod.deleting_since is None:
            pod.deleting_since = now

    # --- the kubelet ------------------------------------------------------------------

    def _kubelet(self, pod: PodRecord, now: float) -> None:
        if pod.deleting_since is not None:
            self._terminate(pod, now)
            return
        if any(isinstance(c.recipe, Unavailable) for c in pod.all_containers()):
            for container in pod.all_containers():
                container.reason = (
                    "ImageUnavailable" if isinstance(container.recipe, Unavailable) else "Waiting"
                )
            return
        if pod.phase in ("Succeeded", "Failed"):
            return
        if pod.sandbox is None:
            if not self._volumes(pod) or self.starts_left <= 0:
                return
            self.starts_left -= 1
            pod.sandbox = self.runtime.start_sandbox(pod)
            self.runtime.write_file(pod.hosts_path, self._hosts(pod))
        for init in pod.init_containers:  # in order, each to completion
            if init.reason == "Completed":
                continue
            self._run_container(pod, init, now)
            if init.reason != "Completed":
                return
        if pod.phase == "Failed":
            return  # an init container failed the pod
        pod.phase = "Running"
        for container in pod.containers:
            self._run_container(pod, container, now)
            self._probe(pod, container, now)
        finished = [c for c in pod.containers if c.state == "Terminated" and c.next_start < 0]
        if len(finished) == len(pod.containers):
            failed = any(c.exit_code != 0 for c in finished)
            pod.phase = "Failed" if failed else "Succeeded"
            self._stop(pod)

    def _volumes(self, pod: PodRecord) -> bool:
        """emptyDirs made and ConfigMaps written into the pod's directory; False (and the
        pod waits, as the kubelet's FailedMount) while a ConfigMap it needs is missing."""
        if pod.volumes_ready:
            return True
        for volume in pod.spec.volumes:
            path = pod.directory / "volumes" / volume.name
            source = volume.source
            if isinstance(source, ConfigMapVolume):
                config_map = self.objects.get(("ConfigMap", source.name))
                if not isinstance(config_map, ConfigMap) and not source.optional:
                    pod.message = f'configmap "{source.name}" not found'
                    return False
                path.mkdir(parents=True, exist_ok=True)
                if isinstance(config_map, ConfigMap):
                    for key, value in config_map.data.items():
                        (path / key).write_text(value)
            else:
                path.mkdir(parents=True, exist_ok=True)
        pod.volumes_ready, pod.message = True, ""
        return True

    def _refresh_config_map(self, config_map: ConfigMap) -> None:
        """A changed ConfigMap reaches the pods that mount it (the kubelet syncs it)."""
        for pod in self.pods.values():
            if not pod.volumes_ready:
                continue
            for volume in pod.spec.volumes:
                source = volume.source
                if isinstance(source, ConfigMapVolume) and source.name == config_map.meta.name:
                    path = pod.directory / "volumes" / volume.name
                    for key, value in config_map.data.items():
                        self.runtime.write_file(path / key, value)

    def _run_container(self, pod: PodRecord, container: ContainerRecord, now: float) -> None:
        """Starts a waiting container when its backoff allows; notices one that exited and
        decides, by restartPolicy, whether and when it starts again."""
        if container.state == "Running" and container.process is not None:
            code = container.process.poll()
            if code is None:
                return
            self._exited(pod, container, code, now)
        if container.state == "Terminated" and container.next_start < 0:
            return
        if container.state != "Running" and now >= container.next_start and self.starts_left > 0:
            self.starts_left -= 1
            self._start(pod, container, now)

    def _start(self, pod: PodRecord, container: ContainerRecord, now: float) -> None:
        if container.log.exists():
            container.log.replace(container.log.with_suffix(".previous.log"))
        if container.started_at is not None:
            container.restarts += 1  # counted when it starts again, as kubectl shows it
        container.process = self.runtime.start_container(pod, container)
        container.state, container.reason, container.exit_code = "Running", "", None
        container.started_at = now
        container.started = container.spec.startup is None
        container.ready = False
        container.probes = {
            kind: ProbeTracker(probe, now)
            for kind, probe in (
                ("startup", container.spec.startup),
                ("liveness", container.spec.liveness),
                ("readiness", container.spec.readiness),
            )
            if probe is not None
        }

    def _exited(self, pod: PodRecord, container: ContainerRecord, code: int, now: float) -> None:
        """A container ended with `code`: Completed or Error, and maybe restarted later."""
        container.state, container.exit_code, container.ready = "Terminated", code, False
        container.process = None
        container.reason = "Completed" if code == 0 else "Error"
        policy = pod.spec.restart_policy
        restart = policy == "Always" or (policy == "OnFailure" and code != 0)
        if container.init and code == 0:
            restart = False
        if not restart:
            container.next_start = -1.0
            if container.init and code != 0:
                pod.phase = "Failed"
                self._stop(pod)
            return
        ran = now - (container.started_at or now)
        if ran >= BACKOFF_RESET_S * self.backoff_base_s / 10.0:
            container.crashes = 0
        container.next_start = now + backoff(container.crashes, self.backoff_base_s)
        container.crashes += 1
        if container.next_start > now:
            container.reason = "CrashLoopBackOff"

    def _probe(self, pod: PodRecord, container: ContainerRecord, now: float) -> None:
        """Runs due probes in the background and acts on their outcomes."""
        if container.state != "Running":
            return
        for kind, tracker in container.probes.items():
            if kind != "startup" and not container.started:
                continue
            if kind == "startup" and container.started:
                continue
            if tracker.due(now):
                tracker.begin(now)
                self.spawn(self._probe_work(pod, container, kind, tracker))
        startup = container.probes.get("startup")
        if startup is not None and not container.started:
            if startup.passed is True:
                container.started = True
            elif startup.passed is False:
                self._kill_for(pod, container, "startup probe failed", now)
                return
        liveness = container.probes.get("liveness")
        if liveness is not None and liveness.passed is False:
            self._kill_for(pod, container, "liveness probe failed", now)
            return
        readiness = container.probes.get("readiness")
        if readiness is None:
            container.ready = container.started
        elif readiness.passed is not None:
            container.ready = container.started and readiness.passed

    def _probe_work(
        self, pod: PodRecord, container: ContainerRecord, kind: str, tracker: ProbeTracker
    ) -> Callable[[], None]:
        def work() -> None:
            try:
                ok = self.runtime.probe(pod, container, tracker.probe)
            except OSError:
                ok = False
            with self.lock:
                tracker.record(ok)

        return work

    def _kill_for(self, pod: PodRecord, container: ContainerRecord, why: str, now: float) -> None:
        """Kills a container the kubelet gave up on; it restarts by restartPolicy."""
        self.events.append(f"pod/{pod.name} container {container.name}: {why}, restarting")
        if container.process is not None:
            container.process.kill()
            code = container.process.poll()
            self._exited(pod, container, 137 if code is None else code, now)
            if container.state == "Terminated":
                container.reason = "Error"

    def _terminate(self, pod: PodRecord, now: float) -> None:
        """A deleted pod: SIGTERM, SIGKILL after the grace period, then gone."""
        running = [
            c for c in pod.all_containers() if c.process is not None and c.process.poll() is None
        ]
        for container in running:
            if container.stopping_since is None:
                container.stopping_since = now
                container.process.terminate()  # type: ignore[union-attr]  # checked above
            elif now - container.stopping_since >= pod.grace_s:
                container.process.kill()  # type: ignore[union-attr]
        if running and now - (pod.deleting_since or now) < pod.grace_s + 1:
            return
        self._stop(pod)
        self.pods.pop(pod.name, None)

    def _stop(self, pod: PodRecord) -> None:
        """Ends whatever still runs in a pod and its sandbox."""
        for container in pod.all_containers():
            if container.process is not None and container.process.poll() is None:
                container.process.kill()
        if pod.sandbox is not None:
            self.runtime.stop_sandbox(pod)
            pod.sandbox = None

    # --- Services ---------------------------------------------------------------------

    def _services(self) -> None:
        """Each Service's ClusterIP forwarding to its Ready pods; hosts files follow the
        set of Services."""
        names_changed = False
        wanted = {
            name: obj
            for (kind, name), obj in self.objects.items()
            if kind == "Service" and isinstance(obj, Service)
        }
        for name, record in list(self.services.items()):
            if name not in wanted:
                self.runtime.remove_service(name, record.ip)
                del self.services[name]
                names_changed = True
        for name, service in wanted.items():
            existing = self.services.get(name)
            if existing is None:
                taken = {r.ip: n for n, r in self.services.items()}
                existing = ServiceRecord(cluster_ip(name, taken))
                self.services[name] = existing
                names_changed = True
            record = existing
            endpoints = self._endpoints(service)
            if endpoints != record.endpoints or names_changed:
                record.endpoints = endpoints
                self.runtime.set_service(name, record.ip, endpoints)
        if names_changed:
            self._rewrite_hosts()

    def _endpoints(self, service: Service) -> list[tuple[int, list[tuple[str, int]]]]:
        selected = [
            pod
            for pod in self.pods.values()
            if pod.ready
            and pod.ip is not None
            and all(pod.labels.get(k) == v for k, v in service.selector.items())
        ]
        result = []
        for port in service.ports:
            targets = []
            for pod in sorted(selected, key=lambda p: p.name):
                ports = [p for c in pod.spec.containers for p in c.ports]
                number = resolve_port(port.target_port, ports)
                if number is not None and pod.ip is not None:
                    targets.append((pod.ip, number))
            result.append((port.port, targets))
        return result

    def _hosts(self, pod: PodRecord) -> str:
        services = {name: record.ip for name, record in self.services.items()}
        return hosts_file(pod.name, pod.ip or "", services)

    def _rewrite_hosts(self) -> None:
        for pod in self.pods.values():
            if pod.sandbox is not None:
                self.runtime.write_file(pod.hosts_path, self._hosts(pod))

    # --- reporting ----------------------------------------------------------------------

    def namespace_applied(self) -> bool:
        """Whether a Namespace object was applied (informational)."""
        return any(isinstance(obj, Namespace) for obj in self.objects.values())

    def cron_jobs(self) -> list[CronJob]:
        """The CronJobs (run by hand only)."""
        return [obj for obj in self.objects.values() if isinstance(obj, CronJob)]
