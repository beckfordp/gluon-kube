# SPDX-License-Identifier: MIT

"""`gluon-kube template` against `helm template` output captured on the laptop (GK-3.5,
GK-3.6; ADR 0030 decision 2): every command in the capture's COMMANDS.txt, run through
gluon-kube's renderer, gives the same documents (parsed) from the same templates, in the
same order. The captures are the reference; gluon-kube's output never is."""

import shlex
from pathlib import Path

import pytest
import yaml

from gluon_kube.chart import render_chart

CAPTURES = Path(__file__).resolve().parents[1] / "fixtures" / "helm-template"
GLUON = Path(__file__).resolve().parents[4]


def commands() -> list[tuple[str, list[str]]]:
    """(capture file, helm template argv) per line of COMMANDS.txt."""
    result = []
    for line in (CAPTURES / "COMMANDS.txt").read_text().splitlines():
        argv = shlex.split(line)
        values = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-f"]
        name = Path(values[0]).name.removesuffix(".values.yaml") if values else "_chart-defaults"
        result.append((f"{name}.yaml", argv))
    return result


def documents(text: str) -> list[tuple[str, object]]:
    """(# Source, parsed document) per document of helm template output."""
    result = []
    for part in text.split("\n---\n"):
        part = part.removeprefix("---\n")
        source = next(
            line.removeprefix("# Source: ")
            for line in part.splitlines()
            if line.startswith("# Source: ")
        )
        result.append((source, yaml.safe_load(part)))
    return result


def test_every_capture_has_a_command() -> None:
    captured = {path.name for path in CAPTURES.glob("*.yaml")}
    assert captured == {name for name, _ in commands()}
    assert len(captured) == 10


@pytest.mark.skipif(not (GLUON / "infra" / "k8s" / "gluon").is_dir(), reason="needs gluon")
@pytest.mark.parametrize(("capture", "argv"), commands(), ids=[c for c, _ in commands()])
def test_template_output_equals_the_helm_capture(capture: str, argv: list[str]) -> None:
    assert argv[:2] == ["helm", "template"]
    release, chart = argv[2], GLUON / argv[3]
    namespace = argv[argv.index("--namespace") + 1]
    values = [GLUON / argv[i + 1] for i, arg in enumerate(argv) if arg == "-f"]
    rendered = render_chart(release, chart, values, namespace)
    ours = [(doc.source, yaml.safe_load(doc.text)) for doc in rendered]
    assert ours == documents((CAPTURES / capture).read_text())
