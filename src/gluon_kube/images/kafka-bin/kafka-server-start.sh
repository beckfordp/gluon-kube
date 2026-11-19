#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-server-start.sh (plan GK-5.0): the
# broker (kafka.Kafka) with the real script's log4j and heap defaults.
if [ $# -lt 1 ]; then
  echo "USAGE: $0 [-daemon] server.properties [--override property=value]*"
  exit 1
fi
base_dir=$(dirname "$0")
if [ -z "${KAFKA_LOG4J_OPTS:-}" ]; then
  export KAFKA_LOG4J_OPTS="-Dlog4j.configuration=file:$base_dir/../config/log4j.properties"
fi
if [ -z "${KAFKA_HEAP_OPTS:-}" ]; then
  export KAFKA_HEAP_OPTS="-Xmx1G -Xms1G"
fi
EXTRA_ARGS=${EXTRA_ARGS-'-name kafkaServer -loggc'}
# shellcheck disable=SC2086
exec "$base_dir/kafka-run-class.sh" $EXTRA_ARGS kafka.Kafka "$@"
