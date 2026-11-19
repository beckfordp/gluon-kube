# Laptop reference (read-only)

**Captured on the laptop, not in the sandbox. Reference only: never edit these
files, and nothing in gluon-kube reads them at run time.**

What the sandbox can't produce itself: the real images' contracts and a running
gluon-local on OrbStack, for GK-4's and GK-5's image recipes to be checked against
(gluon `plans/gluon-kube/plan.md`; ADR 0030's risk mitigation). Captured by
`scripts/capture-laptop-reference.sh`, uploaded as `gluon-kube-reference.tar.gz`, and
copied here unchanged (byte for byte, original timestamps). `PROVENANCE.txt` says
when, from which gluon commit, and with which helm, kubectl, docker and OS.

| Path | What |
|---|---|
| `PROVENANCE.txt` | When, gluon commit, tool versions |
| `images/` | Per image gluon-local runs: `docker image inspect`, `/etc/passwd`, `/etc/group`; Postgres' and Debezium's entrypoint scripts; Kafka's `/etc/kafka/docker` and `/opt/kafka/config`, Debezium's config dir (as tars) and its plugin list |
| `cluster/` | gluon-local on OrbStack: the objects (`kubectl get -o yaml`), endpoints, events, and what a pod sees (`inside-<deployment>.txt`: `id`, `/etc/hosts`, `/etc/resolv.conf`, env, mounts) |

The same run's `helm-template/` output is a test fixture, not reference, so it lives
in `tests/fixtures/helm-template/` (GK-3.5).

Reviewed before it was added: no credentials, personal data or laptop paths. The
e-mail addresses are upstream image maintainers' (image labels); the `192.168.x.x`
addresses are OrbStack's internal node and pod network; the passwords are the
throw-away local database defaults the manifests already publish.

A recapture replaces this directory as a whole, with its `PROVENANCE.txt`.
