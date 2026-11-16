# Initial Concept

A simulated gluon-local for the sandbox, per gluon ADR 0030 and gluon
`plans/gluon-kube/plan.md`: a Python program that reads gluon-local's
unchanged manifests, Helm chart and values files, and runs each workload as a
local process inside unprivileged namespaces, so the Rust ports can be run and
checked at the cluster level in the sandbox, where there is no Docker and no
Kubernetes.

# Product — gluon-kube

## Purpose
Bring the cluster-level part of the laptop pass into the sandbox (ADR 0030
decision 1): the chart and values files, Service names, probes, restarts,
rollouts, Jobs, services calling each other by name, and the contract
harness's cluster mode. It is a simulator, not Kubernetes: it copies the
behaviour Gluon's services and scripts depend on and reports everything else.

## Where expectations come from
In order of precedence:
1. gluon's manifests, chart and values files (`infra/k8s/`,
   `environments/local/`), used unchanged (ADR 0030 decision 2);
2. Kubernetes' documented behaviour for what is simulated (probes, restart
   backoff, Services, Jobs), and `helm template` output captured on the laptop
   with its provenance;
3. each image's documented contract (its env vars, entrypoint, paths), for
   the recipe that stands in for it (ADR 0030 decision 4);
4. OrbStack's behaviour, compared on the laptop (plan GK-8.2).

## Users
- The consultant (solo): brings up a Rust port with its dependencies in the
  sandbox, and runs the contract harness's cluster mode against it
- Reviewers of a port: read `gluon-kube apply`'s report of what was and wasn't
  simulated

## Goals
- `gluon-kube up <service>`: `bin/k8s-local-up`'s sequence, in the sandbox
- Per-pod network and mount namespaces; Service names and ports as the
  manifests give them (the design proven in GK-1)
- `kubectl`-like verbs: `apply`, `delete`, `get`, `logs`, `exec`, `wait`,
  `rollout`; `helm upgrade --install` and `template`
- Nothing skipped silently: every unsupported kind or field reported

## Non-goals
- Being Kubernetes, or replacing OrbStack on the laptop
- Running images: each image has a local recipe, or is `ImageUnavailable`
- The observability stack, Redis, Apicurio and the Scala services (version 1)
- Enforcing resource limits

## Scope (from gluon `plans/gluon-kube/plan.md`; items owned by this repo)
GK-1 (the feasibility probe) to GK-7, one increment per sandbox session, each
reviewed before the next starts.

## Constraints
Developed and run in the sandbox (ADR 0028, 0030): no root, no Docker, no
`iptables`, no GitHub or image registries; unprivileged user namespaces and
`bwrap`. Laptop captures (`helm template`) come on request.
