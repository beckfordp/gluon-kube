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
  gluon_kube`). Runtime dependencies: pyyaml, pyroute2 (GK-4)

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

## Reuse *(2026-11-18, GK-4.1: decided)*
- Not gluon-contract's process code: recipes run the **images' own entrypoints**
  (captured on the laptop, `laptop-reference/images/`, copied into
  `src/gluon_kube/images/`) on the sandbox's binaries, which is closer to the images
  than the harness's reimplementations and less code. No path dependency on
  `../../tests/contract` (ADR 0030 decision 7's "reused at first" didn't apply).

## Runtime *(2026-11-18, GK-4)*
- The node: `python -m gluon_kube.node`, in-memory state, a Unix-socket JSON API; the
  control loop every 0.2 s; the state directory `$GLUON_KUBE_STATE` or
  `/tmp/gluon-kube-<uid>` (conductor/tracks/runtime_20261118/spec.md "Design")
- pyroute2 0.9.7 is a runtime dependency (the node's netlink)
- The supervisor (`python -m gluon_kube.supervisor`) is the node's parent outside its
  namespaces: it starts the node and relays its egress (GK-5.3)
- Coverage follows subprocesses (coverage's `patch = ["subprocess"]`): the node, the
  supervisor and the netlink helper are measured by the integration tests

## Sandbox
A tool added here that the sandbox doesn't already have is added to gluon
`bin/sandbox-setup` in the same change.

## Layout *(2026-11-15, GK-2)*
- `src/gluon_kube/` — the simulator: `cli` (the command); `model`, `manifests`, `report`
  (reading manifests, GK-3.1/3.2/3.7); `values`, `template`, `chart` (Helm's values,
  the Go-template subset, `helm template`'s output, GK-3.3–3.6) *(2026-11-18)*
- `tests/fixtures/helm-template/` — the laptop's `helm template` captures (GK-3.5);
  `laptop-reference/` — the laptop's image and cluster captures, read-only reference
  for GK-4/GK-5 *(2026-11-18)*
- `tests/unit/`, `tests/integration/`
- `scripts/ci.sh`
- `spikes/` — probes that decide a design; kept with their findings, not
  part of the package, not run by CI
