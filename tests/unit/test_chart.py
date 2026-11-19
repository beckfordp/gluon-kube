# SPDX-License-Identifier: MIT

"""A chart rendered as `helm template` does (GK-3.4, GK-3.6): Helm's document splitting,
its install order by kind, its `# Source:` output, and the errors for what isn't a chart.
Expectations from Helm's documented behaviour (pkg/releaseutil)."""

from pathlib import Path

import pytest

from gluon_kube.chart import ChartError, Rendered, as_helm_output, order_key, render_chart, split
from gluon_kube.template import TemplateError


def chart(tmp_path: Path, templates: dict[str, str], values: str = "port: 8080\n") -> Path:
    root = tmp_path / "mychart"
    (root / "templates").mkdir(parents=True)
    (root / "Chart.yaml").write_text("apiVersion: v2\nname: mychart\nversion: 0.1.0\n")
    (root / "values.yaml").write_text(values)
    for name, text in templates.items():
        (root / "templates" / name).write_text(text)
    return root


def test_split_on_separator_lines_dropping_empty_and_comment_only_documents() -> None:
    text = "# only a comment\n---\na: 1\n---   \n\n---\n# c\nb: 2\n"
    assert split(text) == ["a: 1\n", "# c\nb: 2\n"]


def test_install_order_is_by_kind_then_template_unknown_kinds_last() -> None:
    docs = [
        Rendered("c/templates/z.yaml", "kind: Deployment\n"),
        Rendered("c/templates/y.yaml", "kind: Widget\n"),
        Rendered("c/templates/b.yaml", "kind: Service\n"),
        Rendered("c/templates/a.yaml", "kind: Service\n"),
        Rendered("c/templates/x.yaml", "kind: ConfigMap\n"),
        Rendered("c/templates/w.yaml", "kind: Apple\n"),
    ]
    ordered = [d.source.rsplit("/", 1)[1] for d in sorted(docs, key=order_key)]
    assert ordered == ["x.yaml", "a.yaml", "b.yaml", "z.yaml", "w.yaml", "y.yaml"]


def test_render_a_chart_with_values_files_release_and_namespace(tmp_path: Path) -> None:
    root = chart(
        tmp_path,
        {
            "deployment.yaml": "kind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\n"
            "  namespace: {{ .Release.Namespace }}\nport: {{ .Values.port }}\n",
            "service.yaml": "kind: Service\nchart: {{ .Chart.Name }}\n",
            "_helpers.tpl": "{{ define }}",
            "NOTES.txt": "not rendered",
        },
    )
    override = tmp_path / "local.yaml"
    override.write_text("port: 9000\n")
    docs = render_chart("rel", root, [override], "gluon-local")
    assert as_helm_output(docs) == (
        "---\n# Source: mychart/templates/service.yaml\nkind: Service\nchart: mychart\n"
        "---\n# Source: mychart/templates/deployment.yaml\nkind: Deployment\nmetadata:\n"
        "  name: rel\n  namespace: gluon-local\nport: 9000\n"
    )


def test_namespace_defaults_to_helms_default(tmp_path: Path) -> None:
    root = chart(tmp_path, {"cm.yaml": "kind: ConfigMap\nns: {{ .Release.Namespace }}\n"})
    assert render_chart("r", root, [])[0].text == "kind: ConfigMap\nns: default\n"


def test_template_errors_name_the_chart_path(tmp_path: Path) -> None:
    root = chart(tmp_path, {"cm.yaml": 'kind: ConfigMap\n\nname: {{ include "x" . }}\n'})
    with pytest.raises(TemplateError, match=r"^mychart/templates/cm\.yaml: line 3: 'include'"):
        render_chart("r", root, [])


def test_not_a_chart(tmp_path: Path) -> None:
    with pytest.raises(ChartError, match="not a chart"):
        render_chart("r", tmp_path, [])
    (tmp_path / "Chart.yaml").write_text("version: 1\n")
    with pytest.raises(ChartError, match="no name"):
        render_chart("r", tmp_path, [])
    (tmp_path / "Chart.yaml").write_text("name: [\n")
    with pytest.raises(ChartError, match="not valid YAML"):
        render_chart("r", tmp_path, [])
    (tmp_path / "Chart.yaml").write_text("name: x\n")
    with pytest.raises(ChartError, match="no templates/"):
        render_chart("r", tmp_path, [])
