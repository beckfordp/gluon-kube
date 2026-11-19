#!/bin/sh
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for curlimages/curl's /entrypoint.sh (not captured): runs its
# arguments, with `curl` put first when the first is an option or not a command, as
# the image documents. gluon-local's manifests always give a command.
if [ "${1#-}" != "$1" ] || [ -z "$(command -v "$1")" ]; then
  set -- curl "$@"
fi
exec "$@"
