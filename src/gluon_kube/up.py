# SPDX-License-Identifier: MIT

"""`gluon-kube up [SERVICE ...]` (GK-6.4, GK-6.5; all-Rust parity, allrust-parity_20261123):
gluon bin/k8s-local-up's sequence against the simulated cluster, step for step, with the
same `== … ==` headings:

0. every service's implementation chosen (`<SERVICE>_IMPL`, as the script) and its port
   and values file checked, before anything is built;
1. the namespace and shared local infra applied; the registration Job deleted first
   (a Job's template is immutable); `registry-reload --configmap-only` skipped, as
   Apicurio has no recipe;
2. every infra Deployment that can run with a Ready pod, one whose pod doesn't exist yet
   included (up to 3 minutes; Apicurio is `ImageUnavailable` here, so it isn't waited
   for), then the registration Job complete (up to 2 minutes); either only warns, as the
   script's `|| WARN` does;
3. the retired inventory CronJob deleted and its topic removed;
4. per service: its image built (`gluon-kube build`: only the Rust ports can be, in the
   sandbox), its schema reset on an implementation switch (gluon's `switch-reset.sql`, in
   `<short>-postgres`), `helm upgrade --install` with its values file, `rollout restart`
   and `rollout status`, and on a switch its outbox connector restarted;
5. the registration Job run again once every service has migrated, and every outbox
   connector RUNNING (FAILED tasks restarted; gluon bin/lib/connectors.sh), else up fails.

The default implementation here is `rust` for every service, where the script's is
`scala` unless `ALL_RUST=1`: the Scala services can't be built in the sandbox (ADR 0028).
`<SERVICE>_IMPL=scala` installs a Scala service's chart; its pod shows `ImageUnavailable`
and its rollout isn't waited for.
"""

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gluon_kube import client
from gluon_kube.build import build
from gluon_kube.chart import render_chart
from gluon_kube.client import ClusterError

ALL_SERVICES = (
    "catalog-service",
    "cart-service",
    "order-service",
    "inventory-service",
    "payment-service",
    "notification-service",
    "user-service",
    "fulfilment-service",
)
"""bin/k8s-local-up's ALL_SERVICES, in its order."""
INFRA_TIMEOUT_S = 180.0
JOB_TIMEOUT_S = 120.0
ROLLOUT_TIMEOUT_S = 120.0
CONNECTORS_TIMEOUT_S = 120.0
REGISTER_JOB = "debezium-connector-register"


class UpError(Exception):
    """A choice that stops `up` before anything is built."""


@dataclass(frozen=True, slots=True)
class Choice:
    """A service's chosen implementation: where it is and what its image is called."""

    service: str
    impl: str
    directory: Path
    values: Path
    image: str

    @property
    def short(self) -> str:
        """`order` for order-service: its database, user and `<short>-postgres`."""
        return self.service.removesuffix("-service")

    @property
    def rust(self) -> bool:
        return self.impl == "rust"


def impl_variable(service: str) -> str:
    """user-service -> USER_SERVICE_IMPL."""
    return f"{service.replace('-', '_')}_IMPL".upper()


def impl_of(service: str, env: Mapping[str, str]) -> str:
    """`<SERVICE>_IMPL`, else rust (the script's default is rust only under ALL_RUST=1)."""
    return env.get(impl_variable(service), "rust")


def choices(services: Sequence[str], env: Mapping[str, str], gluon: Path) -> list[Choice]:
    """Every service's choice, each port and values file there; else UpError for the first
    that isn't, before anything is built."""
    chosen = []
    for service in services or ALL_SERVICES:
        impl = impl_of(service, env)
        if impl not in ("scala", "rust"):
            raise UpError(f"{impl_variable(service)} must be scala or rust, got '{impl}'")
        image = f"{service}-rs" if impl == "rust" else service
        choice = Choice(
            service=service,
            impl=impl,
            directory=gluon / "services" / image,
            values=gluon / "environments" / "local" / f"{image}.values.yaml",
            image=image,
        )
        for needed in (choice.directory, choice.values):
            if not needed.exists():
                raise UpError(f"{service} ({impl}): {needed} doesn't exist")
        chosen.append(choice)
    return chosen


def infra_pending(
    objects: Sequence[Sequence[str]], pods: Sequence[Mapping[str, object]]
) -> list[str]:
    """Deployments without a Ready pod, one with no pod yet included; a Deployment whose
    image has no recipe (`ImageUnavailable`) is never Ready here, so it isn't waited for."""
    pending = []
    for kind, name in objects:
        if kind != "Deployment":
            continue
        own = [p for p in pods if p.get("owner") == f"Deployment/{name}"]
        if any(p.get("status") == "ImageUnavailable" for p in own):
            continue
        if not any(p.get("is_ready") for p in own):
            pending.append(name)
    return pending


def connector_names(register_yaml: str) -> list[str]:
    """The outbox connectors the registration Job registers (its ConfigMap's keys), as
    gluon bin/lib/connectors.sh reads them."""
    return re.findall(r"^  ([a-z0-9-]+)\.json: \|$", register_yaml, re.MULTILINE)


