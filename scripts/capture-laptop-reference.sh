#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Laptop only (gluon plan GK-3.5, and the reference GK-4/GK-5's image recipes are
# checked against): captures what the sandbox can't produce, into one directory,
# for tar-and-upload to the sandbox:
#   PROVENANCE.txt    when, gluon commit, helm/kubectl/docker versions, OS
#   helm-template/    `helm template` of infra/k8s/gluon with every
#                     environments/local/*.values.yaml (release name as
#                     bin/k8s-local-up uses it), and with the chart's defaults
#   images/           for each image gluon-local runs: `docker image inspect`
#                     (Entrypoint, Cmd, Env, User, WorkingDir, ExposedPorts), its
#                     /etc/passwd and /etc/group, and its entrypoint scripts
#   cluster/          if gluon-local is up: the objects, endpoints and events, and
#                     what a pod sees (/etc/hosts, /etc/resolv.conf, env, id)
# Nothing here changes the cluster or any image (read-only; images are pulled if
# missing). Each step that fails is logged in FAILED.txt and the rest carries on.
# No laptop paths are written: commands run from the gluon root.
#
# Usage, from the gluon root on the laptop (bash 3.2 is fine):
#   tools/gluon-kube/scripts/capture-laptop-reference.sh
#   tar czf gluon-kube-reference.tar.gz gluon-kube-reference-*/
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
[ -d infra/k8s/gluon ] && [ -d environments/local ] || { echo "run from a gluon checkout" >&2; exit 1; }

OUT="gluon-kube-reference-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$OUT/helm-template" "$OUT/images" "$OUT/cluster"
NS=gluon-local
failed() { echo "$*" >> "$OUT/FAILED.txt"; echo "  (failed: $*)"; }
# try NAME OUTFILE COMMAND...: stdout to OUTFILE; stderr too, on failure, with a FAILED.txt line
try() {
  local name="$1" out="$2"
  shift 2
  if ! "$@" > "$out" 2> "$out.err"; then
    cat "$out.err" >> "$out"
    failed "$name"
  fi
  rm -f "$out.err"
}

echo "== provenance"
{
  echo "captured: $(date -u +%Y-%m-%dT%H:%M:%SZ) by tools/gluon-kube/scripts/capture-laptop-reference.sh"
  echo "gluon commit: $(git rev-parse HEAD 2> /dev/null || echo unknown)"
  echo "gluon dirty files: $(git status --porcelain 2> /dev/null | wc -l | tr -d ' ')"
  echo "helm: $(helm version --short 2> /dev/null || echo missing)"
  echo "kubectl: $(kubectl version --client 2> /dev/null | head -n 1 || echo missing)"
  echo "kubectl context: $(kubectl config current-context 2> /dev/null || echo none)"
  echo "docker server: $(docker version --format '{{.Server.Version}}' 2> /dev/null || echo missing)"
  echo "os: $(uname -sm)"
} > "$OUT/PROVENANCE.txt"
cat "$OUT/PROVENANCE.txt"

