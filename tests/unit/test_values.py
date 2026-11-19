# SPDX-License-Identifier: MIT

"""Helm's values merging (GK-3.3). Expectations from Helm's documented rules
(helm.sh/docs/chart_template_guide/values_files): the chart's values.yaml, then each
-f file in order; maps merge key by key, lists and scalars replace, null deletes."""

from pathlib import Path

import pytest

from gluon_kube.values import ValuesError, load_values, merge


def test_maps_merge_recursively() -> None:
    base = {"image": {"repository": "CHANGE_ME", "tag": "latest", "pullPolicy": "Never"}}
    override = {"image": {"repository": "user-service-rs"}}
    assert merge(base, override) == {
        "image": {"repository": "user-service-rs", "tag": "latest", "pullPolicy": "Never"}
    }


def test_lists_and_scalars_replace() -> None:
    base = {"env": [{"name": "A", "value": "1"}], "port": 8080}
    override = {"env": [{"name": "B", "value": "2"}], "port": 9000}
    assert merge(base, override) == {"env": [{"name": "B", "value": "2"}], "port": 9000}


def test_null_deletes_a_key() -> None:
    assert merge({"javaOpts": "-X", "port": 1}, {"javaOpts": None}) == {"port": 1}


def test_a_map_replaces_a_scalar_and_back() -> None:
    assert merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}
    assert merge({"a": {"b": 2}}, {"a": 1}) == {"a": 1}


def test_inputs_are_not_changed() -> None:
    base = {"image": {"tag": "latest"}}
    merge(base, {"image": {"tag": "v1"}})
    assert base == {"image": {"tag": "latest"}}


def test_files_apply_in_order(tmp_path: Path) -> None:
    chart = tmp_path / "values.yaml"
    chart.write_text("port: 8080\nimage:\n  tag: latest\n")
    first = tmp_path / "a.yaml"
    first.write_text("port: 9000\n")
    second = tmp_path / "b.yaml"
    second.write_text("port: 9100\nimage:\n  tag: v2\n")
    assert load_values(chart, [first, second]) == {"port": 9100, "image": {"tag": "v2"}}


def test_an_empty_values_file_is_no_values(tmp_path: Path) -> None:
    chart = tmp_path / "values.yaml"
    chart.write_text("# nothing\n")
    assert load_values(chart, []) == {}


def test_a_missing_chart_values_file_is_no_values(tmp_path: Path) -> None:
    assert load_values(tmp_path / "values.yaml", []) == {}


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "a: [\n"])
def test_values_that_are_not_a_map_are_refused(tmp_path: Path, text: str) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(text)
    with pytest.raises(ValuesError, match=r"bad\.yaml"):
        load_values(tmp_path / "none.yaml", [bad])


def test_a_missing_values_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValuesError, match=r"nope\.yaml: no such file"):
        load_values(tmp_path / "values.yaml", [tmp_path / "nope.yaml"])
