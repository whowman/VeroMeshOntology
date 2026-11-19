#!/usr/bin/env bash
# Install the VeroMesh chart into a throwaway kind cluster and prove it works:
#   1. images build and load; a single-node Kafka starts; the release installs
#      (writer Fuseki + 2 read replicas) and becomes ready
#   2. helm test passes (health, schema, knowledge graph reachable)
#   3. the worked example loads through the API and commits
#   4. both replicas hold exactly what the writer holds (replication via Kafka)
#   5. while the writer is down, the API still answers reads (from the replicas);
#      after it restarts the model is intact (persistent volume) and new writes
#      reach the replicas again
#   6. a replica whose volume is deleted rebuilds itself from the Kafka topic
#   7. a pod that is not the service cannot reach the writer or the replicas
#      (NetworkPolicy)
#   8. backups: a decision is recorded in PostgreSQL; a backup job (from the
#      CronJob, encrypted to a key made for the test) holds the graph and the log
#   9. restore: after more changes, the graph returns exactly to the backup on the
#      writer and both replicas; the decision log, which holds entries, is kept
#  10. disaster: with the decision log database emptied, a restore brings it back
#      with its hash chain intact
#
# Run with scripts/test.sh k8s. Needs Docker, kind, kubectl and Helm.
#   KIND_CLUSTER=name   cluster to use (created if missing; default veromesh-test)
#   KEEP_CLUSTER=1      leave the cluster running afterwards
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLUSTER="${KIND_CLUSTER:-veromesh-test}"
NS=veromesh
RELEASE=veromesh
RESULTS="${1:-$ROOT/.build/test-results}"
mkdir -p "$RESULTS"
CTX="kind-$CLUSTER"
KEY="$(mktemp)"
k() { kubectl --context "$CTX" -n "$NS" "$@"; }
# shellcheck source=../../deploy/versions.env
source "$ROOT/deploy/versions.env"
WRITER="$RELEASE-fuseki-0"
REPLICAS=("$RELEASE-fuseki-replica-0" "$RELEASE-fuseki-replica-1")

created=""
PF_PID=""
FPF_PID=""
cleanup() {
  local rc=$?
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  [[ -n "$FPF_PID" ]] && kill "$FPF_PID" 2>/dev/null || true
  if (( rc != 0 )); then
    echo "!! Failed; cluster state follows" >&2
    k get pods,pvc,svc -o wide >&2 || true
    k describe pods >"$RESULTS/k8s-describe.txt" 2>&1 || true
    for p in $(k get pods -o name 2>/dev/null); do
      echo "---- $p" >>"$RESULTS/k8s-logs.txt"; k logs "$p" --all-containers >>"$RESULTS/k8s-logs.txt" 2>&1 || true
    done
    echo "!! Pod descriptions and logs: $RESULTS/k8s-describe.txt, $RESULTS/k8s-logs.txt" >&2
  fi
  rm -f "$KEY"
  if [[ -n "$created" && -z "${KEEP_CLUSTER:-}" ]]; then
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  fi
  exit $rc
}
trap cleanup EXIT

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo ">> Creating kind cluster $CLUSTER"
  kind create cluster --name "$CLUSTER" --wait 180s >/dev/null
  created=1
fi

echo ">> Building images and loading them into kind"
TAG=test SKIP_DATAHUB=1 "$ROOT/deploy/helm/build-images.sh" --kind "$CLUSTER" >/dev/null

# A pulled image is a multi-platform index whose other platforms are not on
# disk; "kind load docker-image" then fails with "content digest not found".
# Save only this machine's platform and load the archive.
load_pulled() {
  local archive
  docker pull -q "$1" >/dev/null
  archive=$(mktemp)
  docker save --platform "$(docker version -f '{{.Server.Os}}/{{.Server.Arch}}')" -o "$archive" "$1"
  kind load image-archive "$archive" --name "$CLUSTER" >/dev/null
  rm -f "$archive"
}

