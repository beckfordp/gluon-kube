# SPDX-License-Identifier: MIT

"""The `gluon-kube` command. Its verbs follow kubectl's and helm's (gluon ADR 0030
decision 5): `template` (GK-3.6); `apply` (`--dry-run`: GK-3.7); `cluster start|stop|
status`, `get`, `logs`, `exec`, `run`, `port-forward`, `delete`, `wait` (GK-4);
`create job --from` (GK-5.4); `rollout`, `helm upgrade --install`, `build` and `up`
(GK-6); `proxy`, the cluster's names outside it. `-n/--namespace gluon-local` is accepted
everywhere kubectl takes it.

Exit codes: 0 done; 1 something unsupported, refused or not as waited for; 2 a usage
error or input gluon-kube can't read (the message names the file and document).
"""

import argparse
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from gluon_kube import __version__, client
from gluon_kube.build import build
from gluon_kube.chart import ChartError, as_helm_output, render_chart
from gluon_kube.client import ClusterError
from gluon_kube.images import gluon_root
from gluon_kube.manifests import MANIFEST_SUFFIXES, NAMESPACE, ManifestError, load
from gluon_kube.report import describe, summary
from gluon_kube.template import TemplateError
from gluon_kube.up import up
from gluon_kube.values import ValuesError

DESCRIPTION = (
    "A simulated gluon-local for the sandbox (gluon ADR 0030): runs gluon-local's "
    "unchanged manifests, chart and values files as local processes in unprivileged "
    "namespaces."
)
KINDS = {
    "pod": "Pod",
    "pods": "Pod",
    "po": "Pod",
    "deploy": "Deployment",
    "deployment": "Deployment",
    "deployments": "Deployment",
    "svc": "Service",
    "service": "Service",
    "services": "Service",
    "job": "Job",
    "jobs": "Job",
    "cronjob": "CronJob",
    "cronjobs": "CronJob",
    "cm": "ConfigMap",
    "configmap": "ConfigMap",
    "configmaps": "ConfigMap",
    "ns": "Namespace",
    "namespace": "Namespace",
}


def kind_of(word: str) -> str:
    """kubectl's resource names and abbreviations, to a kind."""
    kind = KINDS.get(word.lower().split(".")[0])
    if kind is None:
        raise ClusterError(f'the server doesn\'t have a resource type "{word}"')
    return kind


API_GROUPS = {"Deployment": "apps", "Job": "batch", "CronJob": "batch"}


def resource_name(kind: str, name: str) -> str:
    """kubectl's `-o name`: `deployment.apps/x`, `service/x`."""
    group = API_GROUPS.get(kind)
    return f"{kind.lower()}{'.' + group if group else ''}/{name}"


def split_ref(ref: str, kind: str | None = None) -> tuple[str, str]:
    """`deploy/x` or (`deploy`, `x`): kind and name."""
    if "/" in ref:
        word, name = ref.split("/", 1)
        return kind_of(word), name
    if kind is None:
        raise ClusterError(f"expected <kind>/<name>, got {ref}")
    return kind_of(kind), ref


def add_namespace(parser: argparse.ArgumentParser) -> None:
    """`-n/--namespace`: only gluon-local is simulated."""
    parser.add_argument("-n", "--namespace", default=NAMESPACE, help="only gluon-local")


