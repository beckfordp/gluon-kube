# SPDX-License-Identifier: MIT

"""Reading manifests into the model (gluon ADR 0030 decision 2; plan GK-3.1, GK-3.2).

Every field of every object is accounted for: read into the model (simulated),
recorded but not enforced (a Note with status "recorded"), or unsupported (a Note with
status "unsupported", and the object isn't started). Nothing is dropped silently.
Malformed input raises ManifestError naming its source (`file#document`).
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from gluon_kube.model import (
    ConfigMap,
    ConfigMapVolume,
    Container,
    ContainerPort,
    CronJob,
    Deployment,
    EmptyDir,
    EnvVar,
    ExecAction,
    HttpGet,
    Job,
    KubeObject,
    Namespace,
    NotSimulated,
    ObjectMeta,
    PodSpec,
    PodTemplate,
    Probe,
    ProbeHandler,
    Service,
    ServicePort,
    TcpSocket,
    Volume,
    VolumeMount,
    VolumeSource,
)

NAMESPACE = "gluon-local"
"""The one namespace simulated (ADR 0030 decision 5)."""

API_VERSIONS = {
    "Namespace": "v1",
    "Service": "v1",
    "ConfigMap": "v1",
    "Deployment": "apps/v1",
    "Job": "batch/v1",
    "CronJob": "batch/v1",
}
"""The simulated kinds and the API version each is read as."""

MANIFEST_SUFFIXES = (".yaml", ".yml")


class ManifestError(Exception):
    """Input that isn't a manifest gluon-kube can read; the message names its source."""


@dataclass(frozen=True, slots=True)
class Note:
    """A field not simulated: recorded (kept, not enforced) or unsupported."""

    path: str
    status: Literal["recorded", "unsupported"]
    detail: str


@dataclass(frozen=True, slots=True)
class Loaded:
    """An object read from `source` (`file#document`), with its notes."""

    obj: KubeObject
    source: str
    notes: tuple[Note, ...] = ()

    @property
    def supported(self) -> bool:
        """No field is unsupported: the object can be simulated as written."""
        return all(note.status != "unsupported" for note in self.notes)


def type_name(value: object) -> str:
    """A YAML-ish name for a Python value's type, for error messages."""
    return type(value).__name__


