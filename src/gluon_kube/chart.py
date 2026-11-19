# SPDX-License-Identifier: MIT

"""A Helm chart rendered as `helm template` renders it (plan GK-3.4, GK-3.6).

`Chart.yaml` and `values.yaml` are read, each `-f` file merged over the values (Helm's
rules, `values.py`), and every `templates/*.yaml` rendered with `.Release`, `.Values`
and `.Chart`. Files whose names start with `_` are partials in Helm and never rendered;
gluon's chart has none, and the renderer refuses `include` anyway. Each rendered file is
split into documents, empty ones dropped, and the documents sorted as Helm installs
them (by kind, then template path: Helm's `releaseutil.InstallOrder`).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from gluon_kube.template import render
from gluon_kube.values import Values, load_values

INSTALL_ORDER = [
    "PriorityClass",
    "Namespace",
    "NetworkPolicy",
    "ResourceQuota",
    "LimitRange",
    "PodSecurityPolicy",
    "PodDisruptionBudget",
    "ServiceAccount",
    "Secret",
    "SecretList",
    "ConfigMap",
    "StorageClass",
    "PersistentVolume",
    "PersistentVolumeClaim",
    "CustomResourceDefinition",
    "ClusterRole",
    "ClusterRoleList",
    "ClusterRoleBinding",
    "ClusterRoleBindingList",
    "Role",
    "RoleList",
    "RoleBinding",
    "RoleBindingList",
    "Service",
    "DaemonSet",
    "Pod",
    "ReplicationController",
    "ReplicaSet",
    "Deployment",
    "HorizontalPodAutoscaler",
    "StatefulSet",
    "Job",
    "CronJob",
    "IngressClass",
    "Ingress",
    "APIService",
]
"""Helm 3's install order by kind (pkg/releaseutil/kind_sorter.go)."""

SEPARATOR = re.compile(r"(?:^|\n)---[^\n]*(?=\n|$)")
KIND = re.compile(r"^kind:\s*(\S+)", re.MULTILINE)


class ChartError(Exception):
    """A chart gluon-kube can't read; the message names the file."""


@dataclass(frozen=True, slots=True)
class Rendered:
    """One rendered document and the template it came from (`<chart>/templates/<file>`)."""

    source: str
    text: str

    @property
    def kind(self) -> str:
        """The document's kind, as Helm's sorter reads it ("" if none)."""
        match = KIND.search(self.text)
        return match.group(1) if match else ""


def chart_name(chart: Path) -> str:
    """The chart's name from Chart.yaml."""
    path = chart / "Chart.yaml"
    try:
        meta = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ChartError(f"{chart}: not a chart (no Chart.yaml)") from None
    except yaml.YAMLError as error:
        raise ChartError(f"{path}: not valid YAML: {error}") from None
    if not isinstance(meta, Mapping) or not isinstance(meta.get("name"), str):
        raise ChartError(f"{path}: no name")
    return str(meta["name"])


def chart_meta(chart: Path) -> dict[str, object]:
    """`.Chart`: Chart.yaml's fields with Helm's capitalised names."""
    meta = yaml.safe_load((chart / "Chart.yaml").read_text())
    assert isinstance(meta, Mapping)
    return {str(key)[:1].upper() + str(key)[1:]: value for key, value in meta.items()}


def split(text: str) -> list[str]:
    """A rendered file's documents, as Helm's SplitManifests: on `---` lines, each
    trimmed; blank or comment-only documents dropped."""
    result = []
    for part in SEPARATOR.split(text):
        body = part.strip()
        if any(line.strip() and not line.lstrip().startswith("#") for line in body.splitlines()):
            result.append(body + "\n")
    return result


def order_key(rendered: Rendered) -> tuple[int, str, str]:
    """Helm's install order: known kinds by rank, unknown kinds after them by name;
    ties by template path."""
    kind = rendered.kind
    if kind in INSTALL_ORDER:
        return (INSTALL_ORDER.index(kind), "", rendered.source)
    return (len(INSTALL_ORDER), kind, rendered.source)


def render_chart(
    release: str, chart: Path, values_files: Sequence[Path], namespace: str = "default"
) -> list[Rendered]:
    """The chart's documents for `release` in `namespace`, in Helm's order."""
    name = chart_name(chart)
    values: Values = load_values(chart / "values.yaml", values_files)
    context: dict[str, object] = {
        "Release": {
            "Name": release,
            "Namespace": namespace,
            "Service": "Helm",
            "IsInstall": True,
            "IsUpgrade": False,
            "Revision": 1,
        },
        "Values": values,
        "Chart": chart_meta(chart),
    }
    templates = chart / "templates"
    if not templates.is_dir():
        raise ChartError(f"{chart}: no templates/ directory")
    documents: list[Rendered] = []
    for path in sorted(templates.iterdir()):
        if not path.is_file() or path.name.startswith("_") or path.suffix not in (".yaml", ".yml"):
            continue
        source = f"{name}/templates/{path.name}"
        output = render(path.read_text(), source, context)
        documents += [Rendered(source, text) for text in split(output)]
    return sorted(documents, key=order_key)


def as_helm_output(documents: Sequence[Rendered]) -> str:
    """`helm template`'s stdout: each document after `---` and `# Source:`."""
    return "".join(f"---\n# Source: {doc.source}\n{doc.text}" for doc in documents)
