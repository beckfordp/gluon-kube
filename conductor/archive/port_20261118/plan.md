# Plan — GK-6 a Rust port from its chart and values file (port_20261118)

*2026-11-18, owner: one shot. Built, then written down; archived with the work.*

## Phase 1: The port
- [x] Task: GK-6.1 `helm upgrade --install` `cfbe646`
- [x] Task: GK-6.2 the `<port>-rs` recipe from its Dockerfile; `gluon-kube build` `cfbe646`
- [x] Task: GK-6.3 `rollout restart` and `rollout status` (GK-4) `af34f94`
- [x] Task: GK-6.4 `gluon-kube up` `cfbe646`
- [x] Task: GK-6.5 the switch reset and connector restart `cfbe646`
- [x] Task: exec and logs through the node; the node's socket in run pods `cfbe646`
- [x] Task: gluon `bin/verify-complete-GK-6.sh`; the kube-spike report's GK-6 line