def parser() -> argparse.ArgumentParser:
    """The command line."""
    result = argparse.ArgumentParser(prog="gluon-kube", description=DESCRIPTION)
    result.add_argument("--version", action="version", version=f"gluon-kube {__version__}")
    commands = result.add_subparsers(dest="command", required=True, metavar="command")

    apply = commands.add_parser("apply", help="apply manifests (--dry-run: report only)")
    apply.add_argument("-f", "--filename", action="append", type=Path, required=True)
    apply.add_argument("--dry-run", action="store_true", help="report only; start nothing")
    add_namespace(apply)

    template = commands.add_parser("template", help="render a chart as `helm template` does")
    template.add_argument("release", help="the release name (.Release.Name)")
    template.add_argument("chart", type=Path, help="the chart directory")
    template.add_argument("-f", "--values", action="append", type=Path, default=[])
    template.add_argument("-n", "--namespace", default="default", help="helm's default")

    cluster = commands.add_parser("cluster", help="start, stop or check the simulated cluster")
    cluster.add_argument("action", choices=("start", "stop", "status"))
    cluster.add_argument(
        "--backoff-base", type=float, default=10.0, help="the restart backoff's first delay (s)"
    )

    get = commands.add_parser("get", help="list pods, deployments, services, jobs")
    get.add_argument("kind")
    get.add_argument("name", nargs="?")
    get.add_argument("-o", "--output", choices=("wide", "name"))
    add_namespace(get)

    logs = commands.add_parser("logs", help="a container's output")
    logs.add_argument("ref", help="pod/NAME, deploy/NAME or job/NAME")
    logs.add_argument("-c", "--container")
    logs.add_argument("-p", "--previous", action="store_true")
    logs.add_argument("--tail", type=int, default=-1)
    add_namespace(logs)

    exec_ = commands.add_parser("exec", help="run a command in a running container")
    exec_.add_argument("ref")
    exec_.add_argument("-c", "--container")
    exec_.add_argument("-i", "--stdin", action="store_true", help="pass stdin to the command")
    exec_.add_argument("-t", "--tty", action="store_true", help="(accepted)")
    exec_.add_argument("argv", nargs=argparse.REMAINDER)
    add_namespace(exec_)

    forward = commands.add_parser(
        "port-forward", help="forward local ports to a Service or pod (LOCAL:REMOTE or PORT)"
    )
    forward.add_argument("ref", help="svc/NAME, deploy/NAME or pod/NAME")
    forward.add_argument("ports", nargs="+")
    forward.add_argument("--address", default="127.0.0.1")
    add_namespace(forward)

    proxy = commands.add_parser(
        "proxy", help="the cluster's names outside it: an HTTP proxy on localhost"
    )
    proxy.add_argument("--port", type=int, default=3128)
    proxy.add_argument("--address", default="127.0.0.1")

    run = commands.add_parser("run", help="a command in a throwaway pod with the cluster's names")
    run.add_argument("--env", action="append", default=[], help="KEY=VALUE")
    run.add_argument("argv", nargs=argparse.REMAINDER)

    delete = commands.add_parser("delete", help="delete objects")
    delete.add_argument("ref", nargs="*", help="KIND NAME or KIND/NAME")
    delete.add_argument("-f", "--filename", action="append", type=Path, default=[])
    delete.add_argument("--ignore-not-found", action="store_true")
    add_namespace(delete)

    wait = commands.add_parser("wait", help="wait for a condition")
    wait.add_argument("ref", nargs="+", help="KIND/NAME, or KIND with --all")
    wait.add_argument("--for", dest="condition", required=True, help="condition=Ready|complete")
    wait.add_argument("--all", action="store_true")
    wait.add_argument("--timeout", default="30s")
    add_namespace(wait)

    create = commands.add_parser("create", help="create job NAME --from=cronjob/NAME")
    create.add_argument("kind", choices=("job",))
    create.add_argument("name")
    create.add_argument("--from", dest="source", required=True, help="cronjob/NAME")
    add_namespace(create)

    helm = commands.add_parser("helm", help="helm upgrade --install RELEASE CHART -f VALUES")
    helm.add_argument("action", choices=("upgrade",))
    helm.add_argument("--install", action="store_true", required=True)
    helm.add_argument("release")
    helm.add_argument("chart", type=Path)
    helm.add_argument("-f", "--values", action="append", type=Path, default=[])
    add_namespace(helm)

    build = commands.add_parser("build", help="build a Rust port's image parts (its Dockerfile)")
    build.add_argument("service", help="e.g. user-service-rs")

    up = commands.add_parser(
        "up", help="bin/k8s-local-up's sequence (<SERVICE>_IMPL, default rust)"
    )
    up.add_argument("services", nargs="*")

    rollout = commands.add_parser("rollout", help="restart a Deployment, or wait for its rollout")
    rollout.add_argument("action", choices=("restart", "status"))
    rollout.add_argument("ref", help="deployment/NAME")
    rollout.add_argument("--timeout", default="300s")
    add_namespace(rollout)
    return result


