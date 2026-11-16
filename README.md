# gluon-kube

A simulated gluon-local for the sandbox, per gluon
[ADR 0030](../../docs/adr/0030-gluon-kube-sandbox-kubernetes-runtime.md): it
reads gluon-local's unchanged manifests, Helm chart and values files, and runs
each workload as a local process inside unprivileged namespaces, so the Rust
ports can be run and checked at the cluster level where there is no Docker and
no Kubernetes. It is a simulator, not Kubernetes: what it copies and what it
leaves out are in the ADR and gluon
[`docs/gluon-kube-findings.md`](../../docs/gluon-kube-findings.md).

**Status (2026-11-15):** GK-2, the package skeleton. The design is being proven
by the feasibility probe in [`spikes/netns-probe/`](./spikes/netns-probe)
(GK-1); the simulator itself starts in GK-3. Work is planned in
[`conductor/tracks.md`](./conductor/tracks.md) and sequenced in gluon
[`plans/gluon-kube/plan.md`](../../plans/gluon-kube/plan.md). Progress,
feature by feature: gluon `bin/verify-complete-kube-spike.sh`.

## Build and check

```
uv sync                      # the pinned Python and the locked dependencies
uv run gluon-kube --version
scripts/ci.sh                # what CI runs: format, lint, types, tests, 80% coverage
uv run spikes/netns-probe/probe.py   # the GK-1 probe (Linux, unprivileged namespaces)
```

Linux only: it needs unprivileged user namespaces and `bwrap`, which gluon
`bin/sandbox-setup` checks.

## Layout
- `src/gluon_kube/` — the simulator (`gluon-kube` command)
- `tests/unit/`, `tests/integration/` — run by `scripts/ci.sh`
- `spikes/` — probes that decide a design, kept with their findings; not part
  of the package, not run by CI
- `conductor/` — product, tech stack, workflow and tracks
