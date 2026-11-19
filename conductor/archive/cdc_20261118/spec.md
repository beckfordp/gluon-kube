# Spec — GK-5 Kafka, Connect and the registration Job (cdc_20261118)

Source: gluon ADR 0030 decisions 4 and 5; gluon `plans/gluon-kube/plan.md` GK-5.0–5.5.
*2026-11-18, owner: one shot.*

## Design
- **GK-5.0 launchers.** Kafka's `bin/` scripts ship only in its binary distribution,
  which the sandbox can't reach, and the laptop capture has no `bin/`. gluon-kube's
  own `kafka-run-class.sh` (the real one's variables and defaults, `<base>/libs/*`),
  `kafka-server-start.sh`, `connect-distributed.sh` and the tools gluon-local calls
  (`kafka-topics`, `kafka-console-consumer`/`-producer`, `kafka-consumer-groups`,
  `kafka-broker-api-versions`, `kafka-storage`) serve `/opt/kafka/bin` and `/kafka/bin`.
- **GK-5.1 `apache/kafka:3.8.0`** runs the image's own `/etc/kafka/docker/run`
  (configure, launch: `KafkaDockerWrapper` turns `KAFKA_*` into server.properties and
  formats storage with `CLUSTER_ID`) and `/opt/kafka/config` (a writable copy), as
  `appuser`, on the Maven Central jars and the sandbox's Java 21 at
  `/opt/java/openjdk`. `/__cacert_entrypoint.sh` is a stand-in that runs its arguments.
- **GK-5.2 `debezium/connect:3.0.0.Final`** runs the image's own
  `/docker-entrypoint.sh` as `kafka` on the Connect runtime jars (`/kafka/libs`) and the
  Debezium Postgres plugin (`/kafka/connect`), with Kafka 3.8.0's `config/` as
  `/kafka/config.orig`, an `ip addr` stand-in (for its HOST_NAME) and
  `/etc/redhat-release` (it picks `cp` flags by it).
- **GK-5.3 init containers and egress.** Init containers were built in GK-4 (in order,
  each to completion, by restartPolicy). `curlimages/curl` runs the sandbox's curl as
  `curl_user` with the CA bundle at `/cacert.pem`. Pods have no route out (no NAT
  without iptables), so images that fetch (`Recipe.egress`: curl) get
  `HTTPS_PROXY`/`HTTP_PROXY` for the node's proxy (`10.244.0.1:3128`): cluster names
  and addresses are connected from the node, anything else is relayed by the
  **supervisor**, the node's new parent outside its namespaces, over
  `STATE/egress.sock`.
- **GK-5.4 Jobs** (GK-4's controller: backoffLimit counting failed pods and OnFailure
  restarts; `wait --for=condition=complete`). CronJobs by hand:
  `create job NAME --from=cronjob/NAME`.
- **Finding (GK-5 "check first").** Before a service has created `outbox_events`,
  Debezium can't create its filtered publication ("No table filters found for
  filtered publication dbz_publication"): the connector is RUNNING and its task
  FAILED, until the table exists and the connector restarts (bin/k8s-local-up's
  restart after a switch).

## Acceptance (gluon `bin/verify-complete-GK-5.sh`)
1. `scripts/ci.sh` green (coverage ≥ 80%)
2. `apply -f infra/k8s/local-infra/`: every Postgres, Kafka and Connect Running;
   Redis and Apicurio `ImageUnavailable`
3. the registration Job completes; the init container fetched the JMX exporter
4. `user-outbox-connector`: its task FAILED before `outbox_events` exists, RUNNING
   once the table exists and the connector restarted
5. an outbox row reaches `user-events`, read with
   `gluon-kube exec deploy/kafka -- /opt/kafka/bin/kafka-console-consumer.sh`
6. `create job --from=cronjob/user-outbox-cleanup` completes
7. `cluster stop` leaves nothing
8. `bin/verify-complete-kube-spike.sh`: GK-5 PASS, nothing FAIL
9. the track archived; tools/gluon-kube has nothing uncommitted
