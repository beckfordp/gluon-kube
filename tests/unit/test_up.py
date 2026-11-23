# SPDX-License-Identifier: MIT

"""`gluon-kube up`'s decisions (allrust-parity_20261123; gluon all-Rust Stage B, B4), as
gluon bin/k8s-local-up makes them since Stage A: the implementation per service, every
choice checked before a build; which infra is still to be waited for; and the outbox
connectors, registered after the services and brought to RUNNING."""

from pathlib import Path

import pytest

from gluon_kube.up import (
    ALL_SERVICES,
    Choice,
    UpError,
    choices,
    connector_names,
    connector_state,
    deployed_connectors,
    ensure_connectors,
    impl_of,
    infra_pending,
)


def gluon_tree(tmp_path: Path, ports: tuple[str, ...] = ALL_SERVICES) -> Path:
    """A gluon checkout with every Scala service and the named services' Rust ports, each
    with its values file."""
    gluon = tmp_path / "gluon"
    (gluon / "environments" / "local").mkdir(parents=True)
    for service in ALL_SERVICES:
        (gluon / "services" / service).mkdir(parents=True)
        (gluon / "environments" / "local" / f"{service}.values.yaml").write_text("{}\n")
    for service in ports:
        (gluon / "services" / f"{service}-rs").mkdir(parents=True)
        (gluon / "environments" / "local" / f"{service}-rs.values.yaml").write_text("{}\n")
    return gluon


def test_rust_is_every_services_default_here() -> None:
    assert {impl_of(s, {}) for s in ALL_SERVICES} == {"rust"}


def test_a_services_own_variable_wins() -> None:
    env = {"CART_SERVICE_IMPL": "scala", "ALL_RUST": "1"}
    assert impl_of("cart-service", env) == "scala"
    assert impl_of("order-service", env) == "rust"


def test_every_service_is_chosen_with_its_port_and_values(tmp_path: Path) -> None:
    gluon = gluon_tree(tmp_path)
    chosen = choices([], {"ALL_RUST": "1"}, gluon)
    assert [c.service for c in chosen] == list(ALL_SERVICES)
    order = next(c for c in chosen if c.service == "order-service")
    assert order == Choice(
        service="order-service",
        impl="rust",
        directory=gluon / "services" / "order-service-rs",
        values=gluon / "environments" / "local" / "order-service-rs.values.yaml",
        image="order-service-rs",
    )
    assert order.short == "order"


def test_a_scala_choice_is_the_scala_service(tmp_path: Path) -> None:
    gluon = gluon_tree(tmp_path)
    [cart] = choices(["cart-service"], {"CART_SERVICE_IMPL": "scala"}, gluon)
    assert (cart.impl, cart.image, cart.directory.name) == ("scala", "cart-service", "cart-service")


def test_every_port_is_checked_before_anything_is_built(tmp_path: Path) -> None:
    gluon = gluon_tree(tmp_path, ports=("user-service",))
    with pytest.raises(UpError, match=r"cart-service \(rust\): .*services/cart-service-rs"):
        choices(["user-service", "cart-service"], {}, gluon)


def test_a_missing_values_file_is_named(tmp_path: Path) -> None:
    gluon = gluon_tree(tmp_path)
    (gluon / "environments" / "local" / "notification-service-rs.values.yaml").unlink()
    with pytest.raises(UpError, match=r"notification-service-rs\.values\.yaml doesn't exist"):
        choices([], {}, gluon)


def test_an_unknown_implementation_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(UpError, match=r"CART_SERVICE_IMPL must be scala or rust, got 'java'$"):
        choices(["cart-service"], {"CART_SERVICE_IMPL": "java"}, gluon_tree(tmp_path))


def pod(name: str, owner: str, *, ready: bool, status: str = "Running") -> dict[str, object]:
    return {"name": name, "owner": f"Deployment/{owner}", "is_ready": ready, "status": status}


def test_infra_without_a_pod_yet_is_waited_for() -> None:
    objects = [["Deployment", "kafka"], ["Deployment", "cart-redis"], ["Job", "register"]]
    pods = [pod("kafka-1", "kafka", ready=True)]
    assert infra_pending(objects, pods) == ["cart-redis"]


def test_infra_not_ready_is_waited_for_and_ready_infra_isnt() -> None:
    objects = [["Deployment", "kafka"], ["Deployment", "user-postgres"]]
    pods = [pod("kafka-1", "kafka", ready=False), pod("pg-1", "user-postgres", ready=True)]
    assert infra_pending(objects, pods) == ["kafka"]


def test_an_image_with_no_recipe_isnt_waited_for() -> None:
    objects = [["Deployment", "apicurio-registry"]]
    pods = [pod("a-1", "apicurio-registry", ready=False, status="ImageUnavailable")]
    assert infra_pending(objects, pods) == []


REGISTER = """apiVersion: v1
kind: ConfigMap
data:
  order-outbox-connector.json: |
    {"name": "order-outbox-connector"}
  user-outbox-connector.json: |
    {"name": "user-outbox-connector"}
"""


def test_connector_names_are_the_register_jobs() -> None:
    assert connector_names(REGISTER) == ["order-outbox-connector", "user-outbox-connector"]


@pytest.mark.parametrize(
    ("code", "body", "state"),
    [
        (200, '{"connector":{"state":"RUNNING"},"tasks":[{"state":"RUNNING"}]}', "RUNNING"),
        (200, '{"connector":{"state":"RUNNING"},"tasks":[{"state":"FAILED"}]}', "FAILED"),
        (200, '{"connector":{"state":"RUNNING"},"tasks":[]}', "NO_TASKS"),
        (404, "", "MISSING"),
        (500, "", "UNREACHABLE"),
        (200, "not json", "UNREACHABLE"),
    ],
)
def test_a_connectors_state_is_its_first_not_running(code: int, body: str, state: str) -> None:
    assert connector_state(code, body) == state


def test_failed_connectors_are_restarted_until_running() -> None:
    states = {"a": ["FAILED", "RUNNING"], "b": ["UNREACHABLE", "RUNNING"]}
    restarted: list[str] = []
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    pending = ensure_connectors(
        ["a", "b"],
        state_of=lambda name: states[name].pop(0),
        restart=restarted.append,
        timeout_s=60,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert pending == []
    assert restarted == ["a"]


def test_connectors_not_running_by_the_deadline_are_returned() -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    pending = ensure_connectors(
        ["a", "b"],
        state_of=lambda name: "RUNNING" if name == "a" else "MISSING",
        restart=lambda name: None,
        timeout_s=20,
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert pending == ["b:MISSING"]


def test_only_deployed_services_connectors_are_waited_for() -> None:
    """A service not deployed has no outbox table: its connector can't run (B7)."""
    objects = [["Deployment", "cart-service"], ["Deployment", "order-service"], ["Job", "x"]]
    names = ["order-outbox-connector", "user-outbox-connector", "audit-connector"]
    assert deployed_connectors(names, objects) == (
        ["order-outbox-connector", "audit-connector"],
        ["user-outbox-connector"],
    )
