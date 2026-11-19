# Encryption in transit: Istio ambient, certificates from OpenBao

Every connection between VeroMesh's pods is encrypted and authenticated with mutual TLS, and every certificate involved comes from one place: the customer's OpenBao. Nothing in VeroMesh handles certificates itself.

```
                    the customer's OpenBao (PKI engine: the mesh CA)
                                  ▲  signs, through cert-manager's
                                  │  Issuer (Kubernetes auth)
   cert-manager ── istio-csr ─────┘      ◀── certificate requests from istiod and ztunnel
                                                (one per workload identity)
   istiod (no CA of its own)        ztunnel on every node: wraps each enrolled pod's
                                    traffic in mutual TLS (HBONE, port 15008)

   VeroMesh service ◀──mTLS──▶ Fuseki writer and replicas ◀──mTLS── backup job
```

| Part | What it does | License |
| --- | --- | --- |
| [Istio](https://istio.io) in ambient mode (istiod, the CNI agent, ztunnel) | Mutual TLS between pods without sidecars; who may call whom, by identity | Apache-2.0 |
| [cert-manager](https://cert-manager.io) and [istio-csr](https://cert-manager.io/docs/usage/istio-csr/) | Takes Istio's certificate requests and gets them signed; also the place for every other certificate (ingress, Kafka, PostgreSQL) | Apache-2.0 |
| [OpenBao](https://openbao.org) | The certificate authority (its PKI engine); also the place for secrets and encryption keys | MPL-2.0 |

None of these are VeroMesh code or part of our images: the customer installs them from their own charts, at the versions pinned in [../versions.env](../versions.env) (`ISTIO_VERSION`, `CERT_MANAGER_VERSION`, `ISTIO_CSR_VERSION`, `OPENBAO_CHART_VERSION`). Istio is the open source project with no licence fee on any cloud; a vendor's support subscription is optional.

## Why this design

- **One place for certificates.** cert-manager issues and renews every certificate in the cluster, and OpenBao is the authority behind them. Rotating the CA, revoking, auditing: one place.
- **No code in VeroMesh.** The service, Fuseki and the backup job speak plain HTTP inside their pods; ztunnel encrypts on the way out and decrypts on the way in. Nothing to configure per component, and nothing to get wrong in each one.
- **Identity, not addresses.** Each pod's certificate names its service account (`spiffe://cluster.local/ns/<namespace>/sa/<account>`). Fuseki accepts only the service's and the backup job's identities, on top of the NetworkPolicy.
- **Short-lived certificates.** Workload certificates last 24 hours at most (`max_ttl=72h` in OpenBao) and are renewed automatically.

## Install, in this order

istio-csr has to be running before Istio is installed.

### 1. OpenBao as the certificate authority

On the customer's OpenBao (it can run in the cluster from its own chart, or anywhere the cluster can reach), with the `bao` CLI:

```bash
export BAO_ADDR=https://openbao.example.com:8200 BAO_TOKEN=<token able to configure PKI and auth>

# OpenBao holds the mesh's root (its key never leaves OpenBao):
deploy/mesh/openbao-pki.sh > mesh-root.pem

# Or an intermediate under the customer's own root CA (recommended where one exists):
MESH_CSR_OUT=mesh.csr deploy/mesh/openbao-pki.sh       # have the root CA sign mesh.csr (CA:TRUE, pathlen 0)
MESH_SIGNED_CERT=signed.pem ROOT_CERT=customer-root.pem deploy/mesh/openbao-pki.sh > mesh-root.pem
```

It enables the PKI engine at `pki_mesh/`, creates the role `istio` (SPIFFE identities in the trust domain, and istiod's serving certificate; 24-hour certificates, 72 at most), and lets cert-manager sign in with Kubernetes auth as the service account `istio-system/openbao-issuer`, allowed only to sign with that role. Set `K8S_HOST` if OpenBao runs outside the cluster, and `TRUST_DOMAIN` if Istio's is not `cluster.local`.

For production, OpenBao itself: run it highly available, with auto-unseal through the customer's cloud key service (their key), and audit logging on.

### 2. cert-manager and the Issuer

```bash
source deploy/versions.env
helm install cert-manager cert-manager --repo https://charts.jetstack.io --version "$CERT_MANAGER_VERSION" \
  -n cert-manager --create-namespace --set crds.enabled=true

kubectl create namespace istio-system
kubectl -n istio-system create secret generic openbao-ca --from-file=ca.crt=<CA of OpenBao's own TLS certificate>
# Edit spec.vault.server in the file to the customer's OpenBao first.
kubectl apply -f deploy/mesh/openbao-issuer.yaml
kubectl -n istio-system wait --for=condition=Ready issuer/openbao-mesh
```

### 3. istio-csr

```bash
kubectl -n cert-manager create secret generic istio-root-ca --from-file=ca.pem=mesh-root.pem
helm install cert-manager-istio-csr cert-manager-istio-csr --repo https://charts.jetstack.io \
  --version "$ISTIO_CSR_VERSION" -n cert-manager -f deploy/mesh/istio-csr-values.yaml
```

The root certificate comes from a Secret, not from what the issuer returns, so a compromised issuer cannot quietly change what the mesh trusts. For production, also replace cert-manager's default approver with [approver-policy](https://cert-manager.io/docs/policy/approval/approver-policy/), so only istio-csr's requests for the mesh's identities are signed.

### 4. Istio in ambient mode

```bash
REPO=https://istio-release.storage.googleapis.com/charts
helm install istio-base base --repo $REPO --version "$ISTIO_VERSION" -n istio-system
helm install istiod istiod   --repo $REPO --version "$ISTIO_VERSION" -n istio-system -f deploy/mesh/istiod-values.yaml
helm install istio-cni cni   --repo $REPO --version "$ISTIO_VERSION" -n istio-system -f deploy/mesh/cni-values.yaml
helm install ztunnel ztunnel --repo $REPO --version "$ISTIO_VERSION" -n istio-system -f deploy/mesh/ztunnel-values.yaml
```

istiod runs without its own CA (`ENABLE_CA_SERVER=false`); its serving certificate and every workload's come from istio-csr. If the Secret `istio-system/istio-ca-secret` exists, istiod made a CA of its own and the values were not applied.

On AKS, install Istio this way too (the open source charts). Azure's managed Istio add-on has its own release cadence and its own CA plugin (certificates from Azure Key Vault); check that it supports ambient mode and istio-csr before choosing it over these charts.

### 5. VeroMesh in the mesh

```bash
helm upgrade --install veromesh deploy/helm/veromesh -n veromesh ... --set mesh.enabled=true
```

With `mesh.enabled`:

- every VeroMesh pod (the service, the Fuseki writer and replicas, the backup job, the test pod) carries `istio.io/dataplane-mode=ambient`;
- Fuseki takes mutual TLS only (`PeerAuthentication` STRICT), from the service's and the backup job's identities only (`AuthorizationPolicy`);
- the service takes mutual TLS only (`mesh.apiMtls: STRICT`), so whatever calls it must be in the mesh too: enroll the ingress controller's namespace (`kubectl label namespace <ingress namespace> istio.io/dataplane-mode=ambient`), and the connectors' and agents' namespaces. While they are being enrolled, `mesh.apiMtls: PERMISSIVE` accepts both;
- the NetworkPolicy also admits ztunnel's HBONE port (15008) and Istio's probe address (169.254.7.127).

Enroll DataHub's and Kafka's namespaces the same way (a namespace label), so their traffic is encrypted too. PostgreSQL and Kafka outside the cluster (managed services) are reached over their own TLS: `sslmode=verify-full` in the decision log's URL, and the TLS settings in `fuseki.replication.kafka.existingSecret`.

## Checking it

```bash
# No CA in istiod; the service's certificate is OpenBao's
kubectl -n istio-system get secret istio-ca-secret            # NotFound
istioctl ztunnel-config certificates                         # each identity, its issuer and expiry
```

`scripts/test.sh k8s-mesh` does all of the above in a throwaway kind cluster (OpenBao in dev mode, which is for tests only) and checks that the service's certificate is signed by OpenBao, that a pod outside the mesh cannot reach the service, that a pod with another identity cannot reach Fuseki, and that backups still work through the mesh. CI runs it on every pull request.
