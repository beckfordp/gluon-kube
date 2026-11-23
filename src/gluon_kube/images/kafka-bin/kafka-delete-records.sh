#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-delete-records.sh (all-Rust B6: gluon bin/reset-data): runs org.apache.kafka.tools.DeleteRecordsCommand.
exec "$(dirname "$0")/kafka-run-class.sh" org.apache.kafka.tools.DeleteRecordsCommand "$@"
