#!/usr/bin/env bash
# Run VeroMesh backups and restores in a Kubernetes install, from an operator's machine.
#
#   scripts/veromesh-backup.sh now                       take a backup now
#   scripts/veromesh-backup.sh list                      the backups held
#   scripts/veromesh-backup.sh restore <id> --identity <private key file>
#                              [--decision-log auto|skip|replace] [--yes]
#
# Options (before or after the command)
#   -n, --namespace NS     the release's namespace (default: kubectl's current one)
#   -r, --release NAME     the Helm release (default: veromesh)
#   --context CTX          kubectl context
#   --timeout DURATION     how long to wait for a job (default 60m)
#
# Every run is a Job built from the backup CronJob, so it uses exactly the
# settings, image and storage of the nightly backup.
#
# restore:
#   1. stops the VeroMesh service (scales it to zero), so nothing writes meanwhile;
#   2. puts the private key in a temporary Secret, used only by the restore job;
#   3. replaces the knowledge graph with the backup's, through the Fuseki writer
#      (the read replicas follow through Kafka), and restores the decision log if
#      its database is empty (auto), never (skip), or always (replace: decisions
#      recorded after the backup are lost);
#   4. deletes the Secret and starts the service again, whether or not it worked.
#
# Needs kubectl and jq.
set -euo pipefail

NS="" RELEASE=veromesh CONTEXT="" TIMEOUT=60m
CMD="" ID="" IDENTITY="" DLOG=auto YES=""
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }
die() { echo "veromesh-backup: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    -n | --namespace) NS="$2"; shift 2 ;;
    -r | --release) RELEASE="$2"; shift 2 ;;
    --context) CONTEXT="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --decision-log) DLOG="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    -h | --help) usage 0 ;;
    now | list | restore) CMD="$1"; shift ;;
    -*) die "unknown option $1" ;;
    *)
      if [[ "$CMD" == restore && -z "$ID" ]]; then ID="$1"; shift; else die "unexpected argument $1"; fi ;;
  esac
done
[[ -n "$CMD" ]] || usage
command -v kubectl >/dev/null || die "kubectl is required"
command -v jq >/dev/null || die "jq is required"

k() {
  local args=()
  [[ -n "$CONTEXT" ]] && args+=(--context "$CONTEXT")
  [[ -n "$NS" ]] && args+=(-n "$NS")
  kubectl "${args[@]}" "$@"
}

sel="app.kubernetes.io/instance=$RELEASE"
CRON="$(k get cronjob -l "$sel,app.kubernetes.io/component=backup" -o name | head -1)"
[[ -n "$CRON" ]] || die "no backup CronJob for release $RELEASE (is backup.enabled on?)"
CRON="${CRON#*/}"
stamp="$(date -u +%Y%m%d%H%M%S)"

# run_job <name> <identity secret or ""> <args...>: a Job from the CronJob's template; waits; prints its log.
run_job() {
  local name="$1" secret="$2"; shift 2
  local args; args="$(printf '%s\n' "$@" | jq -R . | jq -s .)"
  k get cronjob "$CRON" -o json | jq --arg name "$name" --argjson args "$args" --arg secret "$secret" '
    .spec.jobTemplate as $t
    | {apiVersion: "batch/v1", kind: "Job",
       metadata: {name: $name, labels: ($t.metadata.labels // {})},
       spec: ($t.spec | .backoffLimit = 0 | .ttlSecondsAfterFinished = 86400
         | .template.spec.containers[0].args = $args
         | if $secret == "" then . else
             .template.spec.volumes += [{name: "identity", secret: {secretName: $secret, defaultMode: 288}}]
             | .template.spec.containers[0].volumeMounts += [{name: "identity", mountPath: "/etc/veromesh-identity", readOnly: true}]
             | .template.spec.containers[0].env += [{name: "AGE_IDENTITY_FILE", value: "/etc/veromesh-identity/identity"}]
           end)}' | k create -f - >/dev/null
  echo ">> Job $name started"
  local status=""
  local deadline=$((SECONDS + $(duration_seconds "$TIMEOUT")))
  while ((SECONDS < deadline)); do
    status="$(k get job "$name" -o jsonpath='{.status.succeeded}/{.status.failed}')"
    [[ "$status" == 1/* || "$status" == */1 ]] && break
    sleep 5
  done
  k logs "job/$name" --all-containers 2>/dev/null || true
  [[ "$status" == 1/* ]] || { echo "!! Job $name did not succeed (kubectl describe job/$name)" >&2; return 1; }
}

duration_seconds() {
  case "$1" in
    *h) echo $((${1%h} * 3600)) ;;
    *m) echo $((${1%m} * 60)) ;;
    *s) echo "${1%s}" ;;
    *) echo "$1" ;;
  esac
}

case "$CMD" in
  now) run_job "$CRON-manual-$stamp" "" backup ;;
  list) run_job "$CRON-list-$stamp" "" list ;;
  restore)
    [[ -n "$ID" ]] || die "which backup? scripts/veromesh-backup.sh list shows them"
    [[ "$DLOG" =~ ^(auto|skip|replace)$ ]] || die "--decision-log must be auto, skip or replace"
    secret=""
    if [[ -n "$IDENTITY" ]]; then
      [[ -s "$IDENTITY" ]] || die "no private key at $IDENTITY"
    fi
    API="$(k get deployment -l "$sel,app.kubernetes.io/component=api" -o name | head -1)"
    [[ -n "$API" ]] || die "no VeroMesh service deployment for release $RELEASE"
    echo "This replaces the knowledge graph of release $RELEASE with backup $ID."
    [[ "$DLOG" == replace ]] && echo "It also REPLACES the decision log: decisions recorded after $ID will be lost."
    echo "The VeroMesh service is stopped while it runs."
    if [[ -z "$YES" ]]; then
      read -r -p "Type the backup id to go ahead: " answer
      [[ "$answer" == "$ID" ]] || die "not confirmed; nothing changed"
    fi
    replicas="$(k get "$API" -o jsonpath='{.spec.replicas}')"
    cleanup() {
      [[ -n "$secret" ]] && k delete secret "$secret" --ignore-not-found >/dev/null || true
      echo ">> Starting the VeroMesh service again ($replicas replicas)"
      k scale "$API" --replicas "$replicas" >/dev/null
      k rollout status "$API" --timeout 10m || true
    }
    trap cleanup EXIT
    if [[ -n "$IDENTITY" ]]; then
      secret="$CRON-identity-$stamp"
      k create secret generic "$secret" --from-file=identity="$IDENTITY" >/dev/null
    fi
    echo ">> Stopping the VeroMesh service"
    k scale "$API" --replicas 0 >/dev/null
    k wait --for=delete pod -l "$sel,app.kubernetes.io/component=api" --timeout 5m >/dev/null 2>&1 || true
    run_job "$CRON-restore-$stamp" "$secret" restore "$ID" --decision-log "$DLOG"
    echo ">> Restored $ID. Check the decision log's hash chain: GET /api/v1/decisions/verify"
    ;;
esac
