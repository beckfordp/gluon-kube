# Spec — GK-2 the package (package_20261115)

Source: gluon ADR 0030 decisions 6 and 7; gluon `plans/gluon-kube/plan.md`
GK-2.1–GK-2.6. The owner asked (2026-11-15) to finish GK-2 in this session,
reviewed at its end; GK-1's remaining checks continue afterwards, as GK-2
doesn't depend on them.

## What is built
- **GK-2.1** `LICENSE` (MIT, as gluon-contract's), `README.md`, `.gitignore`,
  `.python-version` (3.13). The git repo exists already (GK-1 Phase 0).
- **GK-2.2** `pyproject.toml` in gluon-contract's shape: Python 3.13, `uv_build`,
  `src/gluon_kube` (typed, `py.typed`), exact dev pins (pytest 9.2.0,
  pytest-cov 7.1.0 with the 80% gate, mypy 2.4.1 strict, ruff 0.17.3), no
  runtime dependencies yet (pyroute2 comes with the first code that uses it,
  GK-4). A `gluon-kube` console script (also `python -m gluon_kube`):
  `--version` prints `gluon-kube 0.1.0`; `--help` says what it is; with no
  command it prints usage to stderr and exits 2, as there are no commands
  until GK-3.
- **GK-2.3** Conductor: set up in GK-1 Phase 0; `tech-stack.md` brought up to
  date with what GK-2 pins.
- **GK-2.4** `scripts/ci.sh`: `uv sync --locked`, `ruff format --check`,
  `ruff check`, `mypy`, `pytest --cov`. `.github/workflows/ci.yml` runs it on
  `ubuntu-latest` only (namespaces are Linux), with the AppArmor sysctl that
  Ubuntu 24.04 runners need for unprivileged user namespaces (unused until
  GK-4, unverified until GK-2.6).
- **GK-2.5** gluon: `bin/lib/sandbox.py` lists the repo (done in GK-1 Phase 0);
  `bin/sandbox-setup` syncs it (`projects`: `uv sync --locked`) and checks what
  GK-1 found it needs (`image`: `bwrap`, `unshare`, `nsenter`, and that an
  unprivileged user namespace can be created); `tools/README.md` lists it;
  `bin/verify-complete-kube-spike.sh` reports GK-2 from `scripts/ci.sh`.
- **GK-2.6** (laptop) `gh repo create beckfordp/gluon-kube`, push, CI green on
  GitHub — not in this track.

## Tests
- Unit: the CLI (`--version`, `--help`, no command) through `main(argv)`.
- Integration: the installed console script and `python -m gluon_kube` run as
  processes.

## Acceptance (agreed before the work; `bin/verify-complete-GK-2.sh` in gluon)
1. `tools/gluon-kube` `scripts/ci.sh` green (format, lint, types, tests, coverage ≥ 80%)
2. `uv run gluon-kube --version` prints `gluon-kube 0.1.0`
3. `bin/sandbox-setup --check` exits 0 and reports the isolation tools and
   `tools/gluon-kube`'s environment
4. `bin/verify-complete-kube-spike.sh`: GK-2 PASS, nothing FAIL
5. `LICENSE`, `README.md`, `.github/workflows/ci.yml` (running
   `scripts/ci.sh`) present; `tools/README.md` lists gluon-kube
6. The track is archived; `tools/gluon-kube` has nothing uncommitted
All PASS: runs `bin/sandbox-save`.
