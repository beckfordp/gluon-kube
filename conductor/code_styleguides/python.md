# Python Style Guide

Based on the Google Python Style Guide, narrowed to what gluon-kube needs.
(From gluon-contract's, 2026-11-15.)
ruff (format + lint) and `mypy --strict` enforce most of it; the rest is
review.

## 1. Language
- Python 3.13 syntax: built-in generics (`list[str]`), `X | None`, `match`
  where it reads better than an `if` chain.
- Every function, method and module-level variable is type-annotated;
  `mypy --strict` passes with no `# type: ignore` unless a comment says why.
- No `Any` in our own signatures. Data from the wire (`json.loads`, headers)
  is narrowed at the boundary into typed values (`TypedDict`, frozen
  dataclasses) before the rest of the code sees it.
- Immutable data by default: `@dataclass(frozen=True, slots=True)`, tuples,
  `Mapping`/`Sequence` in signatures.
- No mutable default arguments, no module-level mutable state, no
  `global`.

## 2. Structure
- Pure helpers (parsing, normalising, comparing specs and captures, building expected
  values, rendering templates, merging values) are plain functions with no I/O,
  in `src/gluon_kube/`, and are unit-tested.
- I/O (processes, namespaces, sockets, files) lives in small, clearly named
  modules and is passed in — never opened at import time.
- Errors gluon-kube expects (bad config, a process that never became
  ready) are specific exception classes with a message that says what to do;
  no bare `except:` or `except Exception:` that swallows.

## 3. Naming and formatting
- `snake_case` for modules, functions and variables; `PascalCase` for classes;
  `UPPER_CASE` for constants. No leading-underscore "private" convention
  beyond module internals.
- ruff format is the formatter (line length 100); imports sorted by ruff.
- Docstrings (Google style) on every public module, class and function. Code
  that simulates a Kubernetes behaviour names where that behaviour is
  specified (the manifest, the image's documented contract, Kubernetes' docs).

## 4. Tests and checks
- pytest style: plain `assert`, fixtures over setup methods,
  `pytest.mark.parametrize` for tables of cases, `pytest.raises` with `match`.
- Unit tests (`tests/unit/`) never start processes, touch the network or the
  clock; inject time and randomness where a helper needs them.
- Integration tests (`tests/integration/`) follow `product-guidelines.md`.
  Waiting is polling with a deadline, never `time.sleep` for a fixed period
  (probe periods and restart backoff come from an injected clock).

## Review checklist
- [ ] Fully typed, no unexplained `type: ignore`, no `Any` in our signatures?
- [ ] Wire data narrowed to typed values at the boundary?
- [ ] Pure helpers free of I/O and unit-tested?
- [ ] No fixed sleeps; waits are polls with deadlines?
- [ ] Simulated behaviour names its source; unsupported input reported, not dropped?
- [ ] Failure messages give expected vs actual?
