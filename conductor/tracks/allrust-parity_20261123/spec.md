# Spec — all-Rust parity with bin/k8s-local-up (allrust-parity_20261123)

gluon all-Rust Stage B, track B4. `gluon-kube up` with no arguments must stand up what
`ALL_RUST=1 bin/k8s-local-up` stands up on OrbStack (Stage A, 53/53): all eight Rust ports
and the shared infra, ready for gluon's acceptance suite.

## Requirements
1. **Redis** (`redis:7-alpine`, cart/catalog/order): a recipe from the captured image
   (config, entrypoint, accounts), run on the sandbox's `redis-server` (bin/sandbox-setup's
   `redis` step). Differences listed: starts as redis (uid 999), not root dropping with
   setpriv; `--protected-mode no`, which the image's build compiles in; Redis 7.0
   (Ubuntu's), not 7.4.
2. **Implementation per service**: `<SERVICE>_IMPL` (scala|rust) for every service, as
   bin/k8s-local-up; `ALL_RUST=1` accepted. Default rust for every service here (the
   Scala services can't be built in the sandbox, ADR 0028); `=scala` keeps today's
   Scala path (chart installed, pod ImageUnavailable). Every choice and every chosen
   port/values file checked before anything is built.
3. **WORKDIR**: a Rust port's image WORKDIR exists in its container (Docker creates it;
   `/app` with nothing copied into it made the pod's chdir fail).
4. **Waits for infra**: every infra Deployment with a runnable image has a Ready pod
   before services deploy (a Deployment whose pod doesn't exist yet is waited for).
5. **Switch reset**: any service, from its `<short>-postgres` with gluon's
   `switch-reset.sql` (Stage A renamed `user-switch-reset.sql`), then its outbox
   connector restarted if it has one.
6. **Connectors after migrations**: once every service rolled out, the registration Job
   re-run and every outbox connector RUNNING (FAILED tasks restarted, 409 retried), as
   bin/k8s-local-up and gluon `bin/lib/connectors.sh` do; up fails if one isn't.

## Done when
CI green (`scripts/ci.sh`, coverage gate); in the arm64 sandbox bed `gkube up` brings up
all eight Rust ports with every pod Ready and every connector RUNNING (gluon
`plans/rust-port/all-rust/stage-b.md`).
