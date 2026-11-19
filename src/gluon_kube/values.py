# SPDX-License-Identifier: MIT

"""Helm's values (plan GK-3.3): the chart's `values.yaml`, then each `-f` file in order.
Maps merge key by key, lists and scalars replace, and `null` deletes a key (Helm's
documented coalescing, helm.sh/docs/chart_template_guide/values_files)."""

from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

type Values = dict[str, object]


class ValuesError(Exception):
    """A values file gluon-kube can't read; the message names it."""


def merge(base: Mapping[str, object], override: Mapping[str, object]) -> Values:
    """`override` over `base`, as a new mapping; neither input changes."""
    result: Values = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(current, Mapping):
            result[key] = merge(current, value)
        else:
            result[key] = value
    return result


def read(path: Path) -> Values:
    """One values file as a map (empty file: no values)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ValuesError(f"{path}: no such file") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValuesError(f"{path}: not valid YAML: {error}") from None
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ValuesError(f"{path}: values must be a map, got {type(data).__name__}")
    return {str(key): value for key, value in data.items()}


def load_values(chart_values: Path, files: Sequence[Path]) -> Values:
    """The chart's values (if it has a values.yaml), then each file over them."""
    result = read(chart_values) if chart_values.is_file() else {}
    for path in files:
        result = merge(result, read(path))
    return result
