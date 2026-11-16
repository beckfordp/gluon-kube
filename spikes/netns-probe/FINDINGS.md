# GK-1 findings: the feasibility probe (netns-probe_20261115)

Gluon ADR 0030 decision 3 asked whether the sandbox can give each pod its own network
and mount namespace, route between pods through a node, serve Services from a userspace
proxy and resolve their names from a per-pod `/etc/hosts`, all unprivileged. It can:
every check works. `probe.py` reproduces these results (gluon
`bin/verify-complete-GK-1.sh` compares them); each was first seen failing, then a
deliberately broken copy failed for the stated reason.

Outcome: proven

## Results

| Check | What | Result |
|---|---|---|
| GK-1.1 | node namespace: CAP_NET_ADMIN, lo up, no host interfaces | works |
| GK-1.2 | pod networking without ip: veth, pod IP, route, pod to node | works |
| GK-1.3 | same port, two pods: each pod IP answers with its own name | works |
| GK-1.4 | Service proxy: ClusterIP to a pod; refused with no endpoint | works |
| GK-1.5 | names: per-pod /etc/hosts for glibc, Rust and Java; rewrite seen | works |
| GK-1.6 | per-pod mounts at absolute paths, seen by that pod only | works |
| GK-1.7 | getting in: nsenter into a pod; port-forward from outside | works |
| GK-1.8 | a real workload: PostgreSQL in a pod, psql by Service name | works |
| GK-1.9 | teardown: no process, port or work directory left | works |

## Versions

| Tool | Version |
|---|---|
| Kernel | 6.18.44-fc-v310 |
| bwrap | bubblewrap 0.9.0 |
| Python | 3.13.15 |
| pyroute2 | 0.9.7 |
| PostgreSQL | 16.15 (Ubuntu build) |
| Java | 21 (`/usr/bin/java`) |
| rustc | 1.99.1 |

## What it means for gluon-kube

The design ADR 0030 decision 3 now records: Kubernetes' own shape. A node namespace
that owns the network; a nested `bwrap` per pod with its own network, mount and pid
namespace (and user namespace for an image's USER), its own IP on `10.244.0.0/24`
through a veth pair onto the node's bridge, its own root and `/tmp`; Services as
ClusterIPs on `10.96.0.0/12` served by the node's userspace proxy, forwarding to ready
endpoints only; names from a per-pod `/etc/hosts` rewritten in place; `exec` and `run`
through `nsenter`; port-forward through a Unix socket; teardown by killing the node.

Carried into GK-4: rewrite hosts files in place; tmpfs parents for mount targets the
host lacks; an image's USER needs its passwd entry; pods need their own pid namespace
and `/tmp`; no `fork()` in the threaded node; netlink helpers take ~1.7 s to import
pyroute2 on a busy node (time limits to match).

## Evidence (from one full run, 2026-11-15; Postgres log lines and long command lines trimmed)

### GK-1.1 node namespace: CAP_NET_ADMIN, lo up, no host interfaces

An unprivileged `bwrap --unshare-user --unshare-net` gives uid 0 with every capability in a network namespace of its own: the node. The host's interfaces (`ifb0`, `ifb1`, `eth0`) are hidden.

```
host interfaces: lo ifb0 ifb1 eth0
node:
CapEff:	000001ffffffffff
flags 0x9
ifaces lo
```

### GK-1.2 pod networking without ip: veth, pod IP, route, pod to node

Inside the node, pyroute2 (pure Python, PyPI) creates a bridge and a veth pair, moves one end into a pod by pid and configures it from a helper that joins the pod's namespace (`os.setns`). No `ip`, no root. The helper is a child process, not a fork: the node runs threads.

```
node accepted a connection from 10.244.0.2
pod a:
interfaces lo eth0
pod got: hello from node
```

### GK-1.3 same port, two pods: each pod IP answers with its own name

Pods have their own network namespaces, so two can both bind `0.0.0.0:8080`, as the Rust ports, Postgres and Kafka do. This is what ADR 0030's first decision 3 (one namespace, a loopback address per Service) couldn't give.

```
pod a (10.244.0.2):
a listening on 0.0.0.0:8080

pod b (10.244.0.3):
b listening on 0.0.0.0:8080

pod c (10.244.0.4):
10.244.0.2:8080 answered a
10.244.0.3:8080 answered b
```

### GK-1.4 Service proxy: ClusterIP to a pod; refused with no endpoint

A Service is a ClusterIP on the node's `lo` (this kernel has no `dummy` link type: EOPNOTSUPP) served by a userspace proxy, kube-proxy's original mode; there's no `iptables`. With no endpoint it stops listening (`shutdown()`, not just `close()`: a listener blocked in `accept()` keeps listening otherwise), so callers are refused at once, as with a Kubernetes Service with no ready pods.

```
pod c (10.244.0.4):
with an endpoint: 10.96.0.10:8080 answered a
without one: refused after 0.000 s
```

### GK-1.5 names: per-pod /etc/hosts for glibc, Rust and Java; rewrite seen

A per-pod `/etc/hosts` (short name and `<name>.gluon-local.svc.cluster.local`) is enough for glibc (getent, curl), Rust (`ToSocketAddrs`) and Java 21 (`InetAddress`, as Kafka and Connect use). A running pod sees the file rewritten in place; a file replaced by rename is NOT seen (checked by mutation), so gluon-kube must rewrite in place.

```
pod c's /etc/hosts, first:
127.0.0.1 localhost
10.96.0.10 user-service user-service.gluon-local.svc.cluster.local
pod c (10.244.0.4):
glibc: 10.96.0.10      user-service user-service.gluon-local.svc.cluster.local
glibc fqdn: 10.96.0.10      user-service user-service.gluon-local.svc.cluster.local
curl: a
rust: a from 10.96.0.10:8080
java: a from user-service/10.96.0.10
java fqdn: a from user-service.gluon-local.svc.cluster.local/10.96.0.10
after rewrite glibc: 10.96.0.11      user-service user-service.gluon-local.svc.cluster.local
after rewrite curl: b
```

### GK-1.6 per-pod mounts at absolute paths, seen by that pod only

A pod's root is a tmpfs with the host's top-level entries bound in: a mount point the host's `/` lacks (`/connectors`, `/jmx-config`) can't be created in a directory bound from the host. ConfigMap-like mounts are read-only and seen by that pod only. A target under an existing host directory that lacks it (`/var/lib/postgresql/data`) needs a tmpfs at the parent first (GK-1.8).

```
pod a (mounts):
connectors: user-outbox-connector.json
jmx-config: rules: []
usr: /usr/bin/python3
workspace: /workspace
write: touch: cannot touch '/connectors/written': Read-only file system
pod b (none):
connectors: ls: cannot access '/connectors': No such file or directory
jmx-config: cat: /jmx-config/metrics.yaml: No such file or directory
usr: /usr/bin/python3
workspace: /workspace
write: touch: cannot touch '/connectors/written': No such file or directory
```

### GK-1.7 getting in: nsenter into a pod; port-forward from outside

From outside, unprivileged: `nsenter` into a pod's user, network, mount and pid namespaces (the pid namespace too, as `kubectl exec`: the pod's `/proc` is its own) sees its `/etc/hosts` and reaches Services by name; a port-forward from `127.0.0.1` through a Unix socket (path sockets cross network namespaces) to a relay in the node reaches a Service.