echo "== helm template"
for values in environments/local/*.values.yaml; do
  name="$(basename "$values" .values.yaml)"
  release="$name"
  # bin/k8s-local-up installs the Rust port under the Scala release's name.
  [ "$name" = user-service-rs ] && release=user-service
  echo "  $name (release $release)"
  try "helm template $name" "$OUT/helm-template/$name.yaml" \
    helm template "$release" infra/k8s/gluon --namespace "$NS" -f "$values"
  echo "helm template $release infra/k8s/gluon --namespace $NS -f $values" >> "$OUT/helm-template/COMMANDS.txt"
done
try "helm template chart defaults" "$OUT/helm-template/_chart-defaults.yaml" \
  helm template example infra/k8s/gluon --namespace "$NS"
echo "helm template example infra/k8s/gluon --namespace $NS" >> "$OUT/helm-template/COMMANDS.txt"

echo "== images"
for image in postgres:16-alpine apache/kafka:3.8.0 debezium/connect:3.0.0.Final \
  curlimages/curl:8.10.1 curlimages/curl:8.11.1 user-service-rs:latest; do
  safe="$(echo "$image" | tr '/:' '__')"
  echo "  $image"
  if ! docker image inspect "$image" > /dev/null 2>&1; then
    docker pull -q "$image" > /dev/null 2>&1 || { failed "docker pull $image"; continue; }
  fi
  try "inspect $image" "$OUT/images/$safe.inspect.json" docker image inspect "$image"
  for file in /etc/passwd /etc/group; do
    try "$image $file" "$OUT/images/$safe$(echo "$file" | tr '/' '_')" \
      docker run --rm --entrypoint cat "$image" "$file"
  done
done
# The entrypoint scripts each recipe re-creates.
try "postgres entrypoint" "$OUT/images/postgres_16-alpine.docker-entrypoint.sh" \
  docker run --rm --entrypoint cat postgres:16-alpine /usr/local/bin/docker-entrypoint.sh
try "kafka /etc/kafka/docker" "$OUT/images/apache_kafka_3.8.0.etc-kafka-docker.tar" \
  docker run --rm --entrypoint tar apache/kafka:3.8.0 -C /etc/kafka/docker -cf - .
try "kafka /opt/kafka/config" "$OUT/images/apache_kafka_3.8.0.opt-kafka-config.tar" \
  docker run --rm --entrypoint tar apache/kafka:3.8.0 -C /opt/kafka/config -cf - .
try "debezium entrypoint" "$OUT/images/debezium_connect_3.0.0.Final.docker-entrypoint.sh" \
  docker run --rm --entrypoint cat debezium/connect:3.0.0.Final /docker-entrypoint.sh
try "debezium config dir" "$OUT/images/debezium_connect_3.0.0.Final.kafka-config.tar" \
  docker run --rm --entrypoint tar debezium/connect:3.0.0.Final -C /kafka/config -cf - .
try "debezium plugin list" "$OUT/images/debezium_connect_3.0.0.Final.connect-plugins.txt" \
  docker run --rm --entrypoint sh debezium/connect:3.0.0.Final -c 'ls -R /kafka/connect | head -n 400'

echo "== cluster (read-only; skipped if gluon-local isn't up)"
if kubectl get namespace "$NS" > /dev/null 2>&1; then
  for kind in deployments services endpoints configmaps jobs cronjobs pods; do
    try "kubectl get $kind" "$OUT/cluster/$kind.yaml" kubectl get "$kind" -n "$NS" -o yaml
  done
  try "kubectl get pods wide" "$OUT/cluster/pods.txt" kubectl get pods -n "$NS" -o wide
  try "kubectl get events" "$OUT/cluster/events.txt" kubectl get events -n "$NS" --sort-by=.lastTimestamp
  for deploy in user-service user-postgres kafka kafka-connect notification-service; do
    kubectl get deployment "$deploy" -n "$NS" > /dev/null 2>&1 || continue
    echo "  inside deploy/$deploy"
    try "exec $deploy" "$OUT/cluster/inside-$deploy.txt" kubectl exec -n "$NS" "deploy/$deploy" -- sh -c \
      'echo "## id"; id; echo "## /etc/hosts"; cat /etc/hosts; echo "## /etc/resolv.conf"; cat /etc/resolv.conf; echo "## env"; env | sort; echo "## pwd"; pwd; echo "## mounts"; cat /proc/mounts'
  done
else
  echo "  gluon-local not reachable: cluster/ left empty" | tee "$OUT/cluster/SKIPPED.txt"
fi

echo
echo "Done: $OUT"
[ -f "$OUT/FAILED.txt" ] && { echo "Some steps failed (listed in $OUT/FAILED.txt):"; cat "$OUT/FAILED.txt"; }
echo "Next: tar czf gluon-kube-reference.tar.gz $OUT/  and upload it to the sandbox."
