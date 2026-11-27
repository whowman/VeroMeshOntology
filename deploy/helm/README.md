# VeroMesh on Kubernetes

Everything runs in the customer's own cluster. Three Helm releases, installed in order:

| Release | Chart | What it runs | Values |
| --- | --- | --- | --- |
| `prerequisites` | DataHub's official `datahub-prerequisites` | PostgreSQL, OpenSearch, Kafka (also VeroMesh's change log) | [datahub/prerequisites-values.yaml](datahub/prerequisites-values.yaml) |
| `datahub` | DataHub's official `datahub` | DataHub GMS, UI, Actions, with the VeroMesh model plugin and theme | [datahub/datahub-values.yaml](datahub/datahub-values.yaml) |
| `veromesh` | [veromesh/](veromesh/) (this repository) | VeroMesh service, its knowledge graph (Apache Jena Fuseki: a writer and read replicas) and nightly encrypted backups | [veromesh/values.yaml](veromesh/values.yaml) |

DataHub runs unmodified from its own charts; the overlays only add our plugin, the branded frontend image and the theme settings, the same way `scripts/dev-up.sh` does for the local stack. Chart versions are pinned in [deploy/versions.env](../versions.env) (`DATAHUB_CHART_VERSION`, `DATAHUB_PREREQUISITES_CHART_VERSION`) and match `DATAHUB_VERSION`.