echo ">> Starting a single-node Kafka ($KAFKA_TEST_IMAGE) and PostgreSQL ($POSTGRES_TEST_IMAGE)"
load_pulled "$KAFKA_TEST_IMAGE"
load_pulled "$POSTGRES_TEST_IMAGE"
kubectl --context "$CTX" create namespace "$NS" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
sed "s|KAFKA_IMAGE|$KAFKA_TEST_IMAGE|" "$ROOT/tests/k8s/kafka.yaml" | k apply -f - >/dev/null
sed "s|POSTGRES_IMAGE|$POSTGRES_TEST_IMAGE|" "$ROOT/tests/k8s/postgres.yaml" | k apply -f - >/dev/null
k apply -f "$ROOT/tests/k8s/backup-pvc.yaml" >/dev/null
k rollout status deployment/kafka --timeout 5m
k rollout status deployment/postgres --timeout 5m

# The customer's backup key, made for this test; only the public half goes into the cluster.
docker run --rm --entrypoint age-keygen veromesh/backup:test >"$KEY" 2>/dev/null
PUBLIC_KEY="$(sed -n 's/^# public key: //p' "$KEY")"

echo ">> Installing the chart"
helm --kube-context "$CTX" upgrade --install "$RELEASE" "$ROOT/deploy/helm/veromesh" \
  -n "$NS" --create-namespace -f "$ROOT/deploy/helm/veromesh/ci/kind-values.yaml" \
  --set "backup.encryption.recipients={$PUBLIC_KEY}" --wait --timeout 10m

echo ">> helm test"
helm --kube-context "$CTX" test "$RELEASE" -n "$NS" --logs --timeout 3m

port_forward() {
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  k port-forward "svc/$RELEASE-api" 18700:8700 >"$RESULTS/k8s-port-forward.log" 2>&1 &
  PF_PID=$!
  for _ in $(seq 1 30); do curl -fs http://127.0.0.1:18700/health >/dev/null && return 0; sleep 1; done
  echo "!! port-forward to the API did not come up" >&2; return 1
}
port_forward

echo ">> Loading the worked example through the API"
python3 "$ROOT/tests/k8s/smoke.py" http://127.0.0.1:18700 load

# Quads held by one Fuseki pod, read from its own query endpoint. Port-forwarding
# goes through the kubelet, so the NetworkPolicy does not get in the way here.
count_on() {
  local pod="$1" port=18730 n
  [[ -n "$FPF_PID" ]] && kill "$FPF_PID" 2>/dev/null || true
  k port-forward "pod/$pod" "$port:3030" >>"$RESULTS/k8s-port-forward.log" 2>&1 &
  FPF_PID=$!
  for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$port/\$/ping" >/dev/null && break; sleep 1; done
  n="$(python3 "$ROOT/tests/k8s/smoke.py" "http://127.0.0.1:$port/veromesh" count)"
  kill "$FPF_PID" 2>/dev/null || true
  wait "$FPF_PID" 2>/dev/null || true
  FPF_PID=""
  echo "$n"
}

# Every replica ends up with exactly the writer's quads.
replicas_match_writer() {
  local want have
  want="$(count_on "$WRITER")"
  (( want > 0 )) || { echo "!! the writer holds no data" >&2; return 1; }
  for pod in "${REPLICAS[@]}"; do
    for i in $(seq 1 60); do
      have="$(count_on "$pod" 2>/dev/null || echo -1)"
      [[ "$have" == "$want" ]] && break
      (( i == 60 )) && { echo "!! $pod has $have quads, the writer $want" >&2; return 1; }
      sleep 3
    done
    echo "   $pod: $have quads, same as the writer"
  done
}

echo ">> Replication: the replicas hold what the writer holds"
replicas_match_writer

echo ">> Writer down: reads still answered, from the replicas"
k delete pod "$WRITER" --wait=false >/dev/null
for _ in $(seq 1 60); do
  [[ "$(k get pod "$WRITER" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)" == true ]] || break
  sleep 1
done
python3 "$ROOT/tests/k8s/smoke.py" http://127.0.0.1:18700 check
if k logs deployment/"$RELEASE"-api --since=2m 2>/dev/null | grep -q "reading from http://$RELEASE-fuseki-replicas"; then
  echo "   the service read from the replicas"
else
  echo "   (the writer was back before the reads; no fallback needed)"
fi

