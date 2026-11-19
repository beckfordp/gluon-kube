#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-storage.sh (plan GK-5.0): runs kafka.tools.StorageTool.
exec "$(dirname "$0")/kafka-run-class.sh" kafka.tools.StorageTool "$@"