@dataclass(slots=True)
class Fields:
    """A mapping being read: each key taken is accounted for; `done()` reports the rest
    as unsupported. Wrong types raise ManifestError with the source and path."""

    data: Mapping[str, object]
    path: str
    source: str
    notes: list[Note]
    taken: set[str] = field(default_factory=set)

    def _at(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def _error(self, key: str, expected: str, value: object) -> ManifestError:
        return ManifestError(
            f"{self.source}: {self._at(key)}: expected {expected}, got {type_name(value)}"
        )

    def has(self, key: str) -> bool:
        """The key is present (not taken by asking)."""
        return key in self.data

    def raw(self, key: str) -> object:
        """The value as it is, taken."""
        self.taken.add(key)
        return self.data.get(key)

    def string(self, key: str, default: str | None = None) -> str | None:
        """A string field (numbers and booleans are refused, as the API does)."""
        value = self.raw(key)
        if value is None:
            return default
        if not isinstance(value, str):
            raise self._error(key, "a string", value)
        return value

    def required_string(self, key: str) -> str:
        """A string field that must be present."""
        value = self.string(key)
        if value is None:
            raise ManifestError(f"{self.source}: {self._at(key)} is required")
        return value

    def integer(self, key: str, default: int) -> int:
        """An integer field."""
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._error(key, "an integer", value)
        return value

    def int_or_string(self, key: str) -> int | str | None:
        """A port given as a number or a name."""
        value = self.raw(key)
        if value is None or (isinstance(value, (int, str)) and not isinstance(value, bool)):
            return value
        raise self._error(key, "an integer or a string", value)

    def boolean(self, key: str, default: bool) -> bool:
        """A boolean field."""
        value = self.raw(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self._error(key, "a boolean", value)
        return value

    def mapping(self, key: str) -> "Fields | None":
        """A nested object, or None if absent."""
        value = self.raw(key)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise self._error(key, "a mapping", value)
        return Fields(value, self._at(key), self.source, self.notes)

    def items(self, key: str) -> list["Fields"]:
        """A list of objects (absent: empty)."""
        value = self.raw(key)
        if value is None:
            return []
        if not isinstance(value, list):
            raise self._error(key, "a list", value)
        result = []
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise self._error(f"{key}[{index}]", "a mapping", item)
            result.append(Fields(item, f"{self._at(key)}[{index}]", self.source, self.notes))
        return result

    def strings(self, key: str) -> tuple[str, ...]:
        """A list of strings (absent: empty)."""
        value = self.raw(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise self._error(key, "a list of strings", value)
        return tuple(value)

    def string_map(self, key: str) -> dict[str, str]:
        """A map of strings (labels, selectors, ConfigMap data; absent: empty)."""
        value = self.raw(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise self._error(key, "a map of strings", value)
        return dict(value)

    def record(self, key: str, detail: str) -> None:
        """Keeps a field that is read but not enforced, as a note."""
        if key in self.data:
            self.taken.add(key)
            self.notes.append(Note(self._at(key), "recorded", detail))

    def unsupported(self, key: str, detail: str) -> None:
        """Reports a present field as unsupported, with why."""
        self.taken.add(key)
        self.notes.append(Note(self._at(key), "unsupported", detail))

    def done(self) -> None:
        """Every key not taken is unsupported: reported, never dropped."""
        for key in self.data:
            if key not in self.taken:
                self.unsupported(key, "not simulated by gluon-kube")


def parse(document: object, source: str) -> Loaded:
    """One manifest document into the model, with its notes."""
    if not isinstance(document, Mapping):
        raise ManifestError(f"{source}: not a mapping")
    notes: list[Note] = []
    top = Fields(document, "", source, notes)
    kind = top.string("kind")
    if kind is None:
        raise ManifestError(f"{source}: no kind")
    api_version = top.string("apiVersion")
    meta_fields = top.mapping("metadata")
    if kind not in API_VERSIONS:
        # Reported, not read: a kind not simulated needn't even be an API object
        # (a Kustomization has no metadata).
        name = meta_fields.string("name") if meta_fields is not None else None
        return Loaded(NotSimulated(kind=kind, name=name or "-"), source)
    if meta_fields is None:
        raise ManifestError(f"{source}: metadata.name is required")
    if api_version != API_VERSIONS[kind]:
        top.notes.append(
            Note("apiVersion", "unsupported", f"{kind} is read as {API_VERSIONS[kind]}")
        )
    meta = object_meta(meta_fields)
    obj = PARSERS[kind](meta, top)
    top.done()
    return Loaded(obj, source, tuple(notes))


def object_meta(fields: Fields) -> ObjectMeta:
    """metadata: name, namespace (gluon-local or none), labels."""
    name = fields.required_string("name")
    namespace = fields.string("namespace")
    if namespace is not None and namespace != NAMESPACE:
        fields.unsupported("namespace", f"only {NAMESPACE} is simulated")
    labels = fields.string_map("labels")
    fields.done()
    return ObjectMeta(name=name, namespace=namespace, labels=labels)


def parse_namespace(meta: ObjectMeta, top: Fields) -> Namespace:
    """A Namespace: metadata only."""
    top.mapping("spec")  # a Namespace's spec (finalizers) is the API's, not the manifest's
    return Namespace(meta)


def parse_config_map(meta: ObjectMeta, top: Fields) -> ConfigMap:
    """A ConfigMap: string data."""
    return ConfigMap(meta, top.string_map("data"))


def parse_service(meta: ObjectMeta, top: Fields) -> Service:
    """A ClusterIP Service."""
    spec = top.mapping("spec") or Fields({}, "spec", top.source, top.notes)
    service_type = spec.string("type", "ClusterIP")
    if service_type != "ClusterIP":
        spec.unsupported("type", "only ClusterIP Services are simulated")
    selector = spec.string_map("selector")
    ports = []
    for port_fields in spec.items("ports"):
        port = port_fields.integer("port", 0)
        if port == 0:
            raise ManifestError(f"{top.source}: {port_fields.path}.port is required")
        target = port_fields.int_or_string("targetPort")
        ports.append(
            ServicePort(
                port=port,
                target_port=port if target is None else target,
                name=port_fields.string("name"),
                protocol=port_fields.string("protocol", "TCP") or "TCP",
            )
        )
        port_fields.done()
    spec.done()
    return Service(meta, selector, tuple(ports))


def parse_deployment(meta: ObjectMeta, top: Fields) -> Deployment:
    """A Deployment: replicas of a pod template, selected by matchLabels."""
    spec = required(top, "spec")
    replicas = spec.integer("replicas", 1)
    selector_fields = required(spec, "selector")
    selector = selector_fields.string_map("matchLabels")
    selector_fields.done()
    template = pod_template(required(spec, "template"))
    spec.done()
    return Deployment(meta, selector, template, replicas)


def parse_job(meta: ObjectMeta, top: Fields) -> Job:
    """A Job."""
    return job_from(meta, required(top, "spec"))


def job_from(meta: ObjectMeta, spec: Fields) -> Job:
    """A Job from its spec (a Job's, or a CronJob's jobTemplate.spec)."""
    backoff_limit = spec.integer("backoffLimit", 6)
    template = pod_template(required(spec, "template"))
    spec.done()
    return Job(meta, template, backoff_limit)


def parse_cron_job(meta: ObjectMeta, top: Fields) -> CronJob:
    """A CronJob: the schedule recorded, the job run by hand only."""
    spec = required(top, "spec")
    schedule = spec.required_string("schedule")
    job_template = required(spec, "jobTemplate")
    job_meta_fields = job_template.mapping("metadata")
    if job_meta_fields is not None:
        job_meta_fields.done()
    job = job_from(meta, required(job_template, "spec"))
    job_template.done()
    spec.done()
    return CronJob(meta, schedule, job)


PARSERS: dict[str, Callable[[ObjectMeta, Fields], KubeObject]] = {
    "Namespace": parse_namespace,
    "ConfigMap": parse_config_map,
    "Service": parse_service,
    "Deployment": parse_deployment,
    "Job": parse_job,
    "CronJob": parse_cron_job,
}


def required(fields: Fields, key: str) -> Fields:
    """A nested object that must be present."""
    value = fields.mapping(key)
    if value is None:
        raise ManifestError(f"{fields.source}: {fields._at(key)} is required")
    return value


def pod_template(fields: Fields) -> PodTemplate:
    """A pod template: labels and spec."""
    labels: dict[str, str] = {}
    meta = fields.mapping("metadata")
    if meta is not None:
        labels = meta.string_map("labels")
        meta.done()
    spec = required(fields, "spec")
    pod = PodSpec(
        containers=tuple(container(c) for c in spec.items("containers")),
        init_containers=tuple(container(c) for c in spec.items("initContainers")),
        volumes=tuple(volume(v) for v in spec.items("volumes")),
        restart_policy=spec.string("restartPolicy", "Always") or "Always",
        enable_service_links=spec.boolean("enableServiceLinks", True),
    )
    if not pod.containers:
        raise ManifestError(f"{fields.source}: {spec.path}.containers is required")
    spec.done()
    fields.done()
    return PodTemplate(labels, pod)


def container(fields: Fields) -> Container:
    """A container or init container."""
    name = fields.required_string("name")
    image = fields.required_string("image")
    fields.record("imagePullPolicy", "images aren't pulled: each maps to a local recipe")
    fields.record("resources", "requests and limits are recorded, not enforced")
    env = []
    for var in fields.items("env"):
        if var.has("valueFrom"):
            var.unsupported("valueFrom", "only literal env values are simulated")
        env.append(EnvVar(var.required_string("name"), var.string("value", "") or ""))
        var.done()
    ports = []
    for port in fields.items("ports"):
        number = port.integer("containerPort", 0)
        if number == 0:
            raise ManifestError(f"{fields.source}: {port.path}.containerPort is required")
        ports.append(
            ContainerPort(number, port.string("name"), port.string("protocol", "TCP") or "TCP")
        )
        port.done()
    mounts = []
    for mount in fields.items("volumeMounts"):
        mounts.append(
            VolumeMount(
                mount.required_string("name"),
                mount.required_string("mountPath"),
                mount.boolean("readOnly", False),
            )
        )
        mount.done()
    result = Container(
        name=name,
        image=image,
        command=fields.strings("command"),
        args=fields.strings("args"),
        env=tuple(env),
        ports=tuple(ports),
        volume_mounts=tuple(mounts),
        startup=probe(fields.mapping("startupProbe")),
        liveness=probe(fields.mapping("livenessProbe")),
        readiness=probe(fields.mapping("readinessProbe")),
    )
    fields.done()
    return result


def probe(fields: Fields | None) -> Probe | None:
    """A probe with Kubernetes' defaults; exactly one handler."""
    if fields is None:
        return None
    handlers: list[ProbeHandler] = []
    http = fields.mapping("httpGet")
    if http is not None:
        port = http.int_or_string("port")
        if port is None:
            raise ManifestError(f"{fields.source}: {http.path}.port is required")
        handlers.append(
            HttpGet(http.string("path", "/") or "/", port, http.string("scheme", "HTTP") or "HTTP")
        )
        http.done()
    command = fields.mapping("exec")
    if command is not None:
        handlers.append(ExecAction(command.strings("command")))
        command.done()
    tcp = fields.mapping("tcpSocket")
    if tcp is not None:
        port = tcp.int_or_string("port")
        if port is None:
            raise ManifestError(f"{fields.source}: {tcp.path}.port is required")
        handlers.append(TcpSocket(port))
        tcp.done()
    if len(handlers) != 1:
        raise ManifestError(
            f"{fields.source}: {fields.path}: expected one handler, got {len(handlers)}"
        )
    result = Probe(
        handler=handlers[0],
        initial_delay_seconds=fields.integer("initialDelaySeconds", 0),
        period_seconds=fields.integer("periodSeconds", 10),
        timeout_seconds=fields.integer("timeoutSeconds", 1),
        success_threshold=fields.integer("successThreshold", 1),
        failure_threshold=fields.integer("failureThreshold", 3),
    )
    fields.done()
    return result


def volume(fields: Fields) -> Volume:
    """A pod volume: emptyDir or configMap."""
    name = fields.required_string("name")
    source: VolumeSource | None = None
    empty = fields.mapping("emptyDir")
    if empty is not None:
        empty.done()
        source = EmptyDir()
    config_map = fields.mapping("configMap")
    if config_map is not None:
        config_map.record("defaultMode", "file modes are recorded, not enforced")
        source = ConfigMapVolume(
            config_map.required_string("name"), config_map.boolean("optional", False)
        )
        config_map.done()
    fields.done()
    if source is None:
        raise ManifestError(f"{fields.source}: {fields.path}: no emptyDir or configMap")
    return Volume(name, source)


def load(paths: Iterable[Path]) -> list[Loaded]:
    """Every document in `paths`: files as given, directories' *.yaml/*.yml sorted by
    name; empty documents skipped, each object's source `file#document`."""
    result: list[Loaded] = []
    for path in paths:
        if path.is_dir():
            files: Sequence[Path] = sorted(
                p for p in path.iterdir() if p.is_file() and p.suffix in MANIFEST_SUFFIXES
            )
        elif path.is_file():
            files = [path]
        else:
            raise ManifestError(f"{path}: no such file or directory")
        for file in files:
            result += load_file(file)
    return result


def load_file(path: Path) -> list[Loaded]:
    """Every non-empty document in one file."""
    try:
        documents = list(yaml.safe_load_all(path.read_text()))
    except yaml.YAMLError as error:
        raise ManifestError(f"{path}: not valid YAML: {error}") from None
    return [
        parse(document, f"{path}#{index}")
        for index, document in enumerate(documents, start=1)
        if document is not None
    ]
