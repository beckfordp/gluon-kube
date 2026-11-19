#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/connect-distributed.sh (plan GK-5.0): a
# distributed Connect worker with the real script's log4j and heap defaults.
if [ $# -lt 1 ]; then
  echo "USAGE: $0 [-daemon] connect-distributed.properties"
  exit 1
fi
base_dir=$(dirname "$0")
if [ -z "${KAFKA_LOG4J_OPTS:-}" ]; then
  export KAFKA_LOG4J_OPTS="-Dlog4j.configuration=file:$base_dir/../config/connect-log4j.properties"
fi
if [ -z "${KAFKA_HEAP_OPTS:-}" ]; then
  export KAFKA_HEAP_OPTS="-Xms256M -Xmx2G"
fi
EXTRA_ARGS=${EXTRA_ARGS-'-name connectDistributed'}
# shellcheck disable=SC2086
exec "$base_dir/kafka-run-class.sh" $EXTRA_ARGS org.apache.kafka.connect.cli.ConnectDistributed "$@"