def seconds(text: str) -> float:
    """kubectl's durations: `180s`, `2m`, `1h`, or a bare number of seconds."""
    units = {"s": 1, "m": 60, "h": 3600}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def manifest_files(paths: Sequence[Path]) -> list[Path]:
    """Files as given; a directory's *.yaml/*.yml, sorted (kubectl apply -f DIR)."""
    result: list[Path] = []
    for path in paths:
        if path.is_dir():
            result += sorted(
                p for p in path.iterdir() if p.is_file() and p.suffix in MANIFEST_SUFFIXES
            )
        elif path.is_file() or str(path) == "-":
            result.append(path)
        else:
            raise ManifestError(f"{path}: no such file or directory")
    return result


def apply(files: Sequence[Path], dry_run: bool) -> int:
    """`apply`: --dry-run reports each object (1 if anything is unsupported); otherwise
    the cluster stores them, as kubectl reports (1 if any was refused)."""
    if dry_run:
        items = load(files)
        for item in items:
            print("\n".join(describe(item)))
        print(summary(items))
        return 0 if all(item.supported for item in items) else 1
    documents = []
    for path in manifest_files(files):
        text = sys.stdin.read() if str(path) == "-" else path.read_text()
        documents.append({"source": str(path), "text": text})
    answer = client.request("apply", documents=documents)
    lines = answer["lines"]
    assert isinstance(lines, list)
    for line in lines:
        print(line, file=sys.stderr if str(line).startswith("error") else sys.stdout)
    return 1 if any(str(line).startswith("error") for line in lines) else 0


def table(rows: Sequence[Sequence[str]]) -> str:
    """Columns padded as kubectl's (three spaces between)."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "   ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    )


def get(kind_word: str, name: str | None, output: str | None) -> int:
    """`get pods|deployments|services|jobs`, kubectl's columns."""
    kind = kind_of(kind_word)
    if kind == "Pod":
        pods = client.request("pods")["pods"]
        assert isinstance(pods, list)
        pods = [p for p in pods if name is None or p["name"] == name]
        if name is not None and not pods:
            raise ClusterError(f'pods "{name}" not found')
        if output == "name":
            print("\n".join(resource_name("Pod", str(p["name"])) for p in pods))
            return 0
        head = ["NAME", "READY", "STATUS", "RESTARTS", "AGE"] + (["IP"] if output else [])
        rows = [
            [str(p["name"]), str(p["ready"]), str(p["status"]), str(p["restarts"]), str(p["age"])]
            + ([str(p["ip"])] if output else [])
            for p in pods
        ]
        if not rows:
            print(f"No resources found in {NAMESPACE} namespace.", file=sys.stderr)
            return 0
        print(table([head, *rows]))
        return 0
    if kind == "Service":
        services = client.request("services")["services"]
        assert isinstance(services, list)
        rows = []
        for service in services:
            if name is not None and service["name"] != name:
                continue
            endpoints = ",".join(
                f"{ip}:{port}" for _, targets in service["endpoints"] for ip, port in targets
            )
            ports = ",".join(f"{port}/TCP" for port, _ in service["endpoints"])
            rows.append([service["name"], "ClusterIP", service["ip"], ports, endpoints or "<none>"])
        print(table([["NAME", "TYPE", "CLUSTER-IP", "PORT(S)", "ENDPOINTS"], *rows]))
        return 0
    objects = client.request("objects")["objects"]
    assert isinstance(objects, list)
    names = [n for k, n in objects if k == kind and (name is None or n == name)]
    if name is not None and not names:
        raise ClusterError(f'{kind_word} "{name}" not found')
    if output == "name":
        print("\n".join(resource_name(kind, n) for n in names))
        return 0
    print("\n".join(["NAME", *names]))
    return 0


