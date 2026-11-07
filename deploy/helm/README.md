# VeroMesh on Kubernetes

Everything runs in the customer's own cluster. Three Helm releases, installed in order:

| Release | Chart | What it runs | Values |
| --- | --- | --- | --- |
| `prerequisites` | DataHub's official `datahub-prerequisites` | PostgreSQL, OpenSearch, Kafka | [datahub/prerequisites-values.yaml](datahub/prerequisites-values.yaml) |
| `datahub` | DataHub's official `datahub` | DataHub GMS, UI, Actions, with the VeroMesh model plugin and theme | [datahub/datahub-values.yaml](datahub/datahub-values.yaml) |
| `veromesh` | [veromesh/](veromesh/) (this repository) | VeroMesh service and its knowledge graph (Apache Jena Fuseki) | [veromesh/values.yaml](veromesh/values.yaml) |

DataHub runs unmodified from its own charts; the overlays only add our plugin, the branded frontend image and the theme settings, the same way `scripts/dev-up.sh` does for the local stack. Chart versions are pinned in [deploy/versions.env](../versions.env) (`DATAHUB_CHART_VERSION`, `DATAHUB_PREREQUISITES_CHART_VERSION`) and match `DATAHUB_VERSION`.

Status: a skeleton for Phase 0. It installs and passes its tests on a kind cluster in CI. Not yet included: Fuseki replication with RDF Delta (a separate Phase 0 item), sign-in through Keycloak and secrets from OpenBao (Phase 1), and the scheduled connector jobs.

## 1. Images

```bash
deploy/helm/build-images.sh                                  # build all five
REGISTRY=registry.example.com deploy/helm/build-images.sh --push
```

| Image | Used by |
| --- | --- |
| `veromesh/service:<service version>` | `veromesh` chart, `api.image` |
| `veromesh/fuseki:<Jena version>` | `veromesh` chart, `fuseki.image` |
| `veromesh/connectors:<connectors version>` | connector runs (`kubectl run`, or your scheduler) |
| `veromesh/datahub-frontend-react:<DataHub version>` | `datahub-values.yaml`, `datahub-frontend.image` |
| `veromesh/datahub-model-plugin:<service version>` | `datahub-values.yaml`, GMS init container |

The last two need the DataHub build (JDK 25 or Docker, as for the local stack); `SKIP_DATAHUB=1` builds only the first three. With a private registry, set the image repositories in each values file (and `imagePullSecrets` in the `veromesh` chart).

## 2. DataHub

```bash
source deploy/versions.env
kubectl create namespace veromesh
kubectl -n veromesh create secret generic postgresql-secrets \
  --from-literal=postgres-password="$(openssl rand -hex 16)" \
  --from-literal=replication-password="$(openssl rand -hex 16)" \
  --from-literal=password="$(openssl rand -hex 16)"

helm repo add datahub https://helm.datahubproject.io
helm -n veromesh install prerequisites datahub/datahub-prerequisites \
  --version "$DATAHUB_PREREQUISITES_CHART_VERSION" -f deploy/helm/datahub/prerequisites-values.yaml
helm -n veromesh install datahub datahub/datahub \
  --version "$DATAHUB_CHART_VERSION" -f deploy/helm/datahub/datahub-values.yaml --timeout 20m
```

The prerequisites run as single nodes, which suits a pilot. For production, use managed PostgreSQL, OpenSearch and Kafka: turn them off in `prerequisites-values.yaml` and point DataHub's `global.sql`, `global.elasticsearch` and `global.kafka` settings at them.

Then create a DataHub access token for VeroMesh (Settings > Access Tokens in the DataHub UI) and store it:

```bash
kubectl -n veromesh create secret generic veromesh-datahub --from-literal=token=<token>
```

## 3. VeroMesh

```bash
helm -n veromesh install veromesh deploy/helm/veromesh \
  --set api.datahub.existingSecret=veromesh-datahub
helm -n veromesh test veromesh
kubectl -n veromesh port-forward svc/veromesh-api 8700:8700   # http://localhost:8700/docs
```

With DataHub installed as release `datahub` in the same namespace, the default `api.datahub.gmsUrl` already points at it. Every value is checked against [values.schema.json](veromesh/values.schema.json), so a misspelt key fails the install instead of being ignored. The settings that matter most:

| Value | Default | Notes |
| --- | --- | --- |
| `api.datahub.gmsUrl` | `http://datahub-datahub-gms:8080` | DataHub GMS inside the cluster |
| `api.datahub.existingSecret` | empty | Secret holding the DataHub token (key `token`, or `existingSecretKey`) |
| `api.instanceBase` | `urn:veromesh:org:` | one per customer install |
| `api.ingress.*` | off | host, class, TLS for the API |
| `fuseki.persistence.size` / `storageClass` | `10Gi` / cluster default | the model; about 1.1 million triples for 50,000 people and 5,000 systems |
| `fuseki.javaOpts` | `-Xmx1g` | raise with the model size; keep the memory limit about 1.5× the heap |
| `fuseki.enabled` / `store.url` | Fuseki in the chart | set `fuseki.enabled=false` and `store.url=fuseki:<dataset URL>` to use a store you run |
| `networkPolicy.enabled` | on | only the service may reach Fuseki, which has no authentication of its own |

Examples of complete value sets are in [veromesh/ci/](veromesh/ci/); `production-values.yaml` shows a production shape (token Secret, ingress with TLS, private registry, storage class).

### Data and upgrades

The model lives on the PersistentVolumeClaim `data-veromesh-fuseki-0`. It is kept on `helm uninstall`; delete the claim to discard the model. Fuseki runs as a single replica until replication with RDF Delta lands, so a Fuseki restart briefly makes the API return errors for model reads and writes; nothing is lost.

## Tests

`scripts/test.sh helm` lints and renders the chart for every value set, checks the manifests against the Kubernetes schemas, and renders the DataHub overlays against DataHub's official charts at the pinned versions. `scripts/test.sh k8s` installs the chart into a throwaway kind cluster and runs `helm test`, loads the worked example, restarts Fuseki to prove the model persists, and checks that other pods cannot reach Fuseki. CI runs both; see [docs/testing.md](../../docs/testing.md).
