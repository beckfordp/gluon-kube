# SPDX-License-Identifier: MIT

"""Reading manifests into the model (GK-3.1, GK-3.2): every field simulated, recorded or
reported; malformed input refused with its source. Expectations come from the
Kubernetes API's documented fields and defaults, never from gluon-kube's own output."""

import re
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from gluon_kube.manifests import ManifestError, load, parse
from gluon_kube.model import (
    ConfigMap,
    ConfigMapVolume,
    CronJob,
    Deployment,
    EmptyDir,
    ExecAction,
    HttpGet,
    Job,
    Namespace,
    NotSimulated,
    Service,
)


def doc(text: str) -> dict[str, object]:
    loaded = yaml.safe_load(dedent(text))
    assert isinstance(loaded, dict)
    return loaded


DEPLOYMENT = """\
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: kafka-connect
      namespace: gluon-local
      labels:
        app: kafka-connect
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: kafka-connect
      template:
        metadata:
          labels:
            app: kafka-connect
        spec:
          enableServiceLinks: false
          initContainers:
            - name: fetch
              image: curlimages/curl:8.11.1
              command: ["sh", "-c", "echo hi"]
              volumeMounts:
                - name: jmx
                  mountPath: /jmx
          containers:
            - name: connect
              image: debezium/connect:3.0.0.Final
              imagePullPolicy: IfNotPresent
              args: ["-c", "x=1"]
              ports:
                - containerPort: 8083
                - name: jmx
                  containerPort: 9404
              env:
                - name: GROUP_ID
                  value: "gluon-connect"
              resources:
                requests: {cpu: 250m, memory: 1Gi}
                limits: {cpu: 1000m, memory: 1Gi}
              readinessProbe:
                exec:
                  command: [curl, --fail, http://localhost:8083/]
                initialDelaySeconds: 15
                periodSeconds: 10
                timeoutSeconds: 10
              livenessProbe:
                httpGet:
                  path: /health
                  port: 8080
              volumeMounts:
                - name: jmx
                  mountPath: /jmx
                - name: jmx-config
                  mountPath: /jmx-config
                  readOnly: true
          volumes:
            - name: jmx
              emptyDir: {}
            - name: jmx-config
              configMap:
                name: kafka-connect-jmx-exporter
                defaultMode: 0444
                optional: false
"""


def test_a_deployment_reads_into_the_model() -> None:
    loaded = parse(doc(DEPLOYMENT), "kafka-connect.yaml#1")
    deployment = loaded.obj
    assert isinstance(deployment, Deployment)
    assert (deployment.meta.name, deployment.meta.namespace) == ("kafka-connect", "gluon-local")
    assert deployment.replicas == 1
    assert dict(deployment.selector) == {"app": "kafka-connect"}
    spec = deployment.template.spec
    assert spec.enable_service_links is False
    assert spec.restart_policy == "Always"  # the pod spec's documented default
    (init,) = spec.init_containers
    assert (init.name, init.image, init.command) == (
        "fetch",
        "curlimages/curl:8.11.1",
        ("sh", "-c", "echo hi"),
    )
    (connect,) = spec.containers
    assert connect.args == ("-c", "x=1")
    assert [(p.container_port, p.name) for p in connect.ports] == [(8083, None), (9404, "jmx")]
    assert [(e.name, e.value) for e in connect.env] == [("GROUP_ID", "gluon-connect")]
    assert connect.readiness is not None
    assert connect.readiness.handler == ExecAction(("curl", "--fail", "http://localhost:8083/"))
    assert (connect.readiness.initial_delay_seconds, connect.readiness.period_seconds) == (15, 10)
    assert connect.readiness.timeout_seconds == 10
    assert connect.liveness is not None
    assert connect.liveness.handler == HttpGet(path="/health", port=8080)
    # Kubernetes' probe defaults: period 10 s, timeout 1 s, failure 3, success 1.
    assert (connect.liveness.period_seconds, connect.liveness.timeout_seconds) == (10, 1)
    assert (connect.liveness.failure_threshold, connect.liveness.success_threshold) == (3, 1)
    assert connect.startup is None
    assert [(m.name, m.mount_path, m.read_only) for m in connect.volume_mounts] == [
        ("jmx", "/jmx", False),
        ("jmx-config", "/jmx-config", True),
    ]
    assert [v.source for v in spec.volumes] == [
        EmptyDir(),
        ConfigMapVolume(name="kafka-connect-jmx-exporter", optional=False),
    ]


