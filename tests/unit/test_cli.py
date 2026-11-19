# SPDX-License-Identifier: MIT

"""The gluon-kube command's skeleton (GK-2.2): version, help, and no command yet."""

from pathlib import Path

import pytest

from gluon_kube.cli import main


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "gluon-kube 0.1.0\n"


def test_help_says_what_it_is(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "simulated gluon-local" in out
    assert "ADR 0030" in out


def test_no_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: gluon-kube")
    assert "the following arguments are required: command" in captured.err


MANIFESTS = """\
apiVersion: v1
kind: Namespace
metadata: {name: gluon-local}
---
apiVersion: v1
kind: Service
metadata: {name: s, namespace: gluon-local}
spec:
  selector: {app: s}
  ports: [{port: 80}]
"""


def test_apply_dry_run_reports_each_object_and_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifests = tmp_path / "m.yaml"
    manifests.write_text(MANIFESTS)
    assert main(["apply", "--dry-run", "-f", str(manifests)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Namespace/gluon-local  simulated",
        "Service/s  ClusterIP: 80->80",
        "2 objects (1 Namespace, 1 Service): everything as written",
    ]


def test_apply_dry_run_exits_1_when_something_is_unsupported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifests = tmp_path / "m.yaml"
    manifests.write_text(MANIFESTS.replace("ports:", "type: NodePort\n  ports:"))
    assert main(["apply", "--dry-run", "-f", str(manifests)]) == 1
    out = capsys.readouterr().out
    assert "  unsupported: spec.type (only ClusterIP Services are simulated)" in out
    assert out.splitlines()[-1] == "2 objects (1 Namespace, 1 Service): 1 UNSUPPORTED (Service/s)"


def test_apply_refuses_malformed_input_with_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifests = tmp_path / "m.yaml"
    manifests.write_text("- not a manifest\n")
    assert main(["apply", "--dry-run", "-f", str(manifests)]) == 2
    assert capsys.readouterr().err == f"gluon-kube: error: {manifests}#1: not a mapping\n"


def test_apply_without_dry_run_needs_a_cluster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLUON_KUBE_STATE", str(tmp_path / "state"))
    assert main(["apply", "-f", str(tmp_path)]) == 1
    assert "no cluster running: gluon-kube cluster start" in capsys.readouterr().err


def test_an_unknown_option_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["apply", "--dry-run", "-f", "x.yaml", "--no-such-option"])
    assert exit_info.value.code == 2
    assert "unrecognized arguments: --no-such-option" in capsys.readouterr().err


def write_chart(root: Path, template: str) -> Path:
    (root / "templates").mkdir(parents=True)
    (root / "Chart.yaml").write_text("apiVersion: v2\nname: c\nversion: 0.1.0\n")
    (root / "values.yaml").write_text("port: 1\n")
    (root / "templates" / "cm.yaml").write_text(template)
    return root


def test_template_prints_helm_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = write_chart(tmp_path / "c", "kind: ConfigMap\nname: {{ .Release.Name }}\n")
    values = tmp_path / "v.yaml"
    values.write_text("port: 2\n")
    assert main(["template", "r", str(root), "-f", str(values), "--namespace", "gluon-local"]) == 0
    assert capsys.readouterr().out == (
        "---\n# Source: c/templates/cm.yaml\nkind: ConfigMap\nname: r\n"
    )


def test_template_refuses_unsupported_constructs_with_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = write_chart(tmp_path / "c", 'kind: ConfigMap\nmetadata:\n  name: {{ include "x" . }}\n')
    assert main(["template", "r", str(root)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("gluon-kube: error: c/templates/cm.yaml: line 3: 'include'")
