# SPDX-License-Identifier: MIT

"""`gluon-kube up [SERVICE ...]` (GK-6.4, GK-6.5): gluon bin/k8s-local-up's sequence
against the simulated cluster, step for step, with the same `== … ==` headings:

1. the namespace and shared local infra applied; the registration Job deleted first
   (a Job's template is immutable); `registry-reload --configmap-only` skipped, as
   Apicurio has no recipe;
2. every pod that can run Ready (up to 3 minutes; Redis and Apicurio are
   `ImageUnavailable` here, so they aren't waited for), then the registration Job
   complete (up to 2 minutes); either only warns, as the script's `|| WARN` does;
3. the retired inventory CronJob deleted and its topic removed;
4. per service: its image built (`gluon-kube build`: only the Rust ports can be, in the
   sandbox), the user schema reset on an implementation switch
   (`user-switch-reset.sql`), `helm upgrade --install` with its values file, `rollout
   restart` and `rollout status`, and on a switch the user outbox connector restarted.

`USER_SERVICE_IMPL` is honoured as the script does; its default here is `rust`, since the
Scala services can't be built in the sandbox (ADR 0028): a Scala service's chart is
installed and its pod shows `ImageUnavailable`, and its rollout isn't waited for.
"""

import os
import subprocess
import sys
import time
from collections.abc import Sequence
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


def runnable_pods_ready() -> list[str]:
    """Pods that can run (not ImageUnavailable, not a Job's) and aren't Ready yet."""
    pods = client.request("pods")["pods"]
    assert isinstance(pods, list)
    return [
        str(p["name"])
        for p in pods
        if p["status"] != "ImageUnavailable"
        and str(p["owner"]).startswith("Deployment/")
        and not p["is_ready"]
    ]


def wait_until(pending: object, timeout_s: float) -> bool:
    """Polls `pending()` (what isn't there yet) until empty; False on timeout."""
    assert callable(pending)
    deadline = time.monotonic() + timeout_s
    while pending():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def deployed_image(name: str) -> str:
    """The image a release's Deployment runs now ("" if none)."""
    answer = client.request("images", name=name)
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


def restart_connector(name: str, timeout_s: float = 60.0) -> bool:
    """Connect's restart, retried while it answers 409 (its workers rebalancing, as right
    after the registration Job). The script's single `curl -sf` would stop there, under
    `set -e`: a difference, and a finding for the laptop (gluon plan GK-6)."""
    url = f"http://localhost:8083/connectors/{name}/restart?includeTasks=true"
    deadline = time.monotonic() + timeout_s
    while gk("exec", "deploy/kafka-connect", "--", "curl", "-sf", "-X", "POST", url) != 0:
        if time.monotonic() > deadline:
            return False
        time.sleep(2)
    return True


def up(services: Sequence[str], gluon: Path) -> int:
    """The sequence; 0 when every service rolled out."""
    impl = os.environ.get("USER_SERVICE_IMPL", "rust")
    if impl not in ("scala", "rust"):
        print(f"FAIL: USER_SERVICE_IMPL must be scala or rust, got '{impl}'", file=sys.stderr)
        return 1
    infra = gluon / "infra" / "k8s" / "local-infra"
    step("Applying namespace + shared local infra")
    apply_files([infra / "namespace.yaml"])
    gk("delete", "job", "debezium-connector-register", "--ignore-not-found")
    print("registry-reload --configmap-only: skipped (apicurio-registry has no recipe)")
    apply_files([infra])

    step("Waiting for shared infra to be ready (up to 3m)")
    if not wait_until(runnable_pods_ready, INFRA_TIMEOUT_S):
        warn("not every pod reached Ready in time — check 'gluon-kube get pods'")
    step("Waiting for Debezium outbox connectors to register (up to 2m)")

    def job_pending() -> bool:
        return not client.request("job", name="debezium-connector-register")["succeeded"]

    try:
        registered = wait_until(job_pending, JOB_TIMEOUT_S)
    except ClusterError:
        registered = False  # no such Job: kubectl wait fails at once, and the script warns
    if not registered:
        warn(
            "connector registration didn't complete in time — check "
            "'gluon-kube logs job/debezium-connector-register'"
        )

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
    for svc in services or ALL_SERVICES:
        service_dir = gluon / "services" / svc
        values = gluon / "environments" / "local" / f"{svc}.values.yaml"
        image = svc
        if svc == "user-service" and impl == "rust":
            service_dir = gluon / "services" / "user-service-rs"
            values = gluon / "environments" / "local" / "user-service-rs.values.yaml"
            image = "user-service-rs"
        if not service_dir.is_dir():
            print(f"FAIL: {service_dir} doesn't exist", file=sys.stderr)
            return 1
        if not values.is_file():
            print(f"FAIL: {values} doesn't exist", file=sys.stderr)
            return 1
        rust = image.endswith("-rs")
        if rust:
            step(f"Building {svc} image from services/{image} (gluon-kube build)")
            code = build(service_dir)
            if code != 0:
                print(f"FAIL: building {image} failed ({code})", file=sys.stderr)
                return code
        else:
            step(
                f"Building {svc} image: skipped, the Scala services can't be built in the "
                "sandbox (ADR 0028); its pod will be ImageUnavailable"
            )

        switched = False
        current = deployed_image(svc)
        if svc == "user-service" and current and current.split(":")[0] != image:
            step(
                f"Switching user-service from {current.split(':')[0]} to {image}: resetting "
                "the user database"
            )
            reset = (infra / "user-switch-reset.sql").read_text()
            if (
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
                != 0
            ):
                return 1
            switched = True

        step(f"Deploying {svc} (helm release)")
        apply_rendered(svc, gluon / "infra" / "k8s" / "gluon", values)
        if not rust:
            continue
        step(f"Restarting {svc} to pick up the freshly built image")
        gk("rollout", "restart", f"deployment/{svc}")
        if gk("rollout", "status", f"deployment/{svc}", f"--timeout={ROLLOUT_TIMEOUT_S:.0f}s") != 0:
            status = 1
            continue
        if switched:
            step("Restarting the user outbox connector")
            if not restart_connector("user-outbox-connector"):
                warn("Connect refused the restart for a minute (409 while rebalancing)")
    step("Done. Check status with: gluon-kube get pods")
    return status
