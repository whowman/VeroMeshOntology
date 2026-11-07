#!/usr/bin/env bash
# Install the VeroMesh chart into a throwaway kind cluster and prove it works:
#   1. images build and load; the release installs and becomes ready
#   2. helm test passes (health, schema, knowledge graph reachable)
#   3. the worked example loads through the API and commits
#   4. the model survives a Fuseki pod restart (persistent volume)
#   5. a pod that is not the service cannot reach Fuseki (NetworkPolicy)
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
k() { kubectl --context "$CTX" -n "$NS" "$@"; }

created=""
PF_PID=""
cleanup() {
  local rc=$?
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  if (( rc != 0 )); then
    echo "!! Failed; cluster state follows" >&2
    k get pods,pvc,svc -o wide >&2 || true
    k describe pods >"$RESULTS/k8s-describe.txt" 2>&1 || true
    for p in $(k get pods -o name 2>/dev/null); do
      echo "---- $p" >>"$RESULTS/k8s-logs.txt"; k logs "$p" --all-containers >>"$RESULTS/k8s-logs.txt" 2>&1 || true
    done
    echo "!! Pod descriptions and logs: $RESULTS/k8s-describe.txt, $RESULTS/k8s-logs.txt" >&2
  fi
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

echo ">> Installing the chart"
helm --kube-context "$CTX" upgrade --install "$RELEASE" "$ROOT/deploy/helm/veromesh" \
  -n "$NS" --create-namespace -f "$ROOT/deploy/helm/veromesh/ci/kind-values.yaml" \
  --wait --timeout 8m

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

echo ">> Restarting Fuseki: the model must survive"
k delete pod "$RELEASE-fuseki-0" --wait=true >/dev/null
k rollout status "statefulset/$RELEASE-fuseki" --timeout 5m
python3 "$ROOT/tests/k8s/smoke.py" http://127.0.0.1:18700 check

echo ">> NetworkPolicy: other pods cannot reach Fuseki"
k delete pod np-probe --ignore-not-found >/dev/null
k run np-probe --restart=Never --image "veromesh/service:test" --image-pull-policy=Never \
  --command -- python -c "import urllib.request; urllib.request.urlopen('http://$RELEASE-fuseki:3030/\$/ping', timeout=10); print('REACHED')" >/dev/null
for _ in $(seq 1 60); do
  phase="$(k get pod np-probe -o jsonpath='{.status.phase}')"
  [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
  sleep 2
done
if [[ "$phase" != Failed ]]; then
  echo "!! a pod without the service's labels reached Fuseki (phase $phase)" >&2
  k logs np-probe >&2 || true
  exit 1
fi
k delete pod np-probe >/dev/null
echo ">> Fuseki refused the probe pod, as intended"

echo ">> k8s smoke test passed"
