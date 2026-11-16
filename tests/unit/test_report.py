# SPDX-License-Identifier: MIT

"""What `apply --dry-run` says about each object (GK-3.7): what would run, what is
recorded only, what isn't simulated, and what is unsupported."""

from gluon_kube.manifests import Loaded, Note
from gluon_kube.model import (
    ConfigMap,
    Container,
    CronJob,
    Deployment,
    Job,
    Namespace,
    NotSimulated,
    ObjectMeta,
    PodSpec,
    PodTemplate,
    Service,
    ServicePort,
)
from gluon_kube.report import describe, summary

POD = PodTemplate({"app": "x"}, PodSpec(containers=(Container("postgres", "postgres:16-alpine"),)))


def test_a_deployment_says_what_would_run_and_its_recorded_fields() -> None:
    loaded = Loaded(
        Deployment(ObjectMeta("user-postgres"), {"app": "x"}, POD),
        "p.yaml#1",
        (Note("spec.template.spec.containers[0].resources", "recorded", "not enforced"),),
    )
    assert describe(loaded) == [
        "Deployment/user-postgres  runs 1 pod: postgres (postgres:16-alpine)",
        "  recorded: spec.template.spec.containers[0].resources (not enforced)",
    ]


def test_an_unsupported_field_says_the_object_wont_run() -> None:
    loaded = Loaded(
        Deployment(ObjectMeta("d"), {"app": "x"}, POD, replicas=2),
        "d.yaml#1",
        (Note("spec.strategy", "unsupported", "not simulated by gluon-kube"),),
    )
    assert describe(loaded) == [
        "Deployment/d  UNSUPPORTED, won't run (would run 2 pods: postgres (postgres:16-alpine))",
        "  unsupported: spec.strategy (not simulated by gluon-kube)",
    ]


def test_each_kind_is_described() -> None:
    service = Service(
        ObjectMeta("s"), {"app": "x"}, (ServicePort(8080, 8080), ServicePort(9090, "metrics"))
    )
    assert describe(Loaded(service, "s")) == ["Service/s  ClusterIP: 8080->8080, 9090->metrics"]
    config_map = ConfigMap(ObjectMeta("c"), {"a": "1", "b": "2"})
    assert describe(Loaded(config_map, "c")) == ["ConfigMap/c  2 keys"]
    job = Job(ObjectMeta("j"), POD, backoff_limit=6)
    assert describe(Loaded(job, "j")) == [
        "Job/j  runs to completion: postgres (postgres:16-alpine), backoffLimit 6"
    ]
    cron_job = CronJob(ObjectMeta("cj"), "*/15 * * * *", job)
    assert describe(Loaded(cron_job, "cj")) == [
        "CronJob/cj  schedule '*/15 * * * *' recorded, not run; its job runs by hand"
    ]
    assert describe(Loaded(Namespace(ObjectMeta("gluon-local")), "n")) == [
        "Namespace/gluon-local  simulated"
    ]
    assert describe(Loaded(NotSimulated("DaemonSet", "filebeat"), "f")) == [
        "DaemonSet/filebeat  not simulated"
    ]


def test_the_summary_counts_kinds_and_names_unsupported_objects_only_when_there_are_any() -> None:
    good = Loaded(Namespace(ObjectMeta("gluon-local")), "n")
    recorded = Loaded(ConfigMap(ObjectMeta("c"), {}), "c", (Note("x", "recorded", "r"),))
    other = Loaded(NotSimulated("DaemonSet", "f"), "f")
    assert summary([good, recorded, other]) == (
        "3 objects (1 ConfigMap, 1 DaemonSet, 1 Namespace): 1 field recorded only, "
        "1 object not simulated; everything else as written"
    )
    bad = Loaded(ConfigMap(ObjectMeta("b"), {}), "b", (Note("y", "unsupported", "u"),))
    assert summary([good, bad]) == (
        "2 objects (1 ConfigMap, 1 Namespace): 1 UNSUPPORTED (ConfigMap/b)"
    )
