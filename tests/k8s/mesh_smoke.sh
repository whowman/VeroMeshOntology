#!/usr/bin/env bash
# Encryption in transit, end to end, in a throwaway kind cluster: the setup in
# deploy/mesh/ (OpenBao as the CA, cert-manager and istio-csr issuing, Istio in
# ambient mode) and the chart with mesh.enabled. Proves:
#   1. OpenBao, configured by deploy/mesh/openbao-pki.sh, signs for cert-manager,
#      which signs in with Kubernetes auth (deploy/mesh/openbao-issuer.yaml)
#   2. Istio runs without its own CA: istiod's and every workload's certificate
#      comes from istio-csr; the service's identity certificate is signed by OpenBao
#   3. the chart installs in the mesh and helm test passes through it
#   4. the backup job reaches Fuseki through the mesh and takes a backup
#   5. a pod outside the mesh cannot reach the service (mutual TLS only)
#   6. a pod in the mesh with another identity cannot reach Fuseki, with the
#      NetworkPolicy off so the refusal is the mesh's authorization by identity;
#      it can still reach the service
#
# Run with scripts/test.sh k8s-mesh. Needs Docker, kind, kubectl, Helm, openssl
# and internet access (charts and images of Istio, cert-manager and OpenBao).
#   KIND_CLUSTER=name   cluster to use (created if missing; default veromesh-mesh)
#   KEEP_CLUSTER=1      leave the cluster running afterwards
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLUSTER="${KIND_CLUSTER:-veromesh-mesh}"
NS=veromesh
RELEASE=veromesh
RESULTS="${1:-$ROOT/.build/test-results}"
mkdir -p "$RESULTS"
CTX="kind-$CLUSTER"
MESH="$ROOT/deploy/mesh"
ISTIO_REPO=https://istio-release.storage.googleapis.com/charts
JETSTACK_REPO=https://charts.jetstack.io
OPENBAO_REPO=https://openbao.github.io/openbao-helm
# shellcheck source=../../deploy/versions.env
source "$ROOT/deploy/versions.env"
kc() { kubectl --context "$CTX" "$@"; }
k() { kc -n "$NS" "$@"; }
h() { helm --kube-context "$CTX" "$@"; }
WORK="$(mktemp -d)"

