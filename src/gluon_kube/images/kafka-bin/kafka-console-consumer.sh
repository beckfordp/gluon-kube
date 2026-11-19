#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-console-consumer.sh (plan GK-5.0): runs org.apache.kafka.tools.consumer.ConsoleConsumer.
if [ -z "${KAFKA_HEAP_OPTS:-}" ]; then export KAFKA_HEAP_OPTS="-Xmx512M"; fi
exec "$(dirname "$0")/kafka-run-class.sh" org.apache.kafka.tools.consumer.ConsoleConsumer "$@"
