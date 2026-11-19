#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for the Eclipse Temurin image's /__cacert_entrypoint.sh
# (apache/kafka's ENTRYPOINT; not captured). The real one imports extra CA
# certificates when asked (USE_SYSTEM_CA_CERTS), which gluon-local never does, then
# runs its arguments.
exec "$@"
