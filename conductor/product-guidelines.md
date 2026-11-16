# Product Guidelines — gluon-kube

## Code conventions
Canonical: [`code_styleguides/python.md`](./code_styleguides/python.md)
(+ `general.md`). Fully typed, `mypy --strict` clean, ruff clean.

## The manifests are the reference
- gluon's manifests, chart and values files are read in place and never
  copied or edited for the sandbox (ADR 0030 decision 2).
- Every kind and field is either simulated or reported with its object and
  path. Nothing is dropped silently.
- Simulated behaviour names its source (the manifest field, Kubernetes'
  docs, the image's contract) in its docstring.
- A difference from OrbStack found on the laptop becomes a test or an entry
  in the unsupported list.

## Processes and namespaces
- Unprivileged only: no `sudo`, no setuid helpers.
- Everything gluon-kube starts, it can stop: no stray process, namespace,
  port or state directory outlives `cluster stop` or a test.
- Waits are polls with deadlines; no fixed sleeps.

## Testing / quality gate
- Before every commit: `scripts/ci.sh` (format, lint, types, unit and
  integration tests, coverage >80%; CI runs the same script) — from GK-2.
- Unit tests are pure; integration tests start real processes in real
  namespaces and clean up after themselves.
