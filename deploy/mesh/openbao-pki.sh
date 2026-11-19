#!/bin/sh
# Set up OpenBao as the certificate authority of the VeroMesh mesh, and let
# cert-manager ask it for certificates. Run once, with the bao CLI, against the
# customer's OpenBao (BAO_ADDR, and BAO_TOKEN with rights to configure it):
#
#   deploy/mesh/openbao-pki.sh > mesh-root.pem                      # OpenBao holds the root
#   MESH_CSR_OUT=mesh.csr deploy/mesh/openbao-pki.sh                 # or: an intermediate under the
#   MESH_SIGNED_CERT=signed.pem deploy/mesh/openbao-pki.sh > mesh-root.pem   # customer's own root
#
# Prints the mesh's root certificate (the trust anchor istio-csr hands to every
# workload) on stdout; progress goes to stderr.
#
#   PKI_PATH        mount of the mesh CA (default pki_mesh)
#   TRUST_DOMAIN    Istio's trust domain (default cluster.local)
#   CA_NAME         common name of the CA (default "VeroMesh mesh CA")
#   ISSUER_NS / ISSUER_SA / ISSUER_NAME   the cert-manager Issuer and the service account
#                   it signs in with (default istio-system / openbao-issuer / openbao-mesh)
#   K8S_AUTH_PATH   OpenBao's Kubernetes auth mount (default kubernetes); set
#                   K8S_HOST when OpenBao runs outside this cluster
#   MESH_CSR_OUT    write an intermediate CSR here (for the customer's root CA to sign), then stop
#   MESH_SIGNED_CERT   the signed intermediate (with the chain up to the root) to install
#   ROOT_CERT       with MESH_SIGNED_CERT: the customer's root certificate (the trust anchor)
set -eu

PKI="${PKI_PATH:-pki_mesh}"
TD="${TRUST_DOMAIN:-cluster.local}"
CA_NAME="${CA_NAME:-VeroMesh mesh CA}"
NS="${ISSUER_NS:-istio-system}"
SA="${ISSUER_SA:-openbao-issuer}"
ISSUER="${ISSUER_NAME:-openbao-mesh}"
AUTH="${K8S_AUTH_PATH:-kubernetes}"
say() { echo ">> $*" >&2; }

if ! bao secrets list -format=json | grep -q "\"$PKI/\""; then
  say "Enabling the PKI engine at $PKI/"
  bao secrets enable -path="$PKI" pki >&2
  bao secrets tune -max-lease-ttl=87600h "$PKI" >&2
fi

if [ -n "${MESH_CSR_OUT:-}" ]; then
  say "Intermediate CA: writing the request to $MESH_CSR_OUT for the customer's root CA to sign"
  bao write -field=csr "$PKI/intermediate/generate/internal" \
    common_name="$CA_NAME (intermediate)" key_type=ec key_bits=256 >"$MESH_CSR_OUT"
  say "Sign it (CA:TRUE, pathlen 0), then run again with MESH_SIGNED_CERT and ROOT_CERT"
  exit 0
elif [ -n "${MESH_SIGNED_CERT:-}" ]; then
  say "Installing the signed intermediate"
  bao write "$PKI/intermediate/set-signed" certificate=@"$MESH_SIGNED_CERT" >&2
  root="$(cat "${ROOT_CERT:?set ROOT_CERT to the root certificate of the customer CA}")"
elif [ -z "$(bao read -field=certificate "$PKI/cert/ca" 2>/dev/null || true)" ]; then
  say "Generating the mesh's root CA in OpenBao (its key never leaves OpenBao)"
  bao write -field=certificate "$PKI/root/generate/internal" \
    common_name="$CA_NAME" key_type=ec key_bits=256 ttl=87600h >/dev/null
fi

say "Role 'istio': workload certificates for SPIFFE identities in $TD, and istiod's serving certificate"
bao write "$PKI/roles/istio" \
  allow_any_name=true enforce_hostnames=false require_cn=false \
  allowed_uri_sans="spiffe://$TD/*" use_csr_common_name=true use_csr_sans=true \
  key_type=any server_flag=true client_flag=true ttl=24h max_ttl=72h >&2

say "Policy and Kubernetes sign-in for cert-manager's Issuer $NS/$ISSUER"
printf 'path "%s/sign/istio" { capabilities = ["create", "update"] }\n' "$PKI" \
  | bao policy write cert-manager-mesh - >&2
if ! bao auth list -format=json | grep -q "\"$AUTH/\""; then
  bao auth enable -path="$AUTH" kubernetes >&2
fi
bao write "auth/$AUTH/config" kubernetes_host="${K8S_HOST:-https://kubernetes.default.svc}" >&2
bao write "auth/$AUTH/role/cert-manager-mesh" \
  bound_service_account_names="$SA" bound_service_account_namespaces="$NS" \
  audience="vault://$NS/$ISSUER" token_policies=cert-manager-mesh token_ttl=10m >&2

if [ -z "${root:-}" ]; then
  root="$(bao read -field=certificate "$PKI/cert/ca")"
fi
[ -n "$root" ] || { echo "openbao-pki.sh: no CA certificate in $PKI" >&2; exit 1; }
say "Done. The mesh root certificate follows (store it as the istio-root-ca Secret)."
printf '%s\n' "$root"
