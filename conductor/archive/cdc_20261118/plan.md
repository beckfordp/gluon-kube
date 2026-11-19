# Plan — GK-5 Kafka, Connect and the registration Job (cdc_20261118)

*2026-11-18, owner: one shot. Built, then written down; archived with the work.*

## Phase 1: Kafka, Connect, egress, Jobs
- [x] Task: check first: Debezium with no `outbox_events` (spec "Finding")
- [x] Task: GK-5.0 Kafka's launchers `9ac96c0`
- [x] Task: GK-5.1 the Kafka recipe `9ac96c0`
- [x] Task: GK-5.2 the Connect recipe `9ac96c0`
- [x] Task: GK-5.3 init containers; the curl recipe; egress (proxy, supervisor) `9ac96c0`
- [x] Task: GK-5.4 Jobs; `create job --from=cronjob` `9ac96c0`
- [x] Task: GK-5.5 local-infra end to end (`tests/integration/test_cdc.py`) `9ac96c0`
- [x] Task: gluon `bin/verify-complete-GK-5.sh`; the kube-spike report's GK-5 line
