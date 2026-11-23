# Spec — `up` waits for the connectors of deployed services only

## Overview
`gluon-kube up SERVICE` on a fresh cluster (gluon all-Rust Stage B, B7's amd64 smoke:
`bin/sandbox-bed up --arch amd64 cart-service`) fails: it waits for every registered
outbox connector to be RUNNING, but a service that isn't deployed has no outbox table, so
its connector can't run. gluon bin/k8s-local-up has the same wait (fixed alongside).

## Requirements
- Wait for `<short>-outbox-connector` only when Deployment `<short>-service` exists;
  connectors not named so are waited for as before.
- Name the connectors not waited for (a line, not a failure).

## Acceptance
- Unit tests red, then green; CI green in the Linux container.
- In the amd64 bed, `bin/sandbox-bed up --arch amd64 cart-service` reaches Swagger UI.
