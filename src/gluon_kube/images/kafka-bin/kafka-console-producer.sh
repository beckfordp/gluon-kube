#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-console-producer.sh (plan GK-5.0): runs kafka.tools.ConsoleProducer.
if [ -z "${KAFKA_HEAP_OPTS:-}" ]; then export KAFKA_HEAP_OPTS="-Xmx512M"; fi
exec "$(dirname "$0")/kafka-run-class.sh" kafka.tools.ConsoleProducer "$@"
