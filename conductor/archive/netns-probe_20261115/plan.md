# Plan — GK-1 feasibility probe (netns-probe_20261115)

Each task: written check seen failing, then the setup that passes it, then a
commit (after the owner confirms). Each phase ends with the owner's review.

## Phase 0: Set up [checkpoint: 6936dbf]
- [x] Task: `git init` `tools/gluon-kube` (branch `main`, sandbox identity);
      commit the conductor setup and this track `2a8b5d3`
- [x] Task: Write gluon `bin/verify-complete-GK-1.sh` from the spec's
      acceptance list; run it and see it red for the stated reasons
      *(gluon file, no sandbox git: red 5/6 on 2026-11-15; failure paths proven
      on scratch copies)*
- [x] Task: Probe skeleton: `spikes/netns-probe/probe.py` (PEP 723 header,
      check runner with a deadline per check, `ok`/`FAIL` lines, summary,
      exit code, work directory cleaned up); every check a stub that FAILs
      "not written" `a6f8af2`
- [x] Task: Conductor - User Manual Verification 'Set up' (Protocol in workflow.md)

## Phase 1: Namespaces and pod networking [checkpoint: ec6e006]
- [x] Task: GK-1.1 node namespace `2e03354`
- [x] Task: GK-1.2 pod networking without `ip` (decide `pyroute2` or
      `iproute2`; record the choice in `tech-stack.md`) `2a3dc47` — pyroute2
- [x] Task: GK-1.3 same port, two pods `e575f25`
- [x] Task: GK-1.4 Service proxy, and refusal with no endpoint `28179ae`
- [x] Task: Conductor - User Manual Verification 'Namespaces and pod networking' (Protocol in workflow.md)

## Phase 2: Names, mounts and getting in [checkpoint: 7c294e7]
- [x] Task: GK-1.5 names for glibc, Rust and Java; in-place rewrite `069d3a2`
- [x] Task: GK-1.6 per-pod mounts `069d3a2`
- [x] Task: GK-1.7 `nsenter` and the port-forward `6d6195a`
- [x] Task: Conductor - User Manual Verification 'Names, mounts and getting in' (Protocol in workflow.md)

## Phase 3: A real workload and teardown [checkpoint: 424a4e8]
- [x] Task: GK-1.8 PostgreSQL in a pod, reached by name `559d393`
- [x] Task: GK-1.9 teardown leaves nothing `2ac81f8`
- [x] Task: Conductor - User Manual Verification 'A real workload and teardown' (Protocol in workflow.md)

## Phase 4: Outcome [checkpoint: ed9169a]
- [x] Task: `FINDINGS.md`: each result with its evidence, versions, and
      what it means for the design `440343c`
- [x] Task: GK-1.10 ADR 0030 decision 3 (and "Alternatives considered")
      rewritten; shown to the owner before it's written; `plans/gluon-kube/plan.md`
      and `tech-stack.md` updated *(gluon files; tech-stack `12caa64`)*
- [x] Task: `bin/verify-complete-GK-1.sh` all PASS; archive the track
- [x] Task: Conductor - User Manual Verification 'Outcome' (Protocol in workflow.md)
