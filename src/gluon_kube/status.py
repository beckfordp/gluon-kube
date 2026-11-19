# SPDX-License-Identifier: MIT

"""What `gluon-kube get pods` shows, as kubectl shows it (GK-4.6): READY, STATUS,
RESTARTS and AGE, by kubectl's rules (its printers: `Init:N/M`, a waiting container's
reason, `Completed`, `Terminating`). `ImageUnavailable` is gluon-kube's own reason for an
image with no recipe (ADR 0030 decision 4), where Kubernetes says `ErrImageNeverPull`."""

from gluon_kube.controller import PodRecord

type Row = dict[str, object]


def age(seconds: float) -> str:
    """kubectl's short age: `45s`, `3m12s` (under 10 minutes), `25m`, `2h5m`, `3d`."""
    s = int(max(seconds, 0))
    if s < 120:
        return f"{s}s"
    minutes, hours = s // 60, s // 3600
    if minutes < 10:
        return f"{minutes}m{s % 60}s"
    if hours < 3:
        return f"{minutes}m" if hours == 0 else f"{hours}h{minutes % 60}m"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def pod_status(pod: PodRecord) -> str:
    """The STATUS column."""
    if pod.deleting_since is not None:
        return "Terminating"
    unavailable = [c for c in pod.all_containers() if c.reason == "ImageUnavailable"]
    if unavailable:
        return "ImageUnavailable"
    if pod.phase == "Succeeded":
        return "Completed"
    if pod.init_containers and pod.phase == "Pending":
        for index, container in enumerate(pod.init_containers):
            if container.reason == "Completed":
                continue
            if container.reason in ("CrashLoopBackOff", "Error"):
                return f"Init:{container.reason}"
            return f"Init:{index}/{len(pod.init_containers)}"
    if pod.phase == "Failed":
        return "Error"
    if pod.sandbox is None:
        return "ContainerCreating" if not pod.message else "Pending"
    for container in pod.containers:
        if container.state != "Running" and container.reason:
            return container.reason
    return "Running"


def pod_row(pod: PodRecord, now: float) -> Row:
    """One `get pods` row, and what else the CLI may show (-o wide, waits)."""
    ready = sum(1 for c in pod.containers if c.state == "Running" and c.ready)
    return {
        "name": pod.name,
        "ready": f"{ready}/{len(pod.containers)}",
        "status": pod_status(pod),
        "restarts": sum(c.restarts for c in pod.all_containers()),
        "age": age(now - pod.created),
        "ip": pod.ip or "<none>",
        "owner": f"{pod.owner_kind}/{pod.owner_name}",
        "is_ready": pod.ready,
        "phase": pod.phase,
        "message": pod.message,
    }
