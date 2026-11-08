# Fuseki replication: the writer module

A small Apache Jena module that turns one Fuseki into the **writer** of a replicated knowledge graph. Every committed write is published to a Kafka topic as an [RDF Patch](https://jena.apache.org/documentation/rdf-patch/). Read replicas apply that topic with Telicent's [Fuseki-Kafka](https://github.com/telicent-oss/jena-fuseki-kafka) module. How it fits into a Kubernetes install is described in [deploy/helm/README.md](../../helm/README.md#replication); this page covers the module itself.

## What it does

The module registers a dataset type, `vmr:PublishingDataset`, which wraps a TDB2 dataset. The writer's configuration (rendered by the Helm chart) looks like this:

```turtle
PREFIX vmr:  <https://veromesh.ai/ns/fuseki-replication#>
PREFIX tdb2: <http://jena.apache.org/2016/tdb#>

<#dataset> a vmr:PublishingDataset ;
    vmr:dataset          <#tdb2> ;
    vmr:bootstrapServers "prerequisites-kafka:9092" ;
    vmr:topic            "veromesh.graph" ;
    vmr:stateFile        "/fuseki/databases/writer.state" .

<#tdb2> a tdb2:DatasetTDB2 ; tdb2:location "/fuseki/databases/veromesh" .
```

Optional settings: `vmr:transactionalId` (one per writer), `vmr:chunkBytes` (largest Kafka message; default 512 KiB), `vmr:replicationFactor` (used when the writer creates the topic), `vmr:configFile` (Kafka client properties for TLS or SASL), and `vmr:exitToRecover` (default true; see Failure handling). Any value can be written as `env:{NAME}` or `env:{NAME:default}` to read it from the environment.

**On every write transaction**

1. The changes (quads added and deleted) are collected in a spool file next to the dataset, so large writes do not need memory.
2. On commit, the patch is streamed from the spool file to the topic in one Kafka transaction, with an idempotent producer and `acks=all`. A patch larger than `chunkBytes` is split at line boundaries, and every piece goes into the same Kafka transaction.
3. Only once Kafka has accepted the change is it committed to TDB2. If publishing fails, the local transaction is aborted and the client gets an error.
4. The Kafka offset of the change is recorded in the state file.

The dataset has one writer at a time, so the topic carries every committed write in commit order. Patches carry no `TX`/`TC` lines, so Fuseki-Kafka applies them within its own batches. Replicas read with `isolation.level=read_committed`, so they never see part of a transaction that was aborted.

**When Fuseki starts**, before it serves requests:

| Writer's volume | Topic | What happens |
| --- | --- | --- |
| has a state file | any | applies the changes after the recorded offset (for example, one published just before a crash) |
| empty | any | rebuilds the dataset from the whole topic |
| has data, no state file (or state `-1`) | empty | publishes the dataset to seed the topic (an existing single-node install switching to replication) |
| has data, state `-1` | has changes | replays the topic from the start (a seed or first write was published just before a crash) |
| has data, no state file | has changes | refuses to start: which copy is right is the operator's call |

The state `-1` means "nothing from the topic is known to be applied yet". It is written before serving a fresh install and before seeding, so a crash at either point leaves a state the next start can resume from.

It also creates the topic if it is missing (one partition, kept forever). It fails if the topic has more than one partition or uses `cleanup.policy=compact`, and sets `retention.ms` and `retention.bytes` to `-1` on a topic that limits either. Catch-up gives up after 5 minutes without progress, so Fuseki fails to start rather than hang. Spool files left by a crash are removed. Connecting with the transactional id fences off any older writer and aborts that writer's unfinished transaction.

**Failure handling.**

- A write that fails before it reaches Kafka's commit is not committed; the Kafka transaction is aborted and the client gets an error.
- If Kafka's commit itself fails (a timeout or a lost broker), Kafka may still commit the change. The local write is refused and the writer stops taking writes. With `vmr:exitToRecover` (the default), it then exits, Kubernetes restarts it, and the restart catches up from Kafka, which settles the outcome either way.
- The same applies if the local commit fails after the change was published.
- If another writer has taken over the transactional id (Kafka fences this one), or this writer is not authorised, it stops taking writes and does not exit: restarting would fence the other writer in turn. Reads continue.
- Clearing the whole dataset (a Graph Store Protocol PUT without `?graph=`) is published as deletes.
- A term that UTF-8 cannot carry (an unpaired UTF-16 surrogate) is refused, because the patch could not reproduce it exactly.

Replaying changes the dataset already has does no harm: a patch only adds and deletes specific quads, so replaying the most recent ones onto a dataset that already has them changes nothing. So the state file only needs to be at or behind the dataset, never ahead of it.

## Not replicated

Prefix declarations (queries use full IRIs) and changes made outside Fuseki directly to the TDB2 files, such as a `tdb2.tdbloader` run against the writer's volume. Load data through the service instead.

## Build and test

The module is built inside [../Dockerfile](../Dockerfile), and the build runs its tests. The same Maven build copies the Fuseki server jar and the Fuseki-Kafka module, at the versions pinned in [deploy/versions.env](../../versions.env), into the image.

```bash
scripts/test.sh fuseki-replication          # module tests, in Docker
cd deploy/fuseki/replication && mvn verify  # or directly, with JDK 21 and Maven
```

The tests use Kafka's `MockProducer`/`MockConsumer` and an in-memory Jena dataset. They cover:

- publishing: publish before commit, abort, a failed publish blocking the commit, an unconfirmed Kafka commit, fencing;
- patch content: clearing the dataset, splitting, blank nodes, line breaks in literals, unencodable terms;
- startup: catch-up, seeding, crash recovery at each startup step, and refusing to start. The full path, with a real Kafka, the writer, two replicas and the service, is tested by `scripts/test.sh k8s`.

Apache-2.0. At run time it uses Jena (from the Fuseki server jar) and the Kafka client bundled in the Fuseki-Kafka module, so the jar contains only this module's own classes.
