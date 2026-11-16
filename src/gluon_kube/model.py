# SPDX-License-Identifier: MIT

"""The Kubernetes objects gluon-kube simulates (gluon ADR 0030 decision 5), as read from
gluon-local's manifests: only the fields those manifests and the chart use. Defaults are
Kubernetes' documented ones, applied where the API applies them."""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ObjectMeta:
    """metadata: a name, the namespace if given (else the one applied to), labels."""

    name: str
    namespace: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EnvVar:
    """A container env var with a literal value (valueFrom isn't used by gluon-local)."""

    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ContainerPort:
    """A port a container declares (documentation in Kubernetes; Services name it)."""

    container_port: int
    name: str | None = None
    protocol: str = "TCP"


@dataclass(frozen=True, slots=True)
class HttpGet:
    """An httpGet probe: GET path on a port (number or a container port's name)."""

    path: str
    port: int | str
    scheme: str = "HTTP"


@dataclass(frozen=True, slots=True)
class ExecAction:
    """An exec probe: the command run in the container; exit 0 is success."""

    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TcpSocket:
    """A tcpSocket probe: a connection to the port succeeds."""

    port: int | str


type ProbeHandler = HttpGet | ExecAction | TcpSocket


@dataclass(frozen=True, slots=True)
class Probe:
    """A startup, liveness or readiness probe, with Kubernetes' defaults."""

    handler: ProbeHandler
    initial_delay_seconds: int = 0
    period_seconds: int = 10
    timeout_seconds: int = 1
    success_threshold: int = 1
    failure_threshold: int = 3


@dataclass(frozen=True, slots=True)
class VolumeMount:
    """A volume mounted into a container at an absolute path."""

    name: str
    mount_path: str
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class Container:
    """A container: its image (a recipe in gluon-kube), command line, env, ports,
    probes and mounts. Resources and pull policy are recorded, not enforced."""

    name: str
    image: str
    command: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    env: tuple[EnvVar, ...] = ()
    ports: tuple[ContainerPort, ...] = ()
    volume_mounts: tuple[VolumeMount, ...] = ()
    startup: Probe | None = None
    liveness: Probe | None = None
    readiness: Probe | None = None


@dataclass(frozen=True, slots=True)
class EmptyDir:
    """An emptyDir volume: a directory per pod, deleted with it."""


@dataclass(frozen=True, slots=True)
class ConfigMapVolume:
    """A ConfigMap's keys as files (defaultMode recorded, not enforced)."""

    name: str
    optional: bool = False


type VolumeSource = EmptyDir | ConfigMapVolume


@dataclass(frozen=True, slots=True)
class Volume:
    """A pod volume, by name."""

    name: str
    source: VolumeSource


@dataclass(frozen=True, slots=True)
class PodSpec:
    """What a pod runs: init containers in order, then its containers."""

    containers: tuple[Container, ...]
    init_containers: tuple[Container, ...] = ()
    volumes: tuple[Volume, ...] = ()
    restart_policy: str = "Always"
    enable_service_links: bool = True


@dataclass(frozen=True, slots=True)
class PodTemplate:
    """A pod template: its labels (what Services select) and spec."""

    labels: Mapping[str, str]
    spec: PodSpec


@dataclass(frozen=True, slots=True)
class Deployment:
    """A Deployment: replicas of a pod template."""

    meta: ObjectMeta
    selector: Mapping[str, str]
    template: PodTemplate
    replicas: int = 1


@dataclass(frozen=True, slots=True)
class ServicePort:
    """A Service port and the pod port it forwards to (a number or a port's name)."""

    port: int
    target_port: int | str
    name: str | None = None
    protocol: str = "TCP"


@dataclass(frozen=True, slots=True)
class Service:
    """A ClusterIP Service: a name, the pods it selects, its ports."""

    meta: ObjectMeta
    selector: Mapping[str, str]
    ports: tuple[ServicePort, ...]


@dataclass(frozen=True, slots=True)
class ConfigMap:
    """A ConfigMap: string data by key."""

    meta: ObjectMeta
    data: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Job:
    """A Job: a pod template run to completion, retried up to backoffLimit."""

    meta: ObjectMeta
    template: PodTemplate
    backoff_limit: int = 6


@dataclass(frozen=True, slots=True)
class CronJob:
    """A CronJob: its schedule is recorded; its job is run by hand only."""

    meta: ObjectMeta
    schedule: str
    job: Job


@dataclass(frozen=True, slots=True)
class Namespace:
    """A Namespace: gluon-local is the only one simulated."""

    meta: ObjectMeta


@dataclass(frozen=True, slots=True)
class NotSimulated:
    """A kind gluon-kube recognises but doesn't simulate (DaemonSet, RBAC, …)."""

    kind: str
    name: str


type KubeObject = Deployment | Service | ConfigMap | Job | CronJob | Namespace | NotSimulated
