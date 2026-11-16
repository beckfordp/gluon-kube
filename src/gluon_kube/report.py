# SPDX-License-Identifier: MIT

"""What `gluon-kube apply --dry-run` says about each object (plan GK-3.7): what would
run, what is recorded only, what isn't simulated and what is unsupported (ADR 0030
decisions 2 and 5: nothing is skipped silently)."""

from collections import Counter
from collections.abc import Sequence

from gluon_kube.manifests import Loaded
from gluon_kube.model import (
    ConfigMap,
    CronJob,
    Deployment,
    Job,
    KubeObject,
    Namespace,
    NotSimulated,
    PodTemplate,
    Service,
)


def kind_of(obj: KubeObject) -> str:
    """The object's Kubernetes kind."""
    return obj.kind if isinstance(obj, NotSimulated) else type(obj).__name__


def name_of(obj: KubeObject) -> str:
    """The object's name."""
    return obj.name if isinstance(obj, NotSimulated) else obj.meta.name


def containers(template: PodTemplate) -> str:
    """`name (image)` for each container, init containers first."""
    spec = template.spec
    return ", ".join(f"{c.name} ({c.image})" for c in (*spec.init_containers, *spec.containers))


def what(obj: KubeObject) -> str:
    """What gluon-kube would do with the object."""
    match obj:
        case Deployment():
            pods = "1 pod" if obj.replicas == 1 else f"{obj.replicas} pods"
            return f"runs {pods}: {containers(obj.template)}"
        case Service():
            ports = ", ".join(f"{p.port}->{p.target_port}" for p in obj.ports)
            return f"ClusterIP: {ports}"
        case ConfigMap():
            return "1 key" if len(obj.data) == 1 else f"{len(obj.data)} keys"
        case Job():
            return (
                f"runs to completion: {containers(obj.template)}, backoffLimit {obj.backoff_limit}"
            )
        case CronJob():
            return f"schedule {obj.schedule!r} recorded, not run; its job runs by hand"
        case Namespace():
            return "simulated"
        case NotSimulated():
            return "not simulated"


def describe(loaded: Loaded) -> list[str]:
    """The object's line, then one line per note."""
    obj = loaded.obj
    head = f"{kind_of(obj)}/{name_of(obj)}  "
    if loaded.supported:
        head += what(obj)
    else:
        doing = what(obj).replace("runs ", "would run ", 1)
        head += f"UNSUPPORTED, won't run ({doing})"
    return [head] + [f"  {note.status}: {note.path} ({note.detail})" for note in loaded.notes]


def plural(count: int, word: str) -> str:
    """`1 word` or `n words`."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def summary(items: Sequence[Loaded]) -> str:
    """One line: objects by kind, then what isn't as written."""
    kinds = Counter(kind_of(item.obj) for item in items)
    by_kind = ", ".join(plural(count, kind) for kind, count in sorted(kinds.items()))
    head = f"{plural(len(items), 'object')} ({by_kind}): "
    unsupported = [item for item in items if not item.supported]
    if unsupported:
        names = ", ".join(f"{kind_of(i.obj)}/{name_of(i.obj)}" for i in unsupported)
        return head + f"{len(unsupported)} UNSUPPORTED ({names})"
    recorded = sum(1 for item in items for note in item.notes if note.status == "recorded")
    others = sum(1 for item in items if isinstance(item.obj, NotSimulated))
    parts = []
    if recorded:
        parts.append(f"{plural(recorded, 'field')} recorded only")
    if others:
        parts.append(f"{plural(others, 'object')} not simulated")
    if not parts:
        return head + "everything as written"
    return head + ", ".join(parts) + "; everything else as written"
