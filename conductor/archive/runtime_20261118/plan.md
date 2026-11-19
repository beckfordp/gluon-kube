# Plan — GK-4 the runtime (runtime_20261118)

*2026-11-18, owner: one shot. Built first, then this track written down with the
decisions made; the acceptance script written before the track was closed.*

## Phase 1: The cluster
- [x] Task: GK-4.1 design (spec.md "Design")
- [x] Task: GK-4.2 `cluster start|stop|status`: node, network, control socket `af34f94`
- [x] Task: GK-4.3 Services: ClusterIPs, proxy, endpoints, hosts files `af34f94`
- [x] Task: GK-4.4 pods: sandbox, containers, mounts, env, args, logs `af34f94`
- [x] Task: GK-4.5 probes: httpGet, tcpSocket, exec; startup, liveness, readiness `af34f94`
- [x] Task: GK-4.6 restarts, backoff, `get pods` `af34f94`
- [x] Task: GK-4.7 the Postgres recipe (the image's own entrypoint) `af34f94`
- [x] Task: GK-4.8 `exec` and `run` `af34f94`
- [x] Task: GK-4.9 what a pod sees `af34f94`
- [x] Task: GK-1's probe as integration tests (`tests/integration/test_cluster.py`) `af34f94`

## Phase 2: Acceptance
- [x] Task: gluon `bin/verify-complete-GK-4.sh`; the kube-spike report's GK-4 line
- [x] Task: archive
