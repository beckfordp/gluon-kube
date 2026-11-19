#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-topics.sh (plan GK-5.0): runs org.apache.kafka.tools.TopicCommand.
exec "$(dirname "$0")/kafka-run-class.sh" org.apache.kafka.tools.TopicCommand "$@"
