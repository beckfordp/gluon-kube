#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-broker-api-versions.sh (plan GK-5.0): runs kafka.admin.BrokerApiVersionsCommand.
exec "$(dirname "$0")/kafka-run-class.sh" kafka.admin.BrokerApiVersionsCommand "$@"
