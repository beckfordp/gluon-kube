# Plan — `up` waits for the connectors of deployed services only

## Phase 1
- [x] Task: failing tests: connectors of services not deployed aren't waited for `2ed6d96` (red: import)
- [x] Task: `up` filters by the cluster's Deployments, naming the rest `2ed6d96`; CI (Linux arm64) 292 passed, 5 skipped
- [x] Task: CI green; the amd64 bed's cart-service up (gluon B7): `bin/sandbox-bed up --arch amd64 cart-service` from a fresh login shell, Swagger UI 200, the four other connectors named, not waited for

## Review
- /conductor:review (prompting off): spec met; one pure function, unit-tested; CI green
  (Linux arm64, 292 passed, 5 skipped); gluon's bin/lib/connectors.sh changed alike. No
  findings.
