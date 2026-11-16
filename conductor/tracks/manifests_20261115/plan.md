# Plan — GK-3 reading the manifests (manifests_20261115)

## Phase 1: Model and loader
- [x] Task: Write gluon `bin/verify-complete-GK-3.sh`; see it red *(gluon file; red 6/7 — ci.sh green already; criterion 5 first passed on the plan's own wording, now needs "GK-3.5 status: waiting")*
- [x] Task: GK-3.1/3.2 the model and a field-accounting reader (tests first)
- [x] Task: GK-3.2 the loader: files, directories, multi-document YAML, errors (same module)
- [x] Task: GK-3.7 `gluon-kube apply --dry-run` and its report
- [x] Task: Conductor - User Manual Verification 'Model and loader' (Protocol in workflow.md)

## Phase 2: Values, renderer and chart
- [ ] Task: GK-3.3 values merging
- [ ] Task: GK-3.4 the template renderer (the chart's subset; everything else refused)
- [ ] Task: GK-3.4/3.6 the chart and `gluon-kube template`; hand-derived expectations
- [ ] Task: GK-3.5/3.6 the capture comparison test (skips until the capture arrives)
- [ ] Task: gluon: the kube-spike report's GK-3 line; plan, findings, tech stack
- [ ] Task: Conductor - User Manual Verification 'Values, renderer and chart' (Protocol in workflow.md)
