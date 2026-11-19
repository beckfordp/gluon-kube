# Plan — GK-3 reading the manifests (manifests_20261115)

## Phase 1: Model and loader
- [x] Task: Write gluon `bin/verify-complete-GK-3.sh`; see it red *(gluon file; red 6/7 — ci.sh green already; criterion 5 first passed on the plan's own wording, now needs "GK-3.5 status: waiting")*
- [x] Task: GK-3.1/3.2 the model and a field-accounting reader (tests first)
- [x] Task: GK-3.2 the loader: files, directories, multi-document YAML, errors (same module)
- [x] Task: GK-3.7 `gluon-kube apply --dry-run` and its report
- [x] Task: Conductor - User Manual Verification 'Model and loader' (Protocol in workflow.md)

## Phase 2: Values, renderer and chart
*2026-11-18, owner: one shot, no per-task plan-mode review or commit confirmation; the captures are in.*
- [x] Task: GK-3.3 values merging `cf50506`
- [x] Task: GK-3.4 the template renderer (the chart's subset; everything else refused) `65e1428`
- [x] Task: GK-3.4/3.6 the chart and `gluon-kube template` `3202b9a`
- [x] Task: GK-3.5/3.6 the capture comparison test `3202b9a` *(all ten captures match as parsed documents; two byte-identical)*
- [x] Task: gluon: the kube-spike report's GK-3 line; plan, findings, tech stack
- [x] Task: Conductor - User Manual Verification 'Values, renderer and chart' *(2026-11-18, owner's one-shot: gluon `bin/verify-complete-GK-3.sh` is the verification; no separate phase script)*
