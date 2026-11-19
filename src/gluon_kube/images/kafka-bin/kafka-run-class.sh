#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# gluon-kube's stand-in for Kafka's bin/kafka-run-class.sh (plan GK-5.0): Kafka's
# binary distribution, where the real one ships, isn't reachable from the sandbox.
# The same contract as far as gluon-local uses it: the KAFKA_* variables and their
# defaults, the jars in <base>/libs, JAVA_HOME or java on the PATH, and -daemon,
# -name and -loggc accepted (everything runs in the foreground, as in a container).
if [ $# -lt 1 ]; then
  echo "USAGE: $0 [-daemon] [-name servicename] [-loggc] classname [opts]"
  exit 1
fi
base_dir=$(dirname "$0")/..
while [ $# -gt 0 ]; do
  case "$1" in
    -daemon | -loggc) shift ;;
    -name) shift 2 ;;
    *) break ;;
  esac
done
CLASSPATH="${CLASSPATH:+$CLASSPATH:}$base_dir/libs/*"
LOG_DIR="${LOG_DIR:-$base_dir/logs}"
mkdir -p "$LOG_DIR" 2> /dev/null || true
if [ -z "${KAFKA_LOG4J_OPTS:-}" ]; then
  KAFKA_LOG4J_OPTS="-Dlog4j.configuration=file:$base_dir/config/tools-log4j.properties"
fi
KAFKA_LOG4J_OPTS="-Dkafka.logs.dir=$LOG_DIR $KAFKA_LOG4J_OPTS"
KAFKA_HEAP_OPTS="${KAFKA_HEAP_OPTS:--Xmx256M}"
KAFKA_JVM_PERFORMANCE_OPTS="${KAFKA_JVM_PERFORMANCE_OPTS:--server -XX:+UseG1GC -XX:MaxGCPauseMillis=20 -XX:InitiatingHeapOccupancyPercent=35 -XX:+ExplicitGCInvokesConcurrent -XX:MaxInlineLevel=15 -Djava.awt.headless=true}"
if [ -n "${JMX_PORT:-}" ]; then
  KAFKA_JMX_OPTS="${KAFKA_JMX_OPTS:-} -Dcom.sun.management.jmxremote.port=$JMX_PORT"
fi
JAVA="${JAVA_HOME:+$JAVA_HOME/bin/}java"
# Word splitting of the option variables is the real script's behaviour too.
# shellcheck disable=SC2086
exec "$JAVA" $KAFKA_HEAP_OPTS $KAFKA_JVM_PERFORMANCE_OPTS ${KAFKA_GC_LOG_OPTS:-} \
  ${KAFKA_JMX_OPTS:-} $KAFKA_LOG4J_OPTS -cp "$CLASSPATH" ${KAFKA_OPTS:-} "$@"