def logs(ref: str, container: str | None, previous: bool, tail: int) -> int:
    """`logs`: the container's output so far (--previous: its last instance's)."""
    kind, name = split_ref(ref, "pod") if "/" in ref else ("Pod", ref)
    answer = client.request(
        "logs", ref=f"{kind}/{name}", container=container, previous=previous, tail=tail
    )
    sys.stdout.write(str(answer["text"]))
    return 0


def delete(refs: Sequence[str], files: Sequence[Path], ignore_not_found: bool) -> int:
    """`delete KIND NAME`, `delete KIND/NAME …` or `delete -f FILE`."""
    targets: list[tuple[str, str]] = []
    if files:
        from gluon_kube.report import kind_of as object_kind
        from gluon_kube.report import name_of

        targets += [(object_kind(i.obj), name_of(i.obj)) for i in load(manifest_files(files))]
    if len(refs) == 2 and "/" not in refs[0]:
        targets.append(split_ref(refs[1], refs[0]))
    else:
        targets += [split_ref(ref) for ref in refs]
    status = 0
    for kind, name in targets:
        deleted = client.request("delete", kind=kind, name=name)["deleted"]
        if deleted:
            print(f'{kind.lower()} "{name}" deleted')
        elif not ignore_not_found:
            print(
                f'Error from server (NotFound): {kind.lower()}s "{name}" not found', file=sys.stderr
            )
            status = 1
    return status


def wait(refs: Sequence[str], condition: str, all_: bool, timeout: float) -> int:
    """`wait --for=condition=Ready pod…|deploy/x` or `--for=condition=complete job/x`."""
    wanted = condition.removeprefix("condition=").lower()
    deadline = time.monotonic() + timeout
    while True:
        pending = waiting_for(refs, wanted, all_)
        if not pending:
            return 0
        if time.monotonic() > deadline:
            for item in pending:
                print(f"error: timed out waiting for the condition on {item}", file=sys.stderr)
            return 1
        time.sleep(0.5)


def waiting_for(refs: Sequence[str], wanted: str, all_: bool) -> list[str]:
    """What doesn't meet the condition yet."""
    pending: list[str] = []
    pods = client.request("pods")["pods"]
    assert isinstance(pods, list)
    if all_:
        kind = kind_of(refs[0])
        if kind != "Pod":
            raise ClusterError("--all: only pods")
        if not pods:
            return ["pods (none yet)"]  # a just-applied Deployment's pod comes next tick
        return [f"pods/{p['name']}" for p in pods if not p["is_ready"]]
    for ref in refs:
        kind, name = split_ref(ref)
        if kind == "Job" and wanted == "complete":
            if not client.request("job", name=name)["succeeded"]:
                pending.append(f"jobs/{name}")
        elif kind == "Pod":
            if not any(p["name"] == name and p["is_ready"] for p in pods):
                pending.append(f"pods/{name}")
        elif kind == "Deployment" and wanted in ("available", "ready"):
            owned = [p for p in pods if p["owner"] == f"Deployment/{name}" and p["is_ready"]]
            if not owned:
                pending.append(f"deployments/{name}")
        else:
            raise ClusterError(f"can't wait for {wanted} on {ref}")
    return pending


def rollout(action: str, ref: str, timeout: float) -> int:
    """`rollout restart deployment/x` or `rollout status deployment/x`."""
    kind, name = split_ref(ref, "deployment")
    if kind != "Deployment":
        raise ClusterError("rollout: only deployments")
    if action == "restart":
        client.request("rollout-restart", name=name)
        print(f"deployment.apps/{name} restarted")
        return 0
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        answer = client.request("rollout-status", name=name)
        message = str(answer["message"])
        if message != last:
            print(message)
            last = message
        if answer["done"]:
            return 0
        if time.monotonic() > deadline:
            print(f"error: timed out waiting for the rollout of {name}", file=sys.stderr)
            return 1
        time.sleep(0.5)