def test_resources_pull_policy_and_default_mode_are_recorded_not_enforced() -> None:
    notes = parse(doc(DEPLOYMENT), "kafka-connect.yaml#1").notes
    recorded = {n.path for n in notes if n.status == "recorded"}
    assert recorded == {
        "spec.template.spec.containers[0].imagePullPolicy",
        "spec.template.spec.containers[0].resources",
        "spec.template.spec.volumes[1].configMap.defaultMode",
    }
    assert [n for n in notes if n.status == "unsupported"] == []


def test_an_unknown_field_is_reported_with_its_path_not_dropped() -> None:
    manifest = doc(DEPLOYMENT)
    spec = manifest["spec"]
    assert isinstance(spec, dict)
    spec["strategy"] = {"type": "Recreate"}
    template_spec = spec["template"]["spec"]
    template_spec["containers"][0]["securityContext"] = {"runAsUser": 70}
    template_spec["hostNetwork"] = True
    notes = parse(manifest, "x.yaml#1").notes
    unsupported = sorted(n.path for n in notes if n.status == "unsupported")
    assert unsupported == [
        "spec.strategy",
        "spec.template.spec.containers[0].securityContext",
        "spec.template.spec.hostNetwork",
    ]
    assert parse(manifest, "x.yaml#1").supported is False


def test_env_value_from_is_unsupported_until_a_manifest_needs_it() -> None:
    manifest = doc(DEPLOYMENT)
    env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]  # type: ignore[index]
    env.append({"name": "X", "valueFrom": {"configMapKeyRef": {"name": "c", "key": "k"}}})
    notes = parse(manifest, "x.yaml#1").notes
    assert [n.path for n in notes if n.status == "unsupported"] == [
        "spec.template.spec.containers[0].env[1].valueFrom"
    ]


def test_a_service_reads_ports_with_named_or_numbered_targets() -> None:
    loaded = parse(
        doc("""\
            apiVersion: v1
            kind: Service
            metadata:
              name: user-service
            spec:
              selector:
                app: user-service
              ports:
                - name: http
                  port: 8080
                  targetPort: 8080
                - name: metrics
                  port: 9090
                  targetPort: metrics
                - port: 5432
            """),
        "s.yaml#1",
    )
    service = loaded.obj
    assert isinstance(service, Service)
    assert service.meta.namespace is None
    assert dict(service.selector) == {"app": "user-service"}
    # targetPort defaults to port; protocol to TCP (Kubernetes' documented defaults).
    assert [(p.name, p.port, p.target_port, p.protocol) for p in service.ports] == [
        ("http", 8080, 8080, "TCP"),
        ("metrics", 9090, "metrics", "TCP"),
        (None, 5432, 5432, "TCP"),
    ]
    assert loaded.notes == ()


def test_configmap_job_cronjob_and_namespace() -> None:
    config_map = parse(
        doc("""\
            apiVersion: v1
            kind: ConfigMap
            metadata: {name: c, namespace: gluon-local}
            data:
              a.json: '{"x": 1}'
            """),
        "c.yaml#1",
    ).obj
    assert isinstance(config_map, ConfigMap)
    assert dict(config_map.data) == {"a.json": '{"x": 1}'}
    job = parse(
        doc("""\
            apiVersion: batch/v1
            kind: Job
            metadata: {name: register, labels: {app: register}}
            spec:
              template:
                metadata: {labels: {app: register}}
                spec:
                  restartPolicy: OnFailure
                  containers: [{name: r, image: curlimages/curl:8.10.1, command: [sh]}]
            """),
        "j.yaml#1",
    ).obj
    assert isinstance(job, Job)
    assert job.backoff_limit == 6  # the Job's documented default
    assert job.template.spec.restart_policy == "OnFailure"
    cron_job = parse(
        doc("""\
            apiVersion: batch/v1
            kind: CronJob
            metadata: {name: cleanup}
            spec:
              schedule: "*/15 * * * *"
              jobTemplate:
                spec:
                  template:
                    spec:
                      restartPolicy: OnFailure
                      containers: [{name: c, image: postgres:16-alpine, command: [psql]}]
            """),
        "cj.yaml#1",
    ).obj
    assert isinstance(cron_job, CronJob)
    assert cron_job.schedule == "*/15 * * * *"
    assert cron_job.job.backoff_limit == 6
    namespace = parse(doc("apiVersion: v1\nkind: Namespace\nmetadata: {name: gluon-local}\n"), "n")
    assert isinstance(namespace.obj, Namespace)


