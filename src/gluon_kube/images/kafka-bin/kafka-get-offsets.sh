#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-get-offsets.sh (all-Rust B6: gluon bin/reset-data): runs org.apache.kafka.tools.GetOffsetShell.
exec "$(dirname "$0")/kafka-run-class.sh" org.apache.kafka.tools.GetOffsetShell "$@"