def connector_state(code: int, body: str) -> str:
    """The first state that isn't RUNNING (the connector's, then its tasks'), NO_TASKS for
    none, MISSING for a 404, UNREACHABLE otherwise: gluon bin/lib/connectors.sh's."""
    if code == 404:
        return "MISSING"
    if code != 200:
        return "UNREACHABLE"
    try:
        status = json.loads(body)
        states = [status["connector"]["state"], *(t["state"] for t in status["tasks"])]
    except (ValueError, KeyError, TypeError):
        return "UNREACHABLE"
    if not status["tasks"]:
        states.append("NO_TASKS")
    return next((str(state) for state in states if state != "RUNNING"), "RUNNING")


def deployed_connectors(
    names: Sequence[str], objects: Sequence[Sequence[str]]
) -> tuple[list[str], list[str]]:
    """The connectors to wait for, and those not: `<short>-outbox-connector` only when
    Deployment `<short>-service` exists (a service not deployed has no outbox table, so its
    connector can't run); any other connector is waited for."""
    deployed = {name for kind, name in objects if kind == "Deployment"}
    watched: list[str] = []
    skipped: list[str] = []
    for name in names:
        short = name.removesuffix("-outbox-connector")
        waits = short == name or f"{short}-service" in deployed
        (watched if waits else skipped).append(name)
    return watched, skipped


def ensure_connectors(
    names: Sequence[str],
    state_of: Callable[[str], str],
    restart: Callable[[str], object],
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Polls until every connector is RUNNING, restarting FAILED ones; `name:STATE` of those
    that aren't by the deadline (none: done)."""
    deadline = clock() + timeout_s
    while True:
        pending = []
        for name in names:
            state = state_of(name)
            if state == "FAILED":
                restart(name)
            if state != "RUNNING":
                pending.append(f"{name}:{state}")
        if not pending or clock() >= deadline:
            return pending
        sleep(5)


def step(title: str) -> None:
    """A heading, as the script prints them."""
    print(f"== {title} ==", flush=True)


def warn(message: str) -> None:
    print(f"WARN: {message}", file=sys.stderr, flush=True)


def apply_files(paths: Sequence[Path]) -> None:
    """`kubectl apply -f` for files or a directory, printing kubectl's lines."""
    from gluon_kube.cli import manifest_files

    documents = [{"source": str(p), "text": p.read_text()} for p in manifest_files(paths)]
    lines = client.request("apply", documents=documents)["lines"]
    assert isinstance(lines, list)
    for line in lines:
        print(line, flush=True)


def apply_rendered(release: str, chart: Path, values: Path) -> None:
    """`helm upgrade --install`: the chart rendered (GK-3) and applied under the release."""
    from gluon_kube.chart import as_helm_output

    text = as_helm_output(render_chart(release, chart, [values], "gluon-local"))
    answer = client.request("helm", release=release, documents=[{"source": release, "text": text}])
    print(answer["message"], flush=True)


def infra_not_ready() -> list[str]:
    """infra_pending for the cluster as it is now."""
    objects = client.request("objects")["objects"]
    pods = client.request("pods")["pods"]
    assert isinstance(objects, list)
    assert isinstance(pods, list)
    return infra_pending(objects, pods)


def wait_until(pending: Callable[[], object], timeout_s: float) -> bool:
    """Polls `pending()` (what isn't there yet) until empty; False on timeout."""
    deadline = time.monotonic() + timeout_s
    while pending():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def deployed_image(name: str) -> str:
    """The image a release's Deployment runs now ("" if none)."""
    try:
        answer = client.request("images", name=name)
    except ClusterError:
        return ""
    return str(answer.get("image") or "")


def gk(*argv: str, stdin: str | None = None) -> int:
    """The gluon-kube command itself (exec, delete, rollout), as the script calls kubectl."""
    from gluon_kube.cli import main

    if stdin is None:
        return main(list(argv))
    done = subprocess.run(
        [sys.executable, "-m", "gluon_kube", *argv], input=stdin, text=True, check=False
    )
    return done.returncode


def connect_api(method: str, path: str) -> tuple[int, str]:
    """Kafka Connect's REST API from its own pod, as connectors.sh's `connect_api`: the
    HTTP code and the body (0 when it couldn't be asked)."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "gluon_kube",
            "exec",
            "deploy/kafka-connect",
            "--",
            "curl",
            "-s",
            "-X",
            method,
            "-w",
            "\n%{http_code}",
            f"http://localhost:8083{path}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    body, _, code = done.stdout.rpartition("\n")
    return (int(code) if code.strip().isdigit() else 0), body


def connector_status(name: str) -> str:
    return connector_state(*connect_api("GET", f"/connectors/{name}/status"))


def restart_connector(name: str, only_failed: bool = True, timeout_s: float = 30.0) -> bool:
    """Connect's restart, retried while it answers 409 (its workers rebalancing, as right
    after the registration Job), as connectors.sh's `connector_restart`."""
    query = f"includeTasks=true&onlyFailed={'true' if only_failed else 'false'}"
    deadline = time.monotonic() + timeout_s
    while True:
        code, _ = connect_api("POST", f"/connectors/{name}/restart?{query}")
        if 200 <= code < 300:
            return True
        if code != 409 or time.monotonic() > deadline:
            warn(f"restarting {name}: Connect answered {code}")
            return False
        time.sleep(3)