```
$ nsenter … cat /etc/hosts  (exit 0)
127.0.0.1 localhost
10.96.0.10 user-service user-service.gluon-local.svc.cluster.local
$ nsenter … getent hosts user-service  (exit 0)
10.96.0.10      user-service user-service.gluon-local.svc.cluster.local
$ nsenter … curl -s --http0.9 --max-time 5 http://user-service:8080/  (exit 0)
a
$ nsenter … sh -c echo interfaces $(tail -n +3 /proc/net/dev | cut -d: -f1)  (exit 0)
interfaces lo eth0
$ curl -s --http0.9 --max-time 5 http://127.0.0.1:40583/  (outside; exit 0)
port-forward answered: a
port 40583 free after close: True
node: pod a 10.244.0.2, pod c 10.244.0.4 (pid 23949), relay /tmp/gk1-probe.ztek_v23/check-7/relay.sock
```

### GK-1.8 a real workload: PostgreSQL in a pod, psql by Service name

PostgreSQL 16 from the local binaries, set up as `postgres:16-alpine` does (POSTGRES_USER/PASSWORD/DB, listen on all, scram over TCP, `-c wal_level=logical`), as uid 70 in a user namespace of its own with the image's `postgres` user in a per-pod passwd (initdb refuses an unknown uid), reached from another pod as `user-postgres:5432`; the manifest's readiness command works through `nsenter`. Found: `pg_ctl`'s daemonised server outlived a pod, so pods got their own pid namespace and `--die-with-parent`; and a private `/tmp`, as containers have (Postgres' default socket directory would collide across pods).

```
pod a (10.244.0.2, uid 70):
initialised /var/lib/postgresql/data as 70:70
2026-11-15 10:16:53.058 UTC [69] FATAL:  password authentication failed for user "user"
pod c (10.244.0.4):
psql: user|user|logical
wrong password: psql: error: connection to server at "user-postgres" (10.96.0.10), port 5432 failed: FATAL:...
readiness (exec pg_isready -U user in pod a): exit 0 /tmp:5432 - accepting connections
```

### GK-1.9 teardown: no process, port or work directory left

SIGKILL to the node ends every pod process, a `setsid` daemon included (the pid namespace), and releases the relay, the port-forward's port and the work directory. Without the pods' pid namespace the daemon survives (checked by mutation).

```
before: port-forward answered 'a'
before: 9 processes hold the work dir or the marker:
  24143: bwrap --unshare-user --unshare-net --uid 0 --gid 0 --cap-add ALL --dev-bind / / --proc /proc --die-w
  24144: /home/model/.cache/uv/environments-v2/probe-f6a4b7323dc09216/bin/python3 /workspace/gluon/tools/gluo
  24145: bwrap --unshare-net --unshare-pid --die-with-parent --symlink usr/bin /bin --bind /bin.usr-is-merged
  24146: bwrap --unshare-net --unshare-pid --die-with-parent --symlink usr/bin /bin --bind /bin.usr-is-merged
  24147: python3 /tmp/gk1-probe.ztek_v23/check-9/serve-a.py a 8080
  24183: bwrap --unshare-net --unshare-pid --die-with-parent --symlink usr/bin /bin --bind /bin.usr-is-merged
  24184: bwrap --unshare-net --unshare-pid --die-with-parent --symlink usr/bin /bin --bind /bin.usr-is-merged
  24185: sleep 300
  24224: sleep 31415926
SIGKILL to the node
after: 0 processes left
after: the relay refuses connections: True
after: port 43129 free: True
after: the work directory's contents removed: True
```
