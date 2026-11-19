# Spec — GK-4 the runtime (runtime_20261118)

Source: gluon ADR 0030 decisions 3 to 5; gluon `plans/gluon-kube/plan.md` GK-4.1–4.9.
*2026-11-18, owner: one shot: no per-task plan-mode review or commit confirmation.*

## Design (GK-4.1, decided 2026-11-18)
- **The node** is `python -m gluon_kube.node STATE` in its own user and network
  namespace (bwrap, uid 0 with every capability inside; GK-1.1), started detached by
  `gluon-kube cluster start`. It holds the desired state (applied objects) and the
  observed state (pods, Services) **in memory**, and reconciles them every 0.2 s
  (`controller.Cluster.tick`). The CLI reaches it over a **Unix socket** in the state
  directory (`node.sock`): one JSON request and answer per connection. Killing the
  node ends everything (`--die-with-parent`); nothing persists across a restart, as
  nothing persists across a sandbox teardown.
- **The state directory** is `$GLUON_KUBE_STATE`, else `/tmp/gluon-kube-<uid>`: gone
  with the sandbox and never under the workspace, so never exported. `cluster stop`
  removes it.
- **A pod** is a sandbox (`sleep infinity` in new network, UTS, IPC and pid namespaces:
  Kubernetes' pause container) with its IP (veth onto the node's bridge, GK-1.2) and
  hostname. **Each container** joins the sandbox's network, UTS and IPC namespaces
  (nsenter) and gets its own mount and pid namespace (nested bwrap): a root that is the
  host's, read-only, with the image's files, the pod's volumes and the kubelet's
  /etc/hosts, /etc/hostname and /etc/resolv.conf on top; a directory the host lacks a
  mount point in is overlaid (tmpfs, host entries bound back; GK-1.6's finding); the
  image's user in a user namespace of its own; `--clearenv` and the env Kubernetes
  gives; the command as pid 1 (`--as-pid-1`), so SIGTERM reaches it. A container
  restart is a new bwrap in the same sandbox: same IP, fresh writable layer, volumes
  kept.
- **Images run their own entrypoints** where captured (`laptop-reference/images/`,
  copied unchanged into `src/gluon_kube/images/`), on the sandbox's binaries; each
  recipe lists what in it isn't the image's. Images without a recipe are
  `ImageUnavailable` (a reason of gluon-kube's own, where Kubernetes says
  `ErrImageNeverPull`).
- **exec and run** enter a container from outside in two steps (the sandbox's
  namespaces as the node's root, then the container's mount, pid and user
  namespaces), with the container's env and working directory (`env -i -C`). `run` is
  a throwaway pod of the sandbox itself (`gluon-kube/host`) as the sandbox user.
- **What a pod sees (GK-4.9)**: HOSTNAME and the hosts-file line for itself, as
  OrbStack's kubelet writes them; the `KUBERNETES_SERVICE_*`/`KUBERNETES_PORT*` env
  (10.96.0.1:443, no API server behind it), which the kubelet sets even with
  `enableServiceLinks: false`; Services by name in /etc/hosts (short,
  `.gluon-local`, `.svc`, `.svc.cluster.local`) in place of cluster DNS.

## Acceptance (gluon `bin/verify-complete-GK-4.sh`)
1. `scripts/ci.sh` green (coverage ≥ 80%)
2. `cluster start`; `apply -f infra/k8s/local-infra/postgres-user.yaml`; the pod Ready
3. `gluon-kube run -- psql -h user-postgres -U user user` works (wal_level logical)
4. killing Postgres restarts it: RESTARTS 1, Ready again, `logs --previous`
5. an image with no recipe shows `ImageUnavailable`, its Service no endpoints
6. `exec deploy/user-postgres`: the image's user (70), HOSTNAME, the kubelet's hosts
   file, `KUBERNETES_SERVICE_HOST`
7. `cluster stop` leaves no process and no state directory
8. `bin/verify-complete-kube-spike.sh`: GK-4 PASS, nothing FAIL
9. the track archived; tools/gluon-kube has nothing uncommitted