def wait_for_register_job() -> None:
    """The registration Job complete (up to 2 minutes); a warning, as the script's."""

    def job_pending() -> bool:
        return not client.request("job", name=REGISTER_JOB)["succeeded"]

    try:
        registered = wait_until(job_pending, JOB_TIMEOUT_S)
    except ClusterError:
        registered = False  # no such Job: kubectl wait fails at once, and the script warns
    if not registered:
        warn(
            "connector registration didn't complete in time — check "
            f"'gluon-kube logs job/{REGISTER_JOB}'"
        )


def switch_reset(choice: Choice, sql: str) -> int:
    """The service's database reset for a switch, in its own Postgres if it has one."""
    if deployed_image(f"{choice.short}-postgres") == "":
        return 0
    step(f"Switching {choice.service} to {choice.image}: resetting the {choice.short} database")
    return gk(
        "exec",
        "-i",
        f"deploy/{choice.short}-postgres",
        "--",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-q",
        "-U",
        choice.short,
        "-d",
        choice.short,
        "-f",
        "-",
        stdin=sql,
    )


def up(services: Sequence[str], gluon: Path) -> int:
    """The sequence; 0 when every service rolled out and every connector is RUNNING."""
    try:
        chosen = choices(services, os.environ, gluon)
    except UpError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    infra = gluon / "infra" / "k8s" / "local-infra"
    register = infra / "debezium-connectors-register.yaml"
    names = connector_names(register.read_text()) if register.is_file() else []
    step("Applying namespace + shared local infra")
    apply_files([infra / "namespace.yaml"])
    gk("delete", "job", REGISTER_JOB, "--ignore-not-found")
    print("registry-reload --configmap-only: skipped (apicurio-registry has no recipe)")
    apply_files([infra])

    step("Waiting for shared infra to be ready (up to 3m)")
    if not wait_until(infra_not_ready, INFRA_TIMEOUT_S):
        warn(f"not ready in time: {', '.join(infra_not_ready())} — check 'gluon-kube get pods'")
    step("Waiting for Debezium outbox connectors to register (up to 2m)")
    wait_for_register_job()

    step("Removing retired inventory-events plumbing")
    gk("delete", "cronjob", "inventory-outbox-cleanup", "--ignore-not-found")
    gk(
        "exec",
        "deploy/kafka",
        "--",
        "/opt/kafka/bin/kafka-topics.sh",
        "--bootstrap-server",
        "localhost:9092",
        "--delete",
        "--topic",
        "inventory-events",
        "--if-exists",
    )

    status = 0
    reset_sql = (infra / "switch-reset.sql").read_text()
    for choice in chosen:
        if choice.rust:
            step(f"Building {choice.service} image from services/{choice.image} (gluon-kube build)")
            code = build(choice.directory)
            if code != 0:
                print(f"FAIL: building {choice.image} failed ({code})", file=sys.stderr)
                return code
        else:
            step(
                f"Building {choice.service} image: skipped, the Scala services can't be built "
                "in the sandbox (ADR 0028); its pod will be ImageUnavailable"
            )

        current = deployed_image(choice.service)
        switched = bool(current) and current.split(":")[0] != choice.image
        if switched and switch_reset(choice, reset_sql) != 0:
            return 1

        step(f"Deploying {choice.service} (helm release)")
        apply_rendered(choice.service, gluon / "infra" / "k8s" / "gluon", choice.values)
        if not choice.rust:
            continue
        step(f"Restarting {choice.service} to pick up the freshly built image")
        gk("rollout", "restart", f"deployment/{choice.service}")
        timeout = f"--timeout={ROLLOUT_TIMEOUT_S:.0f}s"
        if gk("rollout", "status", f"deployment/{choice.service}", timeout) != 0:
            status = 1
            continue
        connector = f"{choice.short}-outbox-connector"
        if switched and connector in names:
            step(f"Restarting the {choice.short} outbox connector")
            restart_connector(connector, only_failed=False)

    step("Registering Debezium outbox connectors after the migrations (up to 2m)")
    if register.is_file():
        gk("delete", "job", REGISTER_JOB, "--ignore-not-found")
        apply_files([register])
        wait_for_register_job()
    else:
        warn(f"no {register}: no connectors to register")

    step("Waiting for every deployed service's outbox connector to be RUNNING (up to 2m)")
    objects = client.request("objects")["objects"]
    assert isinstance(objects, list)
    watched, skipped = deployed_connectors(names, objects)
    if skipped:
        print(f"not waited for (service not deployed): {' '.join(skipped)}")
    pending = ensure_connectors(watched, connector_status, restart_connector, CONNECTORS_TIMEOUT_S)
    if pending:
        print(f"FAIL: connectors not RUNNING: {' '.join(pending)}", file=sys.stderr)
        print("see 'gluon-kube logs deploy/kafka-connect'", file=sys.stderr)
        return 1
    step("Done. Check status with: gluon-kube get pods")
    return status
