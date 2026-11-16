# Tech Stack — gluon-kube

Status (2026-11-15): set at conductor setup, before GK-1. Items marked
**Open** are decided, and recorded here with a dated note, in the plan item
that first needs them, before any code depends on them.

## Language / runtime
- **Python 3.13**, fully type-annotated (ADR 0030 decision 7)
- **uv 0.9** — the pinned Python (`.python-version` 3.13), venv, `uv.lock`
  committed; `uv sync --locked` in CI fails on a stale lock. Build backend
  `uv_build`, `src/` layout, package `gluon_kube` (typed: `py.typed`)
  *(2026-11-15, GK-2.2)*
- Console script `gluon-kube` (`gluon_kube.cli:main`; also `python -m
  gluon_kube`). No commands until GK-3; no runtime dependencies until the
  code that needs one (pyroute2, GK-4)

## Test stack (as gluon-contract's) *(2026-11-15, GK-2.2)*
- **pytest 9.2.0** — `testpaths = ["tests"]`: `tests/unit/` (pure) and
  `tests/integration/` (processes; from GK-4, namespaces)
- **pytest-cov 7.1.0** — coverage on `src/gluon_kube`, branch, gate 80%
- **ruff 0.17.3** — format and lint (the same rule set as gluon-contract's),
  over `src/`, `tests/` and `spikes/`
- **mypy 2.4.1**, `strict`, over `src/` and `tests/` (not `spikes/`: the
  probe is a standalone uv script with its own dependencies)

Tool versions are pinned exactly in `pyproject.toml`; bump them deliberately.

## CI *(2026-11-15, GK-2.4)*
- `scripts/ci.sh` defines the checks once: `uv sync --locked`, `ruff format
  --check`, `ruff check`, `mypy`, `pytest --cov`. The sandbox runs it
  directly; GitHub Actions (`ubuntu-latest` only: Linux namespaces) runs the
  same script once the repo is pushed (gluon plan GK-2.6).
- The workflow lifts Ubuntu 24.04's AppArmor restriction on unprivileged user
  namespaces and installs `bubblewrap`, for GK-4's integration tests;
  unverified until the first push.

## Isolation *(2026-11-15, GK-1: decided, `spikes/netns-probe/FINDINGS.md`)*
- **bwrap** (0.9.0, in the sandbox image): the node (`--unshare-user
  --unshare-net`, uid 0 with every capability inside) and each pod (nested:
  `--unshare-net --unshare-pid --die-with-parent`, `--unshare-user --uid N` for
  an image's USER; a tmpfs root with the host's top-level entries bound in, its
  own `/tmp`)
- Netlink for the bridge, veth pairs, addresses and routes: **pyroute2 0.9.7**
  (PyPI, pure Python, pinned) *(2026-11-15, GK-1.2: decided. Inside the node
  namespace it creates the bridge and veth pairs and moves a pod's end by pid;
  the pod's side is configured from a child process that joins the pod's
  network namespace with `os.setns` (not a fork: the node runs threads).
  `iproute2` not needed. It ships no type information: imported with a
  commented `type: ignore`.)*
- **nsenter** (util-linux, in the image) for `exec` and `run`: the pod's user,
  network, mount and pid namespaces
- Services: a userspace proxy in the node (no `iptables` in the image);
  ClusterIPs on the node's `lo` (this kernel has no `dummy` link type)

## Reuse (**Open** until GK-4)
- gluon-contract's process code (`postgres`, `kafka`, `cdc`, `supervisor`)
  as a uv path dependency on `../../tests/contract` (ADR 0030 decision 7)

## Sandbox
A tool added here that the sandbox doesn't already have is added to gluon
`bin/sandbox-setup` in the same change.

## Layout *(2026-11-15, GK-2)*
- `src/gluon_kube/` — the simulator: `cli` (the command) so far
- `tests/unit/`, `tests/integration/`
- `scripts/ci.sh`
- `spikes/` — probes that decide a design; kept with their findings, not
  part of the package, not run by CI