Status: installs and passes its tests on a kind cluster in CI, including Fuseki replication over Kafka (see [Replication](#replication)). The first cloud install, a dev install on Azure beside the assessment, runs from these charts with DataHub's official charts ([deploy/azure](../azure/README.md)); DataHub's prerequisites take a pinned PostgreSQL build and create DataHub's database themselves (`deploy/helm/datahub/prerequisites-values.yaml`). Sign-in: the API takes tokens from any OpenID Connect provider (`api.auth`; Entra ID first), and the service will not start without it unless `api.auth.disabled` is set, which the kind test does. The decision log needs a PostgreSQL database (`api.decisionLog.existingSecret`); without one it is kept in memory. Dates follow `api.timeZone` (default UTC). Backups run nightly, encrypted to the customer's own key ([Backups and restore](#backups-and-restore)); encryption at rest can use the customer's keys and encryption in transit runs through Istio's ambient mesh with certificates from OpenBao ([Encryption](#encryption)). Usage reports are signed with a key the chart makes and keeps (`api.usage`), and the licence comes from `api.licence.existingSecret` or is loaded in the Administration area; without one the install goes read-only after 30 days. Measured at 500,000 people on the Azure reference machines, replication included (docs/testing.md, "Replication under load"). The service builds its planner state in the background as it starts; `/health` reports `planner: warming` until it is `ready`. Notices to owners use `api.publicUrl` for their links (default: the ingress host) and, for Teams, `api.teams.*` (`deploy/teams/README.md`). Not yet included: the service's own secrets from OpenBao, and the scheduled connector jobs.

## 1. Images

```bash
deploy/helm/build-images.sh                                  # build all six
REGISTRY=registry.example.com deploy/helm/build-images.sh --push
```

| Image | Used by |
| --- | --- |
| `veromesh/service:<service version>` | `veromesh` chart, `api.image` |
| `veromesh/fuseki:<Jena version>` | `veromesh` chart, `fuseki.image` (writer and replicas) |
| `veromesh/connectors:<connectors version>` | connector runs (`kubectl run`, or your scheduler) |
| `veromesh/backup:<version in deploy/backup/VERSION>` | `veromesh` chart, `backup.image` (backups and restores) |
| `veromesh/datahub-frontend-react:<DataHub version>` | `datahub-values.yaml`, `datahub-frontend.image` |
| `veromesh/datahub-model-plugin:<service version>` | `datahub-values.yaml`, GMS init container |

The last two need the DataHub build (JDK 25 or Docker, as for the local stack); `SKIP_DATAHUB=1` builds only the first four. With a private registry, set the image repositories in each values file (and `imagePullSecrets` in the `veromesh` chart).

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
age-keygen -o backup-key.txt     # the customer's backup key: keep this file safe, out of the cluster
kubectl -n veromesh create secret generic veromesh-backup-storage --from-file=rclone.conf   # where backups go
helm -n veromesh install veromesh deploy/helm/veromesh \
  --set api.datahub.existingSecret=veromesh-datahub \
  --set "backup.encryption.recipients={$(age-keygen -y backup-key.txt)}" \
  --set backup.rclone.existingSecret=veromesh-backup-storage \
  --set backup.rclone.remote=azure:veromesh-backups/prod
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
| `api.usage.licensedTier` | empty | `small`, `medium`, `large` or `extra-large`: the usage report's share of tier (service/README.md, "Usage report") |
| `api.usage.customerName` / `installId` / `hosting` | empty / generated / `self-hosted` | who the usage report is from |
| `api.usage.signingKey.existingSecret` | empty: the chart makes one | the install's Ed25519 key for signing usage reports; the chart's Secret (`<release>-api-usage-key`) is kept across upgrades and uninstalls |
| `api.usage.mailSecret` | empty | a Secret with `smtp-password` and/or `m365-client-secret` for sending reports; the rest is set on the Administration area's Usage page |
| `api.licence.existingSecret` / `key` | empty / `licence.json` | a Secret with the licence VeroMesh issued; unset, an administrator loads it on the Administration area's Licence page. Without a licence the install runs 30 days, then goes read-only (service/README.md, "Licence") |
| `backup.encryption.recipients` | none: required | the customer's public keys (`age-keygen`, or SSH); backups are encrypted to them |
| `backup.schedule` / `backup.keep` | `17 2 * * *` / `30` | nightly in `api.timeZone`; backups kept |
| `backup.rclone.existingSecret` / `remote` | none: this or a volume is required | off-site: a Secret with `rclone.conf`, and e.g. `azure:veromesh-backups/prod` |
| `backup.volume.existingClaim` | empty | or a PersistentVolumeClaim you create (a pilot; not off-site) |
| `backup.graphFrom` | `writer` | or `replicas`, to spare the writer |
| `networkPolicy.enabled` | on | only the service and the backup job may reach the writer and the replicas, which have no authentication of their own |
| `mesh.enabled` / `mesh.apiMtls` | off / `STRICT` | every pod in Istio's ambient mesh, mutual TLS only ([Encryption](#encryption)) |

Examples of complete value sets are in [veromesh/ci/](veromesh/ci/); `production-values.yaml` shows a production shape (token Secret, ingress with TLS, private registry, storage class, encrypted backups to object storage); `mesh-values.yaml` the mesh.

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

The model lives on the PersistentVolumeClaim `data-veromesh-fuseki-0` (the writer), with a copy on each `data-veromesh-fuseki-replica-<n>`. These are kept on `helm uninstall`; delete them to discard the model, and delete the Kafka topic too, or the next install's writer refuses to start: its volume would be empty while the topic holds changes. Upgrading from chart 0.1.0 (a single Fuseki) to 0.2.0 switches replication on as described above. Chart 0.3.0 adds backups, on by default: an upgrade needs `backup.encryption.recipients` and a destination (`backup.rclone.*` or `backup.volume.existingClaim`), or `backup.enabled=false`; otherwise it stops with a message saying which. Snapshots that would let the topic be compacted are not built yet, so the topic grows with every change. That is fine for models of this size (about 1.1 million triples for 50,000 people), but worth watching.

## Backups and restore

A CronJob (`<release>-backup`, nightly at 02:17 in `api.timeZone`) backs up two things:

- **the knowledge graph:** every quad, read from the writer (or the replicas, `backup.graphFrom`) through Fuseki's Graph Store Protocol, as compressed N-Quads;
- **the decision log:** its database, with `pg_dump` (when `api.decisionLog.existingSecret` is set).

Both are encrypted with [age](https://age-encryption.org) to the customer's public keys (`backup.encryption.recipients`) before they leave the pod. Only someone with a matching private key can read or restore a backup; the private key is never in the cluster except, briefly, during a restore. Each backup is a folder named after its UTC time holding the two files and a `manifest.json` (counts, checksums, versions, the public keys used), written last so a folder without it is unfinished. The newest `backup.keep` (30) are kept.

Where they go, one of two, given at install: for production, `backup.rclone.existingSecret`, a Secret holding an [rclone](https://rclone.org) configuration for the customer's storage (Azure Blob, S3, Google Cloud Storage and others), and `backup.rclone.remote`, the bucket and path. The storage account's own encryption can use the customer's key as well (below). For a pilot, `backup.volume.existingClaim`, a PersistentVolumeClaim you create in the namespace: not off-site, and outside the release so it outlives it. (The chart does not create the claim: on storage classes that bind on first use, as most cloud ones do, an unused claim would hold up `helm install --wait`.)

```bash
scripts/veromesh-backup.sh -n veromesh now                     # a backup now
scripts/veromesh-backup.sh -n veromesh list                    # the backups held
scripts/veromesh-backup.sh -n veromesh restore 20261119T021700Z --identity backup-key.txt
```

A restore, from the operator's machine (kubectl and jq):

1. checks the backup's checksums and that the key opens it, before changing anything;
2. stops the service, so nothing writes meanwhile;
3. replaces the whole knowledge graph through the writer in one transaction (a Graph Store Protocol PUT). The replicas follow through Kafka like after any other write, so the topic keeps being the record;
4. restores the decision log only into an empty database (for example, a new one after the old was lost). The log is append-only, so a database that already holds entries is left alone. `--decision-log replace` overrides that, losing the decisions recorded after the backup; `skip` leaves it out;
5. checks the counts against the manifest, and starts the service again (also if something failed). Then check the log's hash chain: `GET /api/v1/decisions/verify`.

A full rebuild (cluster lost): install the release again with empty volumes and a new Kafka topic, then restore. The restore is the first write, so it is what the new topic starts with.

Watch the CronJob: a failed run leaves a failed Job (`kubectl get jobs -l app.kubernetes.io/component=backup`), which most cluster monitoring already alerts on. Test a restore regularly, into a scratch install.

## Encryption

**At rest, with the customer's keys.** VeroMesh's data lives in four places; each can be encrypted with a key the customer holds in their own key service:

| Where | What | How |
| --- | --- | --- |
| Fuseki's volumes (`fuseki.persistence.storageClass`) | the model | a storage class whose disks use the customer's key: Azure Disk with a disk encryption set (`diskEncryptionSetID`), AWS EBS with `kmsKeyId`, Google Persistent Disk with `disk-encryption-kms-key` |
| The decision log's PostgreSQL | decisions | the managed database's customer-managed key (Azure Database for PostgreSQL, Amazon RDS, Cloud SQL) |
| Backups | both, copied | always encrypted to the customer's own age keys (above); the bucket can also use their key |
| Kafka | the model's change log | the managed service's customer-managed key, or a storage class as for Fuseki |

Without those settings everything is still encrypted at rest by the cloud platform, with the platform's keys. Field-level encryption of personal data inside the model is not done (issue #35).

**In transit.** With `mesh.enabled`, every pod joins Istio's ambient mesh: traffic between them is mutual TLS, Fuseki accepts only the service's and the backup job's identities, and every certificate comes from cert-manager, signed by the customer's OpenBao. Setting that up, in order, is in [../mesh/README.md](../mesh/README.md). Without the mesh, traffic inside the cluster is plain HTTP behind the NetworkPolicy, and TLS starts at the ingress.

## Tests

`scripts/test.sh helm` lints and renders the chart for every value set, checks the manifests against the Kubernetes schemas, and renders the DataHub overlays against DataHub's official charts at the pinned versions. `scripts/test.sh k8s` installs the chart into a throwaway kind cluster alongside a single-node Kafka. It runs `helm test`, loads the worked example and checks that both replicas hold exactly what the writer holds. It then takes the writer down and reads through the API (served by the replicas), restarts the writer and writes again, rebuilds a replica from an empty volume, checks that other pods cannot reach the writer or the replicas, and then takes an encrypted backup, changes the model, restores it (writer and replicas), and restores a lost decision log. `scripts/test.sh backup` tests the backup image against a real Fuseki and PostgreSQL (retention, a wrong key, a damaged backup). `scripts/test.sh k8s-mesh` installs OpenBao, cert-manager, istio-csr and Istio ambient in kind, then the chart in the mesh, and checks the certificates come from OpenBao, plain text is refused, and Fuseki refuses other identities. `scripts/test.sh fuseki-replication` runs the writer module's own tests. CI runs all of them; see [docs/testing.md](../../docs/testing.md).
