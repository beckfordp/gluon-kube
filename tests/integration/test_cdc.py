# SPDX-License-Identifier: MIT

"""gluon-local's CDC path from its manifests (GK-5): `apply -f infra/k8s/local-infra/`
runs every Postgres, Kafka, Kafka Connect (its init container fetching the JMX exporter
from Maven Central through the node's egress) and the connector registration Job; an
outbox row reaches its topic through the real Debezium connector; a CronJob runs by hand.
Expectations from the manifests, bin/k8s-local-up and Debezium's documented behaviour."""

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

GLUON = Path(__file__).resolve().parents[4]
LOCAL_INFRA = GLUON / "infra" / "k8s" / "local-infra"
MIGRATION = (
    GLUON
    / "services"
    / "user-service-rs"
    / "crates"
    / "user-service"
    / "migrations"
    / "20261109000000_baseline.sql"
)
GK = Path(sys.executable).parent / "gluon-kube"
CDC = Path(
    os.environ.get(
        "GLUON_KUBE_CDC_HOME", Path.home() / ".local" / "cdc-kafka-3.8.0_debezium-3.0.0.Final"
    )
)

pytestmark = [
    pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bwrap"),
    pytest.mark.skipif(not (CDC / "COMPLETE").exists(), reason="needs bin/sandbox-setup's cdc"),
    pytest.mark.skipif(not MIGRATION.exists(), reason="needs gluon with user-service-rs"),
]


def gk(*args: str, stdin: str | None = None, timeout: float = 240, check: bool = True) -> str:
    """The gluon-kube command's stdout."""
    done = subprocess.run(
        [str(GK), *args], input=stdin, capture_output=True, text=True, timeout=timeout
    )
    if check and done.returncode != 0:
        raise AssertionError(
            f"gluon-kube {' '.join(args)}: {done.returncode}\n{done.stdout}{done.stderr}"
        )
    return done.stdout


def connector(name: str) -> dict[str, object]:
    """A connector's status from Connect's REST API, from a pod."""
    status = json.loads(
        gk("run", "--", "curl", "-s", f"http://kafka-connect:8083/connectors/{name}/status")
    )
    assert isinstance(status, dict)
    return status


def consume(topic: str, wanted: str, timeout: float = 180) -> list[str]:
    """`topic`'s records (key, tab, value) through the console consumer in the Kafka pod,
    partition 0 from its start (no consumer group to join: quick under load), retried
    until one starts with `wanted` (the topic may not exist yet). On timeout the records
    seen, and the connector's status to say why."""
    deadline = time.monotonic() + timeout
    while True:
        out = gk(
            "exec",
            "deploy/kafka",
            "--",
            "/opt/kafka/bin/kafka-console-consumer.sh",
            "--bootstrap-server",
            "localhost:9092",
            "--topic",
            topic,
            "--partition",
            "0",
            "--offset",
            "earliest",
            "--timeout-ms",
            "20000",
            "--property",
            "print.key=true",
            check=False,
        )
        records = [line for line in out.splitlines() if "\t" in line]
        if any(line.startswith(wanted) for line in records):
            return records
        if time.monotonic() > deadline:
            return [
                *records,
                f"(none for {wanted!r}; connector: {connector('user-outbox-connector')})",
            ]


RESTART_EVERY_S = 20.0