@pytest.mark.parametrize("kind", ["DaemonSet", "Role", "RoleBinding", "ServiceAccount", "Secret"])
def test_other_kinds_are_recognised_as_not_simulated(kind: str) -> None:
    loaded = parse({"apiVersion": "v1", "kind": kind, "metadata": {"name": "x"}, "spec": {}}, "o#1")
    assert loaded.obj == NotSimulated(kind=kind, name="x")
    assert loaded.notes == ()


def test_a_kind_not_simulated_needs_no_name() -> None:
    """A Kustomization (infra/k8s/observability) isn't an API object: no metadata."""
    loaded = parse({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization"}, "k")
    assert loaded.obj == NotSimulated(kind="Kustomization", name="-")


def test_another_namespace_is_unsupported() -> None:
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "c", "namespace": "x"},
    }
    notes = parse(manifest, "c#1").notes
    assert [(n.path, n.status) for n in notes] == [("metadata.namespace", "unsupported")]


def test_an_unexpected_api_version_is_unsupported() -> None:
    manifest: dict[str, object] = {
        "apiVersion": "extensions/v1beta1",
        "kind": "Deployment",
        "metadata": {"name": "d"},
    }
    manifest["spec"] = doc(DEPLOYMENT)["spec"]
    notes = parse(manifest, "d#1").notes
    assert ("apiVersion", "unsupported") in [(n.path, n.status) for n in notes]


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (["a", "list"], "x.yaml#2: not a mapping"),
        ({"apiVersion": "v1", "metadata": {"name": "c"}}, "x.yaml#2: no kind"),
        ({"apiVersion": "v1", "kind": "ConfigMap"}, "x.yaml#2: metadata.name is required"),
        (
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "s"},
                "spec": {"ports": "80"},
            },
            "x.yaml#2: spec.ports: expected a list, got str",
        ),
        (
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "s"},
                "spec": {"ports": [{"port": "eighty"}]},
            },
            "x.yaml#2: spec.ports[0].port: expected an integer, got str",
        ),
    ],
)
def test_malformed_input_is_refused_naming_its_source(manifest: object, message: str) -> None:
    with pytest.raises(ManifestError, match=re.escape(message)):
        parse(manifest, "x.yaml#2")


def test_load_reads_files_and_directories_of_multi_document_yaml(tmp_path: Path) -> None:
    (tmp_path / "b.yaml").write_text(
        "apiVersion: v1\nkind: Namespace\nmetadata: {name: gluon-local}\n---\n"
        "---\n# only a comment\n---\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n"
    )
    (tmp_path / "a.yml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n")
    (tmp_path / "notes.txt").write_text("not a manifest")
    single = tmp_path / "single.yaml"
    single.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: s}\n")
    loaded = load([tmp_path / "a.yml", tmp_path])
    assert [(item.obj.__class__.__name__, item.source) for item in loaded] == [
        ("ConfigMap", f"{tmp_path / 'a.yml'}#1"),
        ("ConfigMap", f"{tmp_path / 'a.yml'}#1"),
        ("Namespace", f"{tmp_path / 'b.yaml'}#1"),
        ("ConfigMap", f"{tmp_path / 'b.yaml'}#4"),
        ("ConfigMap", f"{single}#1"),
    ]


def test_load_refuses_invalid_yaml_naming_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: [unclosed\n")
    with pytest.raises(ManifestError, match=r"bad\.yaml: not valid YAML"):
        load([bad])


def test_load_refuses_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="no such file or directory"):
        load([tmp_path / "missing.yaml"])
