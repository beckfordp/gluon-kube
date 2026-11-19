# gluon-kube

A simulated gluon-local for the sandbox, per gluon
[ADR 0030](../../docs/adr/0030-gluon-kube-sandbox-kubernetes-runtime.md): it
reads gluon-local's unchanged manifests, Helm chart and values files, and runs
each workload as a local process inside unprivileged namespaces, so the Rust
ports can be run and checked at the cluster level where there is no Docker and
no Kubernetes. It is a simulator, not Kubernetes: what it copies and what it
leaves out are in the ADR and gluon
[`docs/gluon-kube-findings.md`](../../docs/gluon-kube-findings.md).

**Sandbox only.** Every other environment runs the real thing: the laptop's gluon-local
is OrbStack's Kubernetes (gluon `bin/k8s-local-up`), and that pass stays the authority
for the cluster. In the sandbox it runs what can run there: the shared infra and the
services that can be built there (the Rust ports).

**Status (2026-11-18):** the simulator is built (gluon plan GK-3 to GK-7): `gluon-kube
up user-service` runs gluon-local's shared infra and user-service-rs from the unchanged
manifests, chart and values file, and the contract harness's cluster mode is green
inside it (gluon `bin/gluon-kube-contract`). Work is planned in
[`conductor/tracks.md`](./conductor/tracks.md) (the archive has each increment's design)
and sequenced in gluon [`plans/gluon-kube/plan.md`](../../plans/gluon-kube/plan.md).
Progress, feature by feature: gluon `bin/verify-complete-kube-spike.sh`.

## Quick start (in the sandbox, from gluon)

`gkube` is gluon's `bin/gkube`, on the PATH after `bin/sandbox-setup` (its `shell`
step). It passes everything to `gluon-kube`, runs as the repos' owner (so the root
desktop terminal works too) and makes gluon-kube's environment if it's missing.

```
gkube cluster start                    # the simulated cluster (state: /tmp/gluon-kube-<uid>)
gkube up user-service                  # shared infra, then user-service-rs built and deployed
                                       #   (~3 min; ~6 min after bin/sandbox-save, which
                                       #   removes the Rust build output)
gkube get pods                         # everything with a recipe Running; Redis and
                                       #   Apicurio ImageUnavailable (no recipe)
gkube proxy &                          # the cluster's names on the sandbox: an HTTP proxy
                                       #   on 127.0.0.1:3128
taiga-browser --proxy-server=http://127.0.0.1:3128 --new-window \
  http://user-service:8080/docs/ http://user-service:8080/docs/asyncapi/
```

The browser opens two tabs, by the Service's name and port as a pod reaches it: the REST
API (Swagger UI, `/docs/`) and the Kafka events the service publishes and consumes
(AsyncAPI, `/docs/asyncapi/`). (Chrome takes `--proxy-server` only when it starts: close
its windows first if it is already running.)

### Cluster names outside the cluster
Inside the cluster every pod reaches every Service by name, each on its own ClusterIP,
so they all can use the same port (`user-service:8080`, `kafka-connect:8083`,
`kafka:9092`). `gkube proxy` gives the sandbox the same names: a name a pod would resolve
(short, `.gluon-local`, `.svc`, `.svc.cluster.local`) is reached through the node, as
from a pod; anything else is connected directly, so the browser still reaches the rest
of the web. For a shell, point `http_proxy` at it:

```
export http_proxy=http://127.0.0.1:3128
curl -i http://user-service:8080/health         # liveness: 200 while the process is up, no body
curl -i http://user-service:8080/health/ready   # readiness: 200 when Postgres answers, else 503
curl -s http://kafka-connect:8083/connectors    # another Service, its own port
```

```
HTTP/1.1 200 OK
connection: close
content-length: 0
date: Wed, 18 Nov 2026 17:50:58 GMT
```

(`connection: close`: the proxy sends one request per connection.) From a pod, without
the proxy: `gkube run -- getent hosts user-service kafka user-postgres`, `gkube run --
curl -i http://user-service:8080/health/ready`.

