# Spec — GK-1 feasibility probe (netns-probe_20261115)

Source: gluon ADR 0030 decision 3 and decision 8 (increment 1); gluon
`plans/gluon-kube/plan.md` GK-1.1–GK-1.10.

## Why
ADR 0030 decision 3 put every workload in one network namespace, each Service
on its own loopback address. That can't work: the Rust ports bind
`0.0.0.0:8080`/`0.0.0.0:9090` (`server.rs`), the Postgres image listens on
`*`, and the broker's `KAFKA_LISTENERS` is `PLAINTEXT://:9092`, so two
workloads on one port collide. The replacement is Kubernetes' own shape:
a network namespace per pod with its own IP, a node namespace that routes
between them, Services as ClusterIPs served by a userspace proxy, a per-pod
`/etc/hosts`, and a mount namespace per pod. This track finds out, before any
gluon-kube code is written, whether all of that works unprivileged here.

## What is built
A probe, not product code: `spikes/netns-probe/probe.py`, a single-file uv
script (PEP 723 inline metadata, dependencies pinned), standard library plus
at most one netlink library. Each check is a function that sets up what it
needs, prints `ok` or `FAIL` with the evidence (command, output), and tears
down. The script carries on after a failure and exits non-zero if any check
failed. `spikes/netns-probe/FINDINGS.md` records each result and what it
means for the design.

Addresses used (Kubernetes' defaults, so the design reads like a cluster):
pods `10.244.0.0/24` on a node bridge at `10.244.0.1`; Services
`10.96.0.0/12`.

## Checks and their pass criteria
| Id | Check | Passes when |
|---|---|---|
| GK-1.1 | Node namespace | Inside `bwrap --unshare-user --unshare-net` (or `unshare -Urn`): `CAP_NET_ADMIN` in the effective set, `lo` up, none of the host's interfaces visible |
| GK-1.2 | Pod networking without `ip` | From the node: a pod network namespace, a veth pair with one end moved into it, pod IP and default route set, node bridge up; a TCP connection pod → node succeeds. `pyroute2` first; `iproute2` from the Ubuntu archive if it can't |
| GK-1.3 | Same port, two pods | Pods A and B both listen on `0.0.0.0:8080`; from pod C, each pod IP answers with its own name |
| GK-1.4 | Service proxy | A proxy in the node on `10.96.0.10:8080` forwards to pod A; from pod C the Service answers as A; with the endpoint removed, pod C's connect is refused (`ECONNREFUSED`), not hung |
| GK-1.5 | Names | A per-pod `/etc/hosts` maps `user-service` and `user-service.gluon-local.svc.cluster.local` to the ClusterIP; both resolve for glibc (`getent`, `curl`), Rust (`ToSocketAddrs`, a throwaway cargo program), and Java 21 (`InetAddress`, a single-file program); after the file is rewritten in place, the new address resolves in a running pod |
| GK-1.6 | Per-pod mounts | A host directory appears at `/connectors` and a file at `/jmx-config/metrics.yaml` in pod A only; pod B doesn't see them; the rest of the filesystem (binaries, `/usr`) is visible as on the host |
| GK-1.7 | Getting in from outside | From outside every namespace: `nsenter` into pod A's namespaces (unprivileged, `--preserve-credentials`) runs a command that sees pod A's `/etc/hosts` and reaches the Service by name; a port-forward (outside `127.0.0.1:<port>` ↔ Unix socket ↔ relay in the node) lets an outside `curl` reach the Service |
| GK-1.8 | A real workload | PostgreSQL 16 from the local binaries in pod A, set up like the `postgres:16-alpine` image (`POSTGRES_USER` superuser, password over TCP, `POSTGRES_DB`, `-c wal_level=logical`), listening on `*:5432`; from pod C, `psql -h user-postgres -U user user -c 'select 1'` answers with `PGPASSWORD=user` |
| GK-1.9 | Teardown | Killing the node process ends every pod process (none left with the probe's marker in `ps`), the port-forward's port is free, the probe's work directory is gone |

Also recorded: kernel, `bwrap`, Python and library versions.

## Outcome (GK-1.10)
- Every check works: ADR 0030 decision 3 and "Alternatives considered" are
  rewritten to the proven design.
- A check fails and can't be worked around within the track: the finding
  names the check and why; decision 3 is rewritten to the fallback (one
  namespace, ports rewritten into each workload's env) or the ADR is parked,
  as the owner decides.
- Either way, the revised text is shown to the owner and approved before it is
  committed, and `plans/gluon-kube/plan.md` is updated to match.

## Acceptance (agreed before the work; `bin/verify-complete-GK-1.sh` in gluon)
Red until the session is done; each criterion printed PASS or FAIL:
1. `spikes/netns-probe/probe.py` runs to completion (no crash, no hang; a
   deadline per check) and prints one result line for each of GK-1.1–1.9.
2. Each result equals the one recorded in `FINDINGS.md` (the probe reproduces
   its findings).
3. `FINDINGS.md` records, for each of GK-1.1–1.9, works or doesn't, with the
   evidence, and the versions.
4. ADR 0030's decision 3 names GK-1 and no longer says "one network
   namespace" unless the findings chose the fallback.
5. Nothing the probe started is still running, and no probe work directory is
   left in `/tmp`.
6. The track is archived; `tools/gluon-kube` has nothing uncommitted.
All PASS: runs `bin/sandbox-save`.

## Out of scope
- Any gluon-kube package code, `pyproject.toml`, CI (GK-2)
- Kafka and Connect in a pod (GK-5); only the Java name lookup is probed here
- Performance of the proxy

## Deviations from the workflow, for approval
- **TDD:** the probe's checks are the tests. Each check is written first and
  seen failing for the stated reason (nothing set up yet), then the setup code
  that makes it pass is written. There are no unit tests and no coverage gate:
  it is a spike, not part of the package.
- **Location:** ADR 0030 said "throwaway, outside any repo". Proposed instead:
  committed under `spikes/netns-probe/`, so the evidence is reviewable, saved
  by `bin/sandbox-save`, and becomes GK-4's first integration tests.
- **git:** this track needs `tools/gluon-kube` to be a git repo, so `git init`
  (sandbox identity) and the conductor setup commit come first; the rest of
  GK-2 stays in GK-2.