echo ">> Writer back: the model survived, and new writes reach the replicas"
k rollout status "statefulset/$RELEASE-fuseki" --timeout 5m
python3 "$ROOT/tests/k8s/smoke.py" http://127.0.0.1:18700 check
python3 "$ROOT/tests/k8s/smoke.py" http://127.0.0.1:18700 note
replicas_match_writer

echo ">> A replica with a new, empty volume rebuilds from the topic"
k scale "statefulset/$RELEASE-fuseki-replica" --replicas 1 >/dev/null
k wait --for=delete "pod/${REPLICAS[1]}" --timeout 3m
k delete pvc "data-${REPLICAS[1]}" --wait=true >/dev/null
k scale "statefulset/$RELEASE-fuseki-replica" --replicas 2 >/dev/null
k rollout status "statefulset/$RELEASE-fuseki-replica" --timeout 5m
replicas_match_writer

echo ">> NetworkPolicy: other pods cannot reach the writer or the replicas"
for target in "$RELEASE-fuseki" "$RELEASE-fuseki-replicas"; do
  k delete pod np-probe --ignore-not-found --wait=true >/dev/null
  k run np-probe --restart=Never --image "veromesh/service:test" --image-pull-policy=Never \
    --command -- python -c "import urllib.request; urllib.request.urlopen('http://$target:3030/\$/ping', timeout=10); print('REACHED')" >/dev/null
  for _ in $(seq 1 60); do
    phase="$(k get pod np-probe -o jsonpath='{.status.phase}')"
    [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
    sleep 2
  done
  if [[ "$phase" != Failed ]]; then
    echo "!! a pod without the service's labels reached $target (phase $phase)" >&2
    k logs np-probe >&2 || true
    exit 1
  fi
  echo "   $target refused the probe pod, as intended"
done
k delete pod np-probe >/dev/null

api() { python3 "$ROOT/tests/k8s/smoke.py" http://127.0.0.1:18700 "$@"; }
backups() { "$ROOT/scripts/veromesh-backup.sh" --context "$CTX" -n "$NS" -r "$RELEASE" --timeout 15m "$@"; }

echo ">> Backup: a decision in PostgreSQL, then a backup job encrypted to the test key"
api decide
entries="$(api log)"
backups now | tee "$RESULTS/k8s-backup.log"
id="$(sed -n 's/^BACKUP_ID=//p' "$RESULTS/k8s-backup.log")"
[[ -n "$id" ]] || { echo "!! the backup job printed no id" >&2; exit 1; }
quads="$(count_on "$WRITER")"
echo "   backup $id: $quads quads, $entries decision log entries"

echo ">> Restore: later changes to the graph are undone; the decision log keeps its entries"
api note
api decide
changed="$(count_on "$WRITER")"
[[ "$changed" != "$quads" ]] || { echo "!! the model did not change after the backup" >&2; exit 1; }
backups restore "$id" --identity "$KEY" --yes | tee "$RESULTS/k8s-restore.log"
port_forward
have="$(count_on "$WRITER")"
[[ "$have" == "$quads" ]] || { echo "!! after the restore the writer holds $have quads, the backup $quads" >&2; exit 1; }
replicas_match_writer
api check
[[ "$(api log)" == "$((entries + 1))" ]] || { echo "!! the decision log lost entries" >&2; exit 1; }
grep -q "Decision log: NOT restored" "$RESULTS/k8s-restore.log"
echo "   graph back to $quads quads everywhere; the decision log kept its $((entries + 1)) entries"

echo ">> Disaster: the decision log database is lost; a restore brings it back, chain intact"
k exec deploy/postgres -- psql -q -U veromesh -d decisions -c "DROP TABLE veromesh_decision_log CASCADE"
backups restore "$id" --identity "$KEY" --yes | tee "$RESULTS/k8s-restore-2.log"
port_forward
[[ "$(api log)" == "$entries" ]] || { echo "!! the decision log was not restored" >&2; exit 1; }
backups list | grep -q "^$id" || { echo "!! list does not show $id" >&2; exit 1; }
echo "   decision log restored: $entries entries, hash chain verified"

echo ">> k8s smoke test passed"
