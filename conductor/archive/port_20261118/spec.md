# Spec — GK-6 a Rust port from its chart and values file (port_20261118)

Source: gluon ADR 0030 decisions 4 and 5; gluon `plans/gluon-kube/plan.md` GK-6.1–6.5.
*2026-11-18, owner: one shot.*

## Design
- **GK-6.2 `<port>-rs:latest`** is read from `services/<port>-rs/Dockerfile`
  (`dockerfile.py`): the final stage's ENTRYPOINT, CMD, ENV, WORKDIR and USER (for
  user-service-rs equal to the laptop's `docker image inspect`), and what it copies
  from earlier stages: the build stage's `/src/<x>` is gluon's `<x>` (the Dockerfile
  lays the repos out as gluon does), and a stage that renders the AsyncAPI HTML is
  `scripts/asyncapi-html.sh`'s output (`target/asyncapi-docs`). Not built: the image
  is `ImageUnavailable`, saying `gluon-kube build <port>-rs`. Runs as 65532:65532 on the
  sandbox's root with debian:bookworm-slim's /etc/passwd and /etc/group (captured).
- **`gluon-kube build <port>-rs`** runs the Dockerfile's `cargo` commands in the repo,
  and `scripts/asyncapi-html.sh` for a stage that renders the HTML, in place of
  `docker build`.
- **GK-6.1 `helm upgrade --install`**: the chart rendered (GK-3) and applied under the
  release; helm's summary (installing or upgraded, revision).
- **GK-6.3 `rollout restart` and `rollout status`** (GK-4's controller: a new template
  stamp; surge, the old pod terminated once the new is Ready).
- **GK-6.4 `gluon-kube up [service ...]`**: bin/k8s-local-up's steps with its headings;
  its waits only for pods that can run (Redis and Apicurio never become Ready here);
  a missing registration Job warns at once, as `kubectl wait` fails; `registry-reload
  --configmap-only` skipped (Apicurio has no recipe). `USER_SERVICE_IMPL` honoured,
  **default `rust`** (the Scala services can't be built in the sandbox; a Scala
  service's chart installs and its pod shows `ImageUnavailable`).
- **GK-6.5** the switch: the user schema reset (`user-switch-reset.sql`) when the
  deployed image changes, and the user outbox connector restarted after the rollout.
- **exec and logs through the node** (found while planning GK-7): a pod can't nsenter
  into another, so `exec` is streamed by the node, as kubectl's by the API server
  (stdin, stdout, stderr frames, the exit code), and `logs` is served by it; `run`
  pods get the node's socket and `GLUON_KUBE_STATE`, so `gluon-kube` works inside.

## Finding (a race)
In a fresh cluster, bin/k8s-local-up's order (local-infra and the registration Job,
then each service) registers `user-outbox-connector` before user-service-rs has
created `outbox_events`. Whether its task fails depends on when Connect first starts
it: before the service's migration, it FAILS ("No table filters found for filtered
publication") and stays FAILED, and no sign-up reaches `user-events` until the
connector is restarted; after it, it runs. Both were seen here (2026-11-18: FAILED
in the manual run and with no service deployed; RUNNING in one `up`). The script
restarts the connector only on an implementation switch. A fresh OrbStack cluster has
the same race (same Debezium, same order; Scala's slower start makes losing it more
likely): a question for the laptop (gluon plan GK-8.2). `gluon-kube up` reproduces the
script as it is.

## Acceptance (gluon `bin/verify-complete-GK-6.sh`)
1. `scripts/ci.sh` green (coverage ≥ 80%)
2. `gluon-kube up user-service` in a fresh cluster exits 0; user-service Ready, running
   user-service-rs as 65532
3. the finding: its connector's task RUNNING after `up`, or FAILED (the race) and
   RUNNING after a restart
4. a sign-up through `gluon-kube run -- curl` gives `UserRegistered` on `user-events`
   (read with `gluon-kube exec deploy/kafka -- …kafka-console-consumer.sh`)
5. `rollout restart` and `rollout status`: a new pod, the old one gone, still answering
6. the switch: `USER_SERVICE_IMPL=scala up` shows `ImageUnavailable`; `rust` again
   resets the schema and restarts the connector; a sign-up after it is published
7. `cluster stop` leaves nothing
8. `bin/verify-complete-kube-spike.sh`: GK-6 PASS, nothing FAIL
9. the track archived; tools/gluon-kube has nothing uncommitted
