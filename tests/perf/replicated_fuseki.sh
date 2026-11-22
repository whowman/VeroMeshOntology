#!/usr/bin/env bash
# Starts the replicated knowledge graph in Docker, as the Helm chart runs it: Kafka,
# one Fuseki writer (publishes each commit to Kafka before committing) and read
# replicas (apply the writer's changes from Kafka). Used by the capacity runs.
#
#   tests/perf/replicated_fuseki.sh start [replicas]   Kafka, the writer on :3030, replicas on :3031, :3032 ...
#   tests/perf/replicated_fuseki.sh replica <n>        one more replica, from an empty volume, on :303<n>
#   tests/perf/replicated_fuseki.sh stop
#
# Settings: IMAGE (default veromesh/fuseki:capacity, built from deploy/fuseki), HEAP (the
# writer's Java heap, default 4g), REPLICA_HEAP (default HEAP), KAFKA_TEST_IMAGE (from
# deploy/versions.env). Containers: kafka, fuseki (the writer), fuseki-r1, fuseki-r2, ...
# on the Docker network veromesh-capacity. The writer's GC log is in `docker logs fuseki`.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/deploy/versions.env"
IMAGE=${IMAGE:-veromesh/fuseki:capacity}
HEAP=${HEAP:-4g}
REPLICA_HEAP=${REPLICA_HEAP:-$HEAP}
NET=veromesh-capacity
CONF=${CONF_DIR:-/tmp/veromesh-fuseki-conf}
TOPIC=veromesh.graph

healthy() {  # wait for a container's health check
  for _ in $(seq 300); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null)" = healthy ] && return 0
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ] || { docker logs --tail 80 "$1"; return 1; }
    sleep 2
  done
  docker logs --tail 80 "$1"
  return 1
}

configs() {
  mkdir -p "$CONF"
  cat >"$CONF/writer.ttl" <<EOF
PREFIX fuseki: <http://jena.apache.org/fuseki#>
PREFIX rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX tdb2:   <http://jena.apache.org/2016/tdb#>
PREFIX vmr:    <https://veromesh.ai/ns/fuseki-replication#>
[] rdf:type fuseki:Server .
<#service> rdf:type fuseki:Service ;
    fuseki:name "veromesh" ;
    fuseki:endpoint [ fuseki:operation fuseki:query ;  fuseki:name "query" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:update ; fuseki:name "update" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:gsp-rw ; fuseki:name "data" ] ;
    fuseki:dataset <#dataset> .
<#dataset> rdf:type vmr:PublishingDataset ;
    vmr:dataset           <#tdb2> ;
    vmr:bootstrapServers  "kafka:9092" ;
    vmr:topic             "$TOPIC" ;
    vmr:transactionalId   "veromesh-fuseki-$TOPIC" ;
    vmr:replicationFactor "1" ;
    vmr:stateFile         "/fuseki/databases/writer.state" .
<#tdb2> rdf:type tdb2:DatasetTDB2 ; tdb2:location "/fuseki/databases/veromesh" .
EOF
  cat >"$CONF/replica.ttl" <<EOF
PREFIX fuseki: <http://jena.apache.org/fuseki#>
PREFIX rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX tdb2:   <http://jena.apache.org/2016/tdb#>
PREFIX fk:     <http://jena.apache.org/fuseki/kafka#>
[] rdf:type fuseki:Server .
<#service> rdf:type fuseki:Service ;
    fuseki:name "veromesh" ;
    fuseki:endpoint [ fuseki:operation fuseki:query ; fuseki:name "query" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:gsp-r ; fuseki:name "data" ] ;
    fuseki:dataset <#tdb2> .
<#tdb2> rdf:type tdb2:DatasetTDB2 ; tdb2:location "/fuseki/databases/veromesh" .
<#connector> rdf:type fk:Connector ;
    fk:bootstrapServers  "kafka:9092" ;
    fk:topic             "$TOPIC" ;
    fk:fusekiServiceName "/veromesh" ;
    fk:groupId           "env:{VEROMESH_KAFKA_GROUP_ID}" ;
    fk:stateFile         "/fuseki/databases/replica.state" ;
    fk:syncTopic         true ;
    fk:replayTopic       false ;
    fk:config ( "isolation.level" "read_committed" ) ;
    # Commit each change as soon as the replica has caught up. At the default (10) a
    # quiet topic switches the connector to low-volume batching, which holds changes
    # until a poll comes back empty: about 10 s behind the writer on every edit.
    fk:config ( "fuseki.kafka.low.volume.threshold" "0" ) ;
    fk:config ( "auto.offset.reset" "earliest" ) ;
    fk:config ( "allow.auto.create.topics" "false" ) .
EOF
}

replica() {  # replica <n>: a replica on port 303<n>, from an empty volume
  local n=$1 name="fuseki-r$1"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --network "$NET" -p "127.0.0.1:303$n:3030" \
    -v "$CONF:/etc/fuseki:ro" -e FUSEKI_CONFIG=/etc/fuseki/replica.ttl \
    -e REPLICA_ID_FILE=/fuseki/databases/replica.id \
    -e JAVA_OPTS="-Xmx$REPLICA_HEAP" "$IMAGE" >/dev/null
  healthy "$name"
}

case "${1:-}" in
  start)
    replicas=${2:-2}
    "$0" stop >/dev/null 2>&1 || true
    configs
    docker network create "$NET" >/dev/null
    docker run -d --name kafka --network "$NET" \
      -e KAFKA_NODE_ID=1 -e KAFKA_PROCESS_ROLES=broker,controller \
      -e KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 \
      -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://kafka:9092 \
      -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
      -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
      -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
      -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
      -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
      -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 \
      -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 \
      -e KAFKA_AUTO_CREATE_TOPICS_ENABLE=false \
      -e KAFKA_HEAP_OPTS="-Xmx1g -Xms1g" \
      "$KAFKA_TEST_IMAGE" >/dev/null
    for _ in $(seq 60); do
      docker exec kafka /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 >/dev/null 2>&1 && break
      sleep 2
    done
    # The writer creates the topic (one partition, kept forever) before it serves.
    docker run -d --name fuseki --network "$NET" -p 127.0.0.1:3030:3030 \
      -v "$CONF:/etc/fuseki:ro" -e FUSEKI_CONFIG=/etc/fuseki/writer.ttl \
      -e JAVA_OPTS="-Xmx$HEAP -Xlog:gc:stdout:uptime" "$IMAGE" >/dev/null
    healthy fuseki
    for n in $(seq "$replicas"); do replica "$n"; done
    echo "Replicated Fuseki: writer :3030, $replicas replica(s) from :3031, Kafka topic $TOPIC"
    ;;
  replica) replica "$2" ;;
  stop)
    docker rm -f fuseki kafka $(docker ps -aq --filter "name=fuseki-r") >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: $0 start [replicas] | replica <n> | stop" >&2
    exit 2
    ;;
esac
