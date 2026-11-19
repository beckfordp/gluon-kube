# SPDX-License-Identifier: MIT

"""The Go-template subset (GK-3.4). Expectations from Go's text/template documentation
(trimming, emptiness, range over maps in key order), sprig's (`quote`, `indent`,
`nindent`) and Helm's (`toYaml` without its trailing newline; a missing value renders
as nothing), never from gluon-kube's own output."""

import pytest

from gluon_kube.template import TemplateError, empty, go_quote, render

CONTEXT: dict[str, object] = {
    "Release": {"Name": "user-service", "Namespace": "gluon-local"},
    "Values": {
        "port": 8080,
        "ratio": 0.5,
        "big": 8080.0,
        "on": True,
        "image": {"repository": "user-service-rs", "tag": "latest"},
        "javaOpts": "-XX:MaxRAMPercentage=40",
        "blank": "",
        "env": [{"name": "A", "value": "1"}, {"name": "B", "value": 'say "hi"'}],
        "resources": {"requests": {"memory": "512Mi", "cpu": "250m"}},
        "labels": {"b": "2", "a": "1"},
    },
}


def r(source: str) -> str:
    return render(source, "t.yaml", CONTEXT)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("name: {{ .Release.Name }}", "name: user-service"),
        ("{{.Release.Namespace}}", "gluon-local"),
        (
            'image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"',
            'image: "user-service-rs:latest"',
        ),
        ("{{ .Values.port }}", "8080"),
        ("{{ .Values.ratio }}", "0.5"),
        ("{{ .Values.big }}", "8080"),
        ("{{ .Values.on }}", "true"),
        ("[{{ .Values.missing }}]", "[]"),
        ("[{{ .Values.missing.deeper }}]", "[]"),
        ("[{{ .Values.port.deeper }}]", "[]"),
        ('{{ "literal" }}', "literal"),
        ("{{ `raw\\n` }}", "raw\\n"),
        ("{{ 7 }}", "7"),
        ("a{{/* a comment */}}b", "ab"),
    ],
)
def test_values_print(source: str, expected: str) -> None:
    assert r(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a  \n  {{- .Values.port }}", "a8080"),
        ("{{ .Values.port -}}  \n  b", "8080b"),
        ("a \n{{- .Values.port -}}\n b", "a8080b"),
        ("a {{ .Values.port }} b", "a 8080 b"),
    ],
)
def test_trim_markers_remove_adjacent_whitespace(source: str, expected: str) -> None:
    assert r(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{{ .Values.javaOpts | quote }}", '"-XX:MaxRAMPercentage=40"'),
        ("{{ quote .Values.port }}", '"8080"'),
        ('{{ "say \\"hi\\"\\n" | quote }}', '"say \\"hi\\"\\n"'),
        ("[{{ .Values.missing | quote }}]", "[]"),
        ("{{ .Values.resources | toYaml }}", "requests:\n  cpu: 250m\n  memory: 512Mi"),
        (
            "{{- toYaml .Values.resources | nindent 4 }}",
            "\n    requests:\n      cpu: 250m\n      memory: 512Mi",
        ),
        ("{{ .Values.image.tag | indent 2 }}", "  latest"),
        ("[{{ toYaml .Values.missing }}]", "[]"),
    ],
)
def test_functions(source: str, expected: str) -> None:
    assert r(source) == expected


def test_with_sets_the_dot_and_skips_empty_values() -> None:
    source = (
        "{{- with .Values.javaOpts }}J={{ . }}{{- end }}"
        "{{- with .Values.blank }}B={{ . }}{{ else }}no blank{{- end }}"
    )
    assert r(source) == "J=-XX:MaxRAMPercentage=40no blank"


def test_range_over_a_list_and_a_map_in_key_order() -> None:
    assert r("{{ range .Values.env }}{{ .name }}={{ .value | quote }};{{ end }}") == (
        'A="1";B="say \\"hi\\"";'
    )
    assert r("{{ range .Values.labels }}{{ . }}{{ end }}") == "12"
    assert r("{{ range .Values.missing }}x{{ else }}none{{ end }}") == "none"
    assert r("{{ range 3 }}x{{ end }}") == "xxx"


def test_if_else() -> None:
    assert r("{{ if .Values.on }}yes{{ else }}no{{ end }}") == "yes"
    assert r("{{ if .Values.blank }}yes{{ else }}no{{ end }}") == "no"
    assert r("{{ if .Values.env }}{{ .Release.Name }}{{ end }}") == "user-service"


@pytest.mark.parametrize(
    ("value", "is_empty"),
    [
        (None, True),
        (False, True),
        (0, True),
        (0.0, True),
        ("", True),
        ([], True),
        ({}, True),
        (True, False),
        (1, False),
        ("x", False),
        ([0], False),
        ({"a": 1}, False),
    ],
)
def test_go_emptiness(value: object, is_empty: bool) -> None:
    assert empty(value) is is_empty


def test_quote_escapes_as_go_does() -> None:
    assert go_quote('a"b\\c\td\x01é') == '"a\\"b\\\\c\\td\\x01é"'


@pytest.mark.parametrize(
    ("source", "line", "construct"),
    [
        ('kind: ConfigMap\nmetadata:\n  name: {{ include "x" . }}\n', 3, "include"),
        ('{{ define "x" }}y{{ end }}', 1, "define"),
        ('{{ template "x" . }}', 1, "template"),
        ("a\n{{ $x := 1 }}", 2, "$x"),
        ("{{ .Values.port | default 80 }}", 1, "default"),
        ("{{ if eq .Values.port 1 }}x{{ end }}", 1, "eq"),
        ("{{ (.Values.port) }}", 1, "("),
        ('{{ printf "%d" 1 }}', 1, "printf"),
        ("{{ if .Values.on }}a{{ else if .Values.on }}b{{ end }}", 1, "else if"),
        ("{{ .Values.port | required }}", 1, "required"),
    ],
)
def test_unsupported_constructs_are_refused_naming_template_line_and_construct(
    source: str, line: int, construct: str
) -> None:
    with pytest.raises(TemplateError) as caught:
        r(source)
    message = str(caught.value)
    assert message.startswith(f"t.yaml: line {line}: ")
    assert construct in message


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("{{ if .Values.on }}x", "if has no end"),
        ("x{{ end }}", "unexpected"),
        ("{{ .Values.port", "unclosed action"),
        ("{{/* open }}", "unclosed comment"),
        ("{{ .Values.port | }}", "empty command"),
        ("{{ .Values.port .Values.port }}", "can't give arguments"),
        ("{{ .Values.port | .Values.port }}", "can't give arguments"),
        ("{{ if }}x{{ end }}", "needs a pipeline"),
        ("{{ .Values.env }}", "printing a list"),
        ("{{ nindent .Values.port }}", "wrong arguments to nindent"),
        ("{{ range .Values.on }}x{{ end }}", "can't range over a bool"),
    ],
)
def test_malformed_templates_are_refused(source: str, problem: str) -> None:
    with pytest.raises(TemplateError, match=problem):
        r(source)