def request_restart(name: str) -> bool:
    """One restart request for the connector and its tasks; False if Connect refused it
    (it answers 409 while its workers rebalance)."""
    url = f"http://localhost:8083/connectors/{name}/restart?includeTasks=true"
    done = subprocess.run(
        [str(GK), "exec", "deploy/kafka-connect", "--", "curl", "-sf", "-X", "POST", url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode == 0


def restart_until_running(name: str, timeout: float = 180) -> None:
    """Restarts the connector until its task is RUNNING, re-sending the restart every
    RESTART_EVERY_S, as one would by hand. Connect accepts a restart at once but carries it
    out only when its worker is in good standing with its group: on a starved machine its
    heartbeats time out and it rebalances, and a single restart can wait past any one
    deadline (2026-11-18: "session timed out without receiving a heartbeat response", the
    report's two CDC failures). On timeout, why: the task's state, whether outbox_events
    exists, and Connect's last log lines."""
    deadline = time.monotonic() + timeout
    last = float("-inf")
    sent = 0
    while True:
        now = time.monotonic()
        if now - last >= RESTART_EVERY_S:
            sent += request_restart(name)
            last = now
            time.sleep(3)  # let Connect take it before reading the state again
        state = task_state(name)
        if state[0] == "RUNNING":
            return
        if time.monotonic() > deadline:
            tables = gk(
                "exec",
                "deploy/user-postgres",
                "--",
                "psql",
                "-U",
                "user",
                "-d",
                "user",
                "-tAc",
                "select tablename from pg_tables where schemaname = 'public'",
                check=False,
            )
            log = gk("logs", "deploy/kafka-connect", "--tail", "30", check=False)
            raise AssertionError(
                f"{name}: {state[0]} after {sent} restarts accepted\ntables: {tables!r}\n"
                f"{state[1][:300]}\n{log}"
            )
        time.sleep(1)


def task_state(name: str) -> tuple[str, str]:
    tasks = connector(name).get("tasks")
    assert isinstance(tasks, list)
    if not tasks:
        return "NONE", ""
    return str(tasks[0]["state"]), str(tasks[0].get("trace", ""))


@pytest.fixture(scope="module")
def infra(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """local-infra applied in a cluster of its own; the registration Job complete."""
    state = tmp_path_factory.mktemp("gk-cdc") / "state"
    previous = os.environ.get("GLUON_KUBE_STATE")
    os.environ["GLUON_KUBE_STATE"] = str(state)
    try:
        gk("cluster", "start")
        gk("apply", "-f", str(LOCAL_INFRA))
        gk("wait", "--for=condition=complete", "job/debezium-connector-register", "--timeout=240s")
        yield
    finally:
        gk("cluster", "stop", check=False)
        if previous is None:
            os.environ.pop("GLUON_KUBE_STATE", None)
        else:
            os.environ["GLUON_KUBE_STATE"] = previous


def test_local_infra_runs_what_has_a_recipe(infra: None) -> None:
    rows = [line.split() for line in gk("get", "pods").splitlines()[1:]]
    status = {row[0].rsplit("-", 2)[0]: row[2] for row in rows}
    for postgres in (
        "catalog",
        "fulfilment",
        "inventory",
        "notification",
        "order",
        "payment",
        "user",
    ):
        assert status[f"{postgres}-postgres"] == "Running"
    assert status["kafka"] == status["kafka-connect"] == "Running"
    for unavailable in (
        "cart-redis",
        "catalog-redis",
        "order-redis",
        "apicurio-registry",
        "apicurio-registry-ui",
    ):
        assert status[unavailable] == "ImageUnavailable"
    assert any(
        row[0].startswith("debezium-connector-register-") and row[2] == "Completed" for row in rows
    )
    logs = gk("logs", "job/debezium-connector-register")
    assert "Registering user-outbox-connector..." in logs
    assert "Removing retired inventory-outbox-connector..." in logs
    assert "/jmx/download.jar: OK" in gk("logs", "deploy/kafka-connect", "-c", "fetch-jmx-exporter")


def test_before_its_table_exists_the_connectors_task_fails(infra: None) -> None:
    """Debezium can't create a filtered publication for a table that doesn't exist yet:
    the task FAILS until the service has migrated and the connector restarts."""
    deadline = time.monotonic() + 60
    while task_state("user-outbox-connector")[0] != "FAILED":
        assert time.monotonic() < deadline, task_state("user-outbox-connector")
        time.sleep(1)
    assert (
        "No table filters found for filtered publication" in task_state("user-outbox-connector")[1]
    )


def test_an_outbox_row_reaches_its_topic_through_debezium(infra: None) -> None:
    ddl = MIGRATION.read_text().split("CREATE TABLE outbox_events", 1)[1].split(");", 1)[0]
    gk(
        "exec",
        "-i",
        "deploy/user-postgres",
        "--",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-q",
        "-U",
        "user",
        "-d",
        "user",
        "-f",
        "-",
        stdin=f"CREATE TABLE outbox_events{ddl});\n",
    )
    restart_until_running("user-outbox-connector")
    gk(
        "exec",
        "deploy/user-postgres",
        "--",
        "psql",
        "-q",
        "-U",
        "user",
        "-d",
        "user",
        "-c",
        "insert into outbox_events (id, aggregate_id, payload, correlation_id) values "
        "(gen_random_uuid(), 'user-1', '{\"type\": \"UserRegistered\"}', 'c-1')",
    )
    records = consume("user-events", "user-1\t")
    assert 'user-1\t{"type": "UserRegistered"}' in records


def test_a_cron_job_runs_by_hand(infra: None) -> None:
    assert gk("create", "job", "cleanup-now", "--from=cronjob/user-outbox-cleanup") == (
        "job.batch/cleanup-now created\n"
    )
    gk("wait", "--for=condition=complete", "job/cleanup-now", "--timeout=60s")
    assert "DELETE" in gk("logs", "job/cleanup-now")


RELEASE_BINARY = GLUON / "services" / "user-service-rs" / "target" / "release" / "user-service"
ASYNCAPI_HTML = GLUON / "services" / "user-service-rs" / "target" / "asyncapi-docs"


@pytest.mark.skipif(
    not (RELEASE_BINARY.exists() and ASYNCAPI_HTML.exists()),
    reason="needs user-service-rs built (gluon-kube build user-service-rs)",
)
def test_user_service_rs_from_its_chart_publishes_a_signup(infra: None) -> None:
    """GK-6: the chart with user-service-rs.values.yaml under the release `user-service`
    (as bin/k8s-local-up installs it); a sign-up through the Service gives UserRegistered
    on user-events through the real connector (after its restart: see the finding)."""
    chart = GLUON / "infra" / "k8s" / "gluon"
    values = GLUON / "environments" / "local" / "user-service-rs.values.yaml"
    # The tests above made outbox_events by hand: start from a clean schema, as
    # bin/k8s-local-up does when the implementation changes.
    reset = (LOCAL_INFRA / "user-switch-reset.sql").read_text()
    gk(
        "exec",
        "-i",
        "deploy/user-postgres",
        "--",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-q",
        "-U",
        "user",
        "-d",
        "user",
        "-f",
        "-",
        stdin=reset,
    )
    out = gk(
        "helm",
        "upgrade",
        "--install",
        "user-service",
        str(chart),
        "-f",
        str(values),
        "--namespace",
        "gluon-local",
    )
    assert 'Release "user-service" does not exist. Installing it now.' in out
    gk("rollout", "status", "deployment/user-service", "--timeout=120s")
    assert gk("exec", "deploy/user-service", "--", "id", "-u") == "65532\n"
    restart_until_running("user-outbox-connector")
    body = '{"email": "gk6@example.com", "password": "correct-horse-battery-1"}'
    created = json.loads(
        gk(
            "run",
            "--",
            "curl",
            "-sf",
            "-X",
            "POST",
            "http://user-service:8080/auth/signup",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
        )
    )
    user_id = created["userId"]
    records = consume("user-events", user_id + "\t")
    mine = [line for line in records if line.startswith(user_id + "\t")]
    assert len(mine) == 1, records
    assert json.loads(mine[0].split("\t", 1)[1])["eventType"] == "UserRegistered"
    page = gk("run", "--", "curl", "-sf", "http://user-service:8080/docs/asyncapi/")
    assert "<title>User Service" in page  # the rendered AsyncAPI HTML, at ASYNCAPI_HTML_DIR
    assert 'src="js/app.js"' in page
