# VeroMesh on Kubernetes

Everything runs in the customer's own cluster. Three Helm releases, installed in order:

| Release | Chart | What it runs | Values |
| --- | --- | --- | --- |
| `prerequisites` | DataHub's official `datahub-prerequisites` | PostgreSQL, OpenSearch, Kafka (also VeroMesh's change log) | [datahub/prerequisites-values.yaml](datahub/prerequisites-values.yaml) |
| `datahub` | DataHub's official `datahub` | DataHub GMS, UI, Actions, with the VeroMesh model plugin and theme | [datahub/datahub-values.yaml](datahub/datahub-values.yaml) |
| `veromesh` | [veromesh/](veromesh/) (this repository) | VeroMesh service and its knowledge graph (Apache Jena Fuseki: a writer and read replicas) | [veromesh/values.yaml](veromesh/values.yaml) |

DataHub runs unmodified from its own charts; the overlays only add our plugin, the branded frontend image and the theme settings, the same way `scripts/dev-up.sh` does for the local stack. Chart versions are pinned in [deploy/versions.env](../versions.env) (`DATAHUB_CHART_VERSION`, `DATAHUB_PREREQUISITES_CHART_VERSION`) and match `DATAHUB_VERSION`.

Status: Phase 0. It installs and passes its tests on a kind cluster in CI, including Fuseki replication over Kafka (see [Replication](#replication)). Not yet included: sign-in through Keycloak and secrets from OpenBao (Phase 1), and the scheduled connector jobs.

## 1. Images

```bash
deploy/helm/build-images.sh                                  # build all five
REGISTRY=registry.example.com deploy/helm/build-images.sh --push
```

| Image | Used by |
| --- | --- |
| `veromesh/service:<service version>` | `veromesh` chart, `api.image` |
| `veromesh/fuseki:<Jena version>` | `veromesh` chart, `fuseki.image` (writer and replicas) |
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
| `fuseki.replication.enabled` | on | a writer plus read replicas, kept in step through Kafka; off: one Fuseki |
| `fuseki.replication.replicas` | `2` | read replicas besides the writer; each holds a full copy |
| `fuseki.replication.kafka.bootstrapServers` | `prerequisites-kafka:9092` | DataHub's prerequisites Kafka; or your own |
| `fuseki.replication.kafka.topic` | `veromesh.graph` | the change log; one per install |
| `fuseki.replication.kafka.existingSecret` | empty | Secret with `kafka.properties` (TLS, SASL settings) |
| `networkPolicy.enabled` | on | only the service may reach the writer and the replicas, which have no authentication of their own |

Examples of complete value sets are in [veromesh/ci/](veromesh/ci/); `production-values.yaml` shows a production shape (token Secret, ingress with TLS, private registry, storage class).

### Replication

```
VeroMesh service ── writes and reads ──▶ writer Fuseki ── each commit, as an RDF Patch ──▶ Kafka topic
       │                                                   (Kafka transaction, before      veromesh.graph
       │                                                    the local commit)                    │
       └── reads while the writer is down ──▶ replica Fusekis (2+) ◀── applied in order ─────────┘
                                                                        (Fuseki-Kafka)
```

- **One writer.** The StatefulSet `veromesh-fuseki` runs one Fuseki that takes every write. Our Jena module ([deploy/fuseki/replication](../fuseki/replication/README.md)) publishes each committed change to the topic before committing it locally, so a change that cannot be published is not made.
- **Read replicas.** `veromesh-fuseki-replica-<n>` are read-only and apply the topic with [Fuseki-Kafka](https://github.com/telicent-oss/jena-fuseki-kafka) (Telicent, Apache-2.0). Each has its own volume and copy of the model.
- **The service** writes to the writer and normally reads from it too, so it always sees its own writes. If the writer is unreachable or answers 502/503/504, reads go to the replicas (`veromesh-fuseki-replicas`), which may be a moment behind. Writes are not redirected; they fail until the writer is back, typically within a minute, since Kubernetes restarts it.
- **Kafka is the record.** The writer creates the topic with one partition (order matters) and no retention limit, because a new or rebuilt replica replays it from the start. Do not delete the topic or shorten its retention. Kafka must allow transactions; on a single broker that takes `transaction.state.log.replication.factor=1` and `transaction.state.log.min.isr=1`, which [prerequisites-values.yaml](datahub/prerequisites-values.yaml) sets.

| What happens | Effect |
| --- | --- |
| Writer restarts | Reads continue from the replicas; writes fail until it is back. It catches up from Kafka before serving, including a change it published just before stopping. |
| A replica restarts | The others keep serving; it resumes from where it stopped. |
| A replica's volume is lost or deleted | It rebuilds from the start of the topic. |
| Kafka is down | Reads continue everywhere; writes fail. |
| Replication switched on for an existing single-node install | The writer keeps its volume, finds the topic empty and seeds it from the existing model; the replicas then copy it. |

Writes are not slowed by the replicas, which apply changes in batches, usually within a second or two. A single large write, such as a big workbook load, is split into several Kafka messages within one Kafka transaction. A replica can briefly show part of it before it has applied the rest.

To turn replication off (one Fuseki, no Kafka), use `--set fuseki.replication.enabled=false`, as in [ci/single-node-values.yaml](veromesh/ci/single-node-values.yaml).

### Data and upgrades

The model lives on the PersistentVolumeClaim `data-veromesh-fuseki-0` (the writer), with a copy on each `data-veromesh-fuseki-replica-<n>`. These are kept on `helm uninstall`; delete them to discard the model, and delete the Kafka topic too, or the next install's writer refuses to start: its volume would be empty while the topic holds changes. Upgrading from chart 0.1.0 (a single Fuseki) to 0.2.0 switches replication on as described above. Snapshots that would let the topic be compacted are not built yet, so the topic grows with every change. That is fine for models of this size (about 1.1 million triples for 50,000 people), but worth watching.

## Tests

`scripts/test.sh helm` lints and renders the chart for every value set, checks the manifests against the Kubernetes schemas, and renders the DataHub overlays against DataHub's official charts at the pinned versions. `scripts/test.sh k8s` installs the chart into a throwaway kind cluster alongside a single-node Kafka. It runs `helm test`, loads the worked example and checks that both replicas hold exactly what the writer holds. It then takes the writer down and reads through the API (served by the replicas), restarts the writer and writes again, rebuilds a replica from an empty volume, and checks that other pods cannot reach the writer or the replicas. `scripts/test.sh fuseki-replication` runs the writer module's own tests. CI runs all three; see [docs/testing.md](../../docs/testing.md).