def cluster(action: str, backoff_base: float) -> int:
    """`cluster start|stop|status`."""
    state = client.state_dir()
    if action == "start":
        pid = client.start(state, backoff_base)
        print(f"cluster started: node pid {pid}, state {state}")
        return 0
    if action == "stop":
        print("cluster stopped" if client.stop(state) else "no cluster was running")
        return 0
    if client.running(state):
        pods = client.request("pods")["pods"]
        assert isinstance(pods, list)
        print(f"cluster running: state {state}, {len(pods)} pods")
        return 0
    print("no cluster running")
    return 1


def strip_dashes(argv: Sequence[str]) -> list[str]:
    """REMAINDER arguments without their leading `--`."""
    return list(argv[1:] if argv and argv[0] == "--" else argv)


def dispatch(args: argparse.Namespace) -> int:
    """Runs the parsed command."""
    if getattr(args, "namespace", NAMESPACE) not in (NAMESPACE, "default"):
        raise ClusterError(f"only the {NAMESPACE} namespace is simulated")
    match args.command:
        case "template":
            sys.stdout.write(
                as_helm_output(render_chart(args.release, args.chart, args.values, args.namespace))
            )
            return 0
        case "apply":
            return apply(args.filename, args.dry_run)
        case "cluster":
            return cluster(args.action, args.backoff_base)
        case "get":
            return get(args.kind, args.name, args.output)
        case "logs":
            return logs(args.ref, args.container, args.previous, args.tail)
        case "exec":
            ref = args.ref if "/" in args.ref else f"pod/{args.ref}"
            return client.execute(ref, args.container, strip_dashes(args.argv), args.stdin)
        case "port-forward":
            mappings = []
            for spec in args.ports:
                local, _, remote = spec.partition(":")
                mappings.append((int(local or 0), int(remote or local)))
            stop = threading.Event()
            try:
                client.port_forward(args.ref, mappings, args.address, stop)
            except KeyboardInterrupt:
                stop.set()
            return 0
        case "proxy":
            client.request("ping")
            server = client.ClusterNamesProxy((args.address, args.port))
            host, port = server.address
            print(
                f"Serving the cluster's names on {host}:{port} (an HTTP proxy): "
                f"http_proxy=http://{host}:{port}, or a browser's --proxy-server",
                flush=True,
            )
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                server.close()
            return 0
        case "run":
            env = dict(item.split("=", 1) for item in args.env)
            return client.run(strip_dashes(args.argv), env)
        case "delete":
            return delete(args.ref, args.filename, args.ignore_not_found)
        case "wait":
            return wait(args.ref, args.condition, args.all, seconds(args.timeout))
        case "create":
            kind, cron_job = split_ref(args.source)
            if kind != "CronJob":
                raise ClusterError("create job --from: only cronjob/NAME")
            print(client.request("create-job", cronjob=cron_job, name=args.name)["line"])
            return 0
        case "rollout":
            return rollout(args.action, args.ref, seconds(args.timeout))
        case "helm":
            text = as_helm_output(render_chart(args.release, args.chart, args.values, NAMESPACE))
            documents = [{"source": f"release {args.release}", "text": text}]
            answer = client.request("helm", release=args.release, documents=documents)
            print(answer["message"])
            return 0
        case "build":
            gluon = gluon_root()
            if gluon is None:
                raise ClusterError("no gluon checkout (GLUON_KUBE_GLUON)")
            return build(gluon / "services" / args.service)
        case "up":
            gluon = gluon_root()
            if gluon is None:
                raise ClusterError("no gluon checkout (GLUON_KUBE_GLUON)")
            return up(args.services, gluon)
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the command; returns its exit code (argparse exits itself on --help,
    --version and usage errors)."""
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return dispatch(args)
    except (ManifestError, ChartError, TemplateError, ValuesError) as error:
        print(f"gluon-kube: error: {error}", file=sys.stderr)
        return 2
    except ClusterError as error:
        print(f"gluon-kube: error: {error}", file=sys.stderr)
        return 1