created=""
cleanup() {
  local rc=$?
  if (( rc != 0 )); then
    echo "!! Failed; cluster state follows" >&2
    kc get pods -A -o wide >&2 || true
    kc get issuers,certificates,certificaterequests -A >&2 || true
    {
      for ns in istio-system cert-manager openbao "$NS"; do
        for p in $(kc -n "$ns" get pods -o name 2>/dev/null); do
          echo "---- $ns/$p"; kc -n "$ns" logs "$p" --all-containers --tail 200 2>&1 || true
        done
      done
    } >"$RESULTS/k8s-mesh-logs.txt"
    kc describe pods -A >"$RESULTS/k8s-mesh-describe.txt" 2>&1 || true
    echo "!! Logs: $RESULTS/k8s-mesh-logs.txt" >&2
  fi
  rm -rf "$WORK"
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

echo ">> 1. OpenBao (dev mode, test only) as the mesh's certificate authority"
h upgrade --install openbao openbao --repo "$OPENBAO_REPO" --version "$OPENBAO_CHART_VERSION" \
  -n openbao --create-namespace --set server.dev.enabled=true --set server.dev.devRootToken=root \
  --set injector.enabled=false --wait --timeout 5m >/dev/null
kc -n openbao wait --for=condition=Ready pod/openbao-0 --timeout 3m >/dev/null
kc -n openbao exec -i openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=root CA_NAME="VeroMesh mesh CA (test)" \
  sh -s <"$MESH/openbao-pki.sh" >"$WORK/mesh-root.pem" 2>"$RESULTS/k8s-mesh-openbao.log" \
  || { cat "$RESULTS/k8s-mesh-openbao.log" >&2; exit 1; }
openssl x509 -in "$WORK/mesh-root.pem" -noout -subject

echo ">> cert-manager, and the openbao-mesh Issuer (Kubernetes auth)"
h upgrade --install cert-manager cert-manager --repo "$JETSTACK_REPO" --version "$CERT_MANAGER_VERSION" \
  -n cert-manager --create-namespace --set crds.enabled=true --wait --timeout 5m >/dev/null
kc create namespace istio-system --dry-run=client -o yaml | kc apply -f - >/dev/null
# The test's OpenBao serves plain HTTP (dev mode), so no TLS CA bundle.
sed -e 's|https://openbao.openbao.svc:8200|http://openbao.openbao.svc:8200|' \
    -e '/caBundleSecretRef:/,/key: ca.crt/d' "$MESH/openbao-issuer.yaml" | kc apply -f - >/dev/null
kc -n istio-system wait --for=condition=Ready issuer/openbao-mesh --timeout 2m

echo ">> istio-csr, trusting only the mesh root"
kc -n cert-manager create secret generic istio-root-ca --from-file=ca.pem="$WORK/mesh-root.pem" \
  --dry-run=client -o yaml | kc apply -f - >/dev/null
h upgrade --install cert-manager-istio-csr cert-manager-istio-csr --repo "$JETSTACK_REPO" \
  --version "$ISTIO_CSR_VERSION" -n cert-manager -f "$MESH/istio-csr-values.yaml" \
  --set app.certmanager.preserveCertificateRequests=true --wait --timeout 5m >/dev/null

echo ">> 2. Istio $ISTIO_VERSION in ambient mode, without its own CA"
if ! helm show chart base --repo "$ISTIO_REPO" --version "$ISTIO_VERSION" >/dev/null 2>&1; then
  echo "!! Istio chart $ISTIO_VERSION is not in $ISTIO_REPO; the newest there:" >&2
  helm repo add istio-test "$ISTIO_REPO" >/dev/null 2>&1 && helm repo update istio-test >/dev/null 2>&1
  helm search repo istio-test/base --versions 2>&1 | head -12 >&2
  exit 1
fi
h upgrade --install istio-base base --repo "$ISTIO_REPO" --version "$ISTIO_VERSION" -n istio-system --wait >/dev/null
h upgrade --install istiod istiod --repo "$ISTIO_REPO" --version "$ISTIO_VERSION" -n istio-system \
  -f "$MESH/istiod-values.yaml" --wait --timeout 5m >/dev/null
h upgrade --install istio-cni cni --repo "$ISTIO_REPO" --version "$ISTIO_VERSION" -n istio-system \
  -f "$MESH/cni-values.yaml" --wait --timeout 5m >/dev/null
h upgrade --install ztunnel ztunnel --repo "$ISTIO_REPO" --version "$ISTIO_VERSION" -n istio-system \
  -f "$MESH/ztunnel-values.yaml" --wait --timeout 5m >/dev/null
if kc -n istio-system get secret istio-ca-secret >/dev/null 2>&1; then
  echo "!! istiod created its own CA (istio-ca-secret); OpenBao must be the only CA" >&2; exit 1
fi
istiod_issuer="$(kc -n istio-system get secret istiod-tls -o jsonpath='{.data.tls\.crt}' | base64 -d \
  | openssl x509 -noout -issuer)"
[[ "$istiod_issuer" == *"VeroMesh mesh CA (test)"* ]] || { echo "!! istiod's certificate: $istiod_issuer" >&2; exit 1; }
echo "   istiod's certificate is signed by OpenBao ($istiod_issuer)"

echo ">> 3. VeroMesh in the mesh (single Fuseki, encrypted backups)"
docker run --rm --entrypoint age-keygen veromesh/backup:test >"$WORK/backup.key" 2>/dev/null
pub="$(sed -n 's/^# public key: //p' "$WORK/backup.key")"
kc create namespace "$NS" --dry-run=client -o yaml | kc apply -f - >/dev/null
k apply -f "$ROOT/tests/k8s/backup-pvc.yaml" >/dev/null
h upgrade --install "$RELEASE" "$ROOT/deploy/helm/veromesh" -n "$NS" --create-namespace \
  -f "$ROOT/deploy/helm/veromesh/ci/kind-values.yaml" \
  --set fuseki.replication.enabled=false --set api.decisionLog.existingSecret= \
  --set mesh.enabled=true --set "backup.encryption.recipients={$pub}" --wait --timeout 10m
h test "$RELEASE" -n "$NS" --logs --timeout 3m

echo ">> The service's identity certificate comes from OpenBao"
want="URI:spiffe://cluster.local/ns/$NS/sa/$RELEASE"
found=""
for _ in $(seq 1 30); do
  for cr in $(kc -n istio-system get certificaterequests -o name); do
    kc -n istio-system get "$cr" -o jsonpath='{.status.certificate}' | base64 -d >"$WORK/cert.pem" 2>/dev/null || continue
    [[ -s "$WORK/cert.pem" ]] || continue
    if openssl x509 -in "$WORK/cert.pem" -noout -ext subjectAltName 2>/dev/null | grep -q "$want\b"; then
      found="$cr"; break 2
    fi
  done
  sleep 2
done
[[ -n "$found" ]] || { echo "!! no certificate for $want among istio-csr's requests" >&2; exit 1; }
issuer="$(openssl x509 -in "$WORK/cert.pem" -noout -issuer)"
[[ "$issuer" == *"VeroMesh mesh CA (test)"* ]] || { echo "!! $want was issued by $issuer" >&2; exit 1; }
openssl verify -CAfile "$WORK/mesh-root.pem" "$WORK/cert.pem" >/dev/null
echo "   $want: issued by $issuer, verifies against the mesh root"

echo ">> 4. The backup job reaches Fuseki through the mesh (and its NetworkPolicy)"
"$ROOT/scripts/veromesh-backup.sh" --context "$CTX" -n "$NS" -r "$RELEASE" --timeout 10m now | tee "$RESULTS/k8s-mesh-backup.log"
grep -q '^BACKUP_ID=' "$RESULTS/k8s-mesh-backup.log"

# probe <name> <in mesh: ambient|none> <service account> <url>: prints REACHED or REFUSED
probe() {
  local name="$1" mode="$2" sa="$3" url="$4" phase=""
  k delete pod "$name" --ignore-not-found --wait=true >/dev/null
  k run "$name" --restart=Never --image veromesh/service:test --image-pull-policy=Never \
    --labels "istio.io/dataplane-mode=$mode" --overrides "{\"spec\":{\"serviceAccountName\":\"$sa\"}}" \
    --command -- python -c "import urllib.request; urllib.request.urlopen('$url', timeout=10); print('REACHED')" >/dev/null
  for _ in $(seq 1 60); do
    phase="$(k get pod "$name" -o jsonpath='{.status.phase}')"
    [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
    sleep 2
  done
  if [[ "$phase" == Succeeded ]]; then echo REACHED; else echo REFUSED; fi
  k delete pod "$name" --ignore-not-found --wait=false >/dev/null
}
k create serviceaccount stranger --dry-run=client -o yaml | k apply -f - >/dev/null
API="http://$RELEASE-api:8700/health"
FUSEKI="http://$RELEASE-fuseki:3030/\$/ping"

echo ">> 5. Outside the mesh: the service refuses plain text"
[[ "$(probe outside none default "$API")" == REFUSED ]] || { echo "!! a pod outside the mesh reached the service" >&2; exit 1; }
echo "   refused, as intended"

echo ">> 6. In the mesh with another identity: the service answers, Fuseki does not"
# NetworkPolicy off, so only the mesh's AuthorizationPolicy stands between the stranger and Fuseki.
h upgrade "$RELEASE" "$ROOT/deploy/helm/veromesh" -n "$NS" --reuse-values --set networkPolicy.enabled=false \
  --wait --timeout 5m >/dev/null
[[ -z "$(k get networkpolicy -o name)" ]] || { echo "!! the NetworkPolicy is still there" >&2; exit 1; }
[[ "$(probe insider ambient stranger "$API")" == REACHED ]] || { echo "!! a pod in the mesh could not reach the service" >&2; exit 1; }
[[ "$(probe intruder ambient stranger "$FUSEKI")" == REFUSED ]] || { echo "!! another identity reached Fuseki" >&2; exit 1; }
echo "   the service answered; Fuseki refused the stranger's identity"

echo ">> k8s mesh test passed"
