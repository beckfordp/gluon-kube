#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# The CI checks, defined once: GitHub Actions runs this script, and so does the
# sandbox, which has no GitHub access (as gluon-contract's). Unit and
# integration tests (testpaths = ["tests"]); the spikes/ probes are run by hand.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv sync --locked              # fails if uv.lock is stale against pyproject.toml
uv run ruff format --check    # src/, tests/ and spikes/
uv run ruff check
uv run mypy                   # src/ and tests/ (pyproject.toml)
uv run pytest --cov           # coverage gate from pyproject.toml (80%)