The other way, one Service on one local port (kubectl's): `gkube port-forward
svc/user-service 8080:8080 &`, then `http://127.0.0.1:8080/docs/`.

From a terminal:

```
curl -s -X POST http://user-service:8080/auth/signup -H 'Content-Type: application/json' \
  -d '{"email": "me@example.com", "password": "correct-horse-battery-1"}'
gkube exec deploy/kafka -- /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic user-events --from-beginning --timeout-ms 10000
```

Stopping: `kill %1` (the proxy), then `gkube cluster stop` (every process and the state
directory go).

If a sign-up doesn't reach `user-events`: the user outbox connector's task can start
before user-service has created its outbox table and stay FAILED (a race in
bin/k8s-local-up's order, gluon `docs/gluon-kube-findings.md`). Check and restart it:

```
gkube run -- curl -s http://kafka-connect:8083/connectors/user-outbox-connector/status
gkube exec deploy/kafka-connect -- curl -sf -o /dev/null -X POST \
  "http://localhost:8083/connectors/user-outbox-connector/restart?includeTasks=true"
```

The contract harness's cluster mode against it, in a cluster of its own:
gluon `bin/gluon-kube-contract` (or `bin/verify-port user-service --only cluster`).

## Use

Every verb, as kubectl's and helm's (`-n gluon-local` accepted; `gluon-kube` or `gkube`):

```
gluon-kube cluster start|stop|status
gluon-kube up [SERVICE ...]                   # bin/k8s-local-up's sequence (USER_SERVICE_IMPL)
gluon-kube get pods|deploy|svc|jobs [NAME] [-o wide|name]
gluon-kube logs deploy/kafka-connect -c fetch-jmx-exporter [--previous] [--tail N]
gluon-kube exec [-i] deploy/user-postgres -- psql -U user user
gluon-kube run [--env K=V] -- curl -s http://user-service:8080/health   # a pod with the cluster's names
gluon-kube port-forward svc/user-service 8080:8080 [9090:9090]
gluon-kube proxy [--port 3128]                # cluster names outside the cluster (HTTP proxy)
gluon-kube apply -f ../../infra/k8s/local-infra/ [--dry-run]
gluon-kube delete deployment NAME | delete -f FILE [--ignore-not-found]
gluon-kube wait --for=condition=Ready pod --all --timeout=180s
gluon-kube wait --for=condition=complete job/debezium-connector-register --timeout=120s
gluon-kube template user-service ../../infra/k8s/gluon -f ../../environments/local/user-service-rs.values.yaml
gluon-kube helm upgrade --install user-service ../../infra/k8s/gluon -f …values.yaml
gluon-kube rollout restart|status deployment/user-service
gluon-kube create job cleanup --from=cronjob/user-outbox-cleanup
gluon-kube build user-service-rs              # the port's Dockerfile, with cargo
```

Images run as recipes (`src/gluon_kube/images.py`): the images' own entrypoints and
config, captured on the laptop ([`laptop-reference/`](./laptop-reference), read-only),
on the sandbox's binaries; each recipe lists what in it isn't the image's.

## Build and check

```
uv sync                      # the pinned Python and the locked dependencies
uv run gluon-kube --version
scripts/ci.sh                # what CI runs: format, lint, types, tests, 80% coverage
uv run spikes/netns-probe/probe.py   # the GK-1 probe (Linux, unprivileged namespaces)
```

Linux only: it needs unprivileged user namespaces and `bwrap`, which gluon
`bin/sandbox-setup` checks.

## Layout
- `src/gluon_kube/` — the simulator (`gluon-kube` command); `images/` the recipes' files
- `laptop-reference/` — the laptop's image and cluster captures (reference only)
- `tests/unit/`, `tests/integration/` — run by `scripts/ci.sh`
- `spikes/` — probes that decide a design, kept with their findings; not part
  of the package, not run by CI
- `conductor/` — product, tech stack, workflow and tracks
