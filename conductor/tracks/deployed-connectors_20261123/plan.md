# Plan — `up` waits for the connectors of deployed services only

## Phase 1
- [x] Task: failing tests: connectors of services not deployed aren't waited for `2ed6d96` (red: import)
- [x] Task: `up` filters by the cluster's Deployments, naming the rest `2ed6d96`; CI (Linux arm64) 292 passed, 5 skipped
- [ ] Task: CI green; the amd64 bed's cart-service up (gluon B7)
