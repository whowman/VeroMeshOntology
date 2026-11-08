#!/bin/sh
# Starts Fuseki in one of three modes (see the Dockerfile).
#
#   FUSEKI_CONFIG unset      single node: TDB2 at /fuseki/databases/$FUSEKI_DATASET, read/write
#   FUSEKI_CONFIG=<file>     writer or replica: the configuration file decides, with the
#                            replication modules on the classpath
#
# Replica only: REPLICA_ID_FILE names a file on the replica's volume holding a
# random id, created on first start. It becomes the Kafka consumer group
# (VEROMESH_KAFKA_GROUP_ID), so a replica whose volume is replaced starts a new
# group and rebuilds from the start of the topic instead of resuming where the
# old volume left off.
set -eu

JAR=/opt/fuseki/fuseki-server.jar
DB=/fuseki/databases

if [ -z "${FUSEKI_CONFIG:-}" ]; then
  mkdir -p "$DB/$FUSEKI_DATASET"
  # shellcheck disable=SC2086
  exec java $JAVA_OPTS -jar "$JAR" --port 3030 --ping --tdb2 --loc "$DB/$FUSEKI_DATASET" --update "/$FUSEKI_DATASET"
fi

if [ -n "${REPLICA_ID_FILE:-}" ]; then
  if [ ! -s "$REPLICA_ID_FILE" ]; then
    mkdir -p "$(dirname "$REPLICA_ID_FILE")"
    tr -dc 'a-z0-9' </dev/urandom | head -c 12 >"$REPLICA_ID_FILE.tmp"
    mv "$REPLICA_ID_FILE.tmp" "$REPLICA_ID_FILE"
  fi
  VEROMESH_KAFKA_GROUP_ID="veromesh-fuseki-${HOSTNAME:-replica}-$(cat "$REPLICA_ID_FILE")"
  export VEROMESH_KAFKA_GROUP_ID
  echo "Replica consumer group: $VEROMESH_KAFKA_GROUP_ID"
fi

# The Fuseki-Kafka module bundles the Kafka client; keep Fuseki's own logging (Log4j2).
# shellcheck disable=SC2086
exec java $JAVA_OPTS -Dslf4j.provider=org.apache.logging.slf4j.SLF4JServiceProvider \
  -cp "$JAR:/opt/fuseki/lib/*" org.apache.jena.fuseki.main.cmds.FusekiServerCmd \
  --port 3030 --ping --conf "$FUSEKI_CONFIG"
