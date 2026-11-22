# Architecture

Veromesh adds the organizational ontology beside DataHub without forking it.

```
  people (steward screen /steward)   agents (MCP /mcp)   connectors, spreadsheets
                     │                      │                     │
                     └──── OpenID Connect sign-in (Entra ID first, any provider) ───┘
                                            ▼
                 ┌────────────────────────────────────────────────┐
                 │   Veromesh service (FastAPI)                    │
                 │   model API · changesets · provenance           │
                 │   decision check · follow-up · measures         │
                 └──────┬──────────────────┬──────────────┬───────┘
          SPARQL 1.1    │                  │ SQL          │  DataHub SDK
                        ▼                  ▼              ▼
          ┌───────────────────────┐ ┌──────────────┐ ┌──────────────────────────────────┐
          │ Jena Fuseki           │ │ PostgreSQL   │ │ DataHub v1.8.0 (unmodified)       │
          │ writer + read replicas│ │ decision log │ │  GMS + frontend + actions         │
          │ (Kafka) · model,      │ │ (append-only,│ │  + veromesh-ontology-model plugin │
          │ sources, bindings     │ │  hash chain) │ │  glossary · bindings · products   │
          └───────────────────────┘ └──────────────┘ └──────────────────────────────────┘
```

## Responsibilities

| Concern | Lives in | Why |
| --- | --- | --- |
| Ontology classes, properties, hierarchy, axioms | Apache Jena Fuseki, TDB2 storage (one named graph per ontology) | OWL/RDF, SPARQL and reasoning need a triple store; DataHub has none |
| Data assets, lineage, ownership, schemas | DataHub | That's what DataHub is for |
| Business vocabulary for search and the UI | DataHub glossary (published from the ontology) | People find concepts where they already look |
| Asset ↔ class bindings | Both: `https://veromesh.ai/graph/bindings` graph **and** DataHub (`veromeshOntologyBindings` aspect + `glossaryTerms`) | SPARQL can answer "all assets that hold any kind of Party"; DataHub shows the concept on the asset page |
| The organizational model and where each fact came from | Fuseki: one named graph per source, an effective graph, attestations | Precedence between sources and owner attestation need per-source claims |
| Decisions about agents' actions, overrides and outcomes | PostgreSQL, the service's own database (`veromesh_decision_log`) | Append-only with a hash chain; auditors need it tamper-evident |
| Data products' organizational context in the catalog | DataHub data product custom properties (`veromesh.*`) | People see owner roles and consumers where they browse data |
| Sign-in | The customer's OpenID Connect provider | No user store of our own; roles come from the token |
| Backups | A nightly CronJob: the graph as N-Quads and the decision log as `pg_dump`, encrypted with age to the customer's keys, to object storage (`deploy/helm/README.md`, "Backups and restore") | Only the customer can read or restore them; a restore goes through the writer, so the replicas follow |
| Encryption in transit | Istio ambient mesh (`mesh.enabled`): mutual TLS between pods, certificates from cert-manager signed by the customer's OpenBao (`deploy/mesh/README.md`) | One place for every certificate; Fuseki reachable only by the service's and the backup job's identities |

## The two ontology modes

- **Glossary in the catalog:** `POST /glossary/publish` turns every `owl:Class` in an ontology into a DataHub glossary term under *Veromesh Ontology → \<ontology name\>* (one node per ontology; `rdfs:subClassOf` stays in the triple store). Binding an asset also attaches the term, so it appears in DataHub search, filters and lineage views. This is a business glossary in the catalog, not a semantic layer: DataHub does not define metrics or the meaning of data. Where a customer has a semantic layer, it is layer 2 of the reference architecture and an input to the ontology.
- **Data products in the catalog:** `POST /api/v1/catalog/data-products` (stewards) writes each data product's organizational context onto its DataHub data product (the model's *Catalog URN*), as `veromesh.*` custom properties on the Properties tab: owner and steward roles, capabilities producing and consuming it, KPIs computed from it, agents consuming it, what it feeds and is fed by, systems, data elements, sensitivity and bounding policies. Roles only, never people. Other properties are kept; VeroMesh's are replaced on each publish. `{"ids": [...]}` limits it to some data products; the report lists those published, those whose URN is not in DataHub, and those with no URN.
- **Columns in the catalog:** `POST /api/v1/catalog/columns` (stewards) writes each column binding onto its DataHub column, in the `veromeshOntologyBindings` aspect with relation `holdsDataElement`, so a column's page shows the business data element it holds. Class bindings on the same column are kept. How complete the inventory is: `GET /api/v1/data-elements/coverage` (defined, owned, sourced, classified, bound, attested, per element and in total; `?dataProduct=` for one value stream, `?criticalOnly=true`).
- **Suggestions from the catalog:** a data element's *Catalog terms* name the DataHub glossary terms or tags (often set by a classifier) that mark its columns. `GET /api/v1/data-elements/suggestions` lists marked columns not yet bound; a steward accepts (`.../suggestions/accept`, written to the source `catalog-suggestions`, so it can be attested) or rejects (`.../suggestions/reject`, never suggested again). Nothing is bound without a steward.
- **Formal OWL/RDF:** the ontologies themselves stay in the triple store with full fidelity. `POST /sparql` runs read-only queries across ontologies and bindings; `GET /classes/assets` already follows `rdfs:subClassOf` transitively.

## Custom DataHub aspect

`datahub-model/` defines `veromeshOntologyBindings` (on datasets, schema fields, dashboards, charts and containers). It records class IRI, ontology IRI and version, which glossary terms alone can't carry. It's built as a DataHub custom-model plugin and mounted into GMS from `~/.datahub/plugins/models/` (or a ConfigMap on Kubernetes); see DataHub's `metadata-models-custom` docs.

## Performance

An edit, a decision check or a read takes time that depends on what it touches, not on the size of the model (`docs/testing.md`, "Performance", for the numbers):

- **Validation** checks only the nodes a change can affect: those within three links of a changed triple (any depth along capability nesting), plus the other holders of values that must be unique. The RDFS entailments the shapes need are computed directly, and each SPARQL constraint is parsed once.
- **Changesets** that upsert in merge mode are planned from the entities they touch (`service/src/veromesh/incremental.py`). They re-resolve only those entities, validate the exact change, and write the difference to the source graph, the claims and the effective model. Replace loads, deletes and anything the fast path cannot show to be equivalent take the full path.
- **Reads** are kept in memory until the model changes. Every write records a model version in the store. A replica's own writes are seen at once; another replica's within 0.25 s (`VEROMESH_VERSION_MAX_AGE`): a replica reuses the version it last read for that long, so a decision check usually needs no round trip to Fuseki for it. A commit writes the version once, by compare-and-swap against the store's current version, and carries its state and the readers' lists forward only when nobody else wrote in between. A read that began before a commit and ends after it does not replace what the commit carried forward. A list that must be built while the planner holds the model in memory is built from that, not read from the store. Replicas follow one another the same way: a small commit also writes what it changed (the source triples removed and added, the entities and the claims it wrote or superseded) into a follow graph, keyed by the version it wrote and in the same transaction, and a replica that finds the store at a version it does not hold replays those records onto its own model instead of reading the whole model again. A version with no record (a large load, a replace, a delete, an attestation, or one older than 15 minutes) makes the replica build its model again, as it would have before.

- **Validation focus:** a changed triple (s, p, o) changes s, and changes o only when a shape follows p backwards or the triple changes o's entailed types. A value many entities share, such as a sensitivity level, does not pull them all in.
- **Loads:** replace loads (what batch connectors send), deletes and removed entities take the fast path too. A load costs about 2 to 4 ms per new entity, mostly SHACL, whatever the size of the model. A nightly sync that changes little costs reading the batch.
- **Round trips:** a small commit (an edit) goes to the store as one update request, one transaction on Fuseki; the ontology files are uploaded only when they change. Larger loads use bulk uploads. An edit through the API makes two writes in all.
- **Warnings:** they are reported for the entities a changeset changes, not every entity it sends, so an unchanged re-sync is quiet.
- **Memory and pauses:** the model in memory is held as plain strings (`compactgraph.py`, an rdflib store), which Python's garbage collector does not walk; library objects are frozen once the service has started. A full collection at 100,000 people stops the service for at most about 1.5 s, where it was 16 to 22 s.
- **Decision log:** each write is two round trips to PostgreSQL, and concurrent writes are committed together. The connections send without Nagle's delay, which had held every round trip about 40 ms under load.
- **Validation in a worker process:** an edit's SHACL validation runs in `python -m veromesh.validation_worker`, started with the service, so decision checks do not take turns with it for Python's interpreter (`VEROMESH_VALIDATION_WORKER`).
- **Large reads a row at a time:** reading the source graphs, the effective model, claims, attestations or a changeset's items asks Fuseki for SPARQL TSV and decodes it line by line, so other threads run while a large model is read.
- **Re-sync:** a connector's whole load is written as N-Triples text and compared with the source graph as encoded strings; only what changed is decoded.

## Next steps

1. **DataHub → Veromesh events.** Subscribe to DataHub's change stream (DataHub Actions framework, Apache-2.0) to keep bindings in sync when someone edits glossary terms in the DataHub UI, and to remove bindings for deleted assets.
2. **Reasoning.** Add an OWL 2 RL materialization step (e.g. owlrl, W3C-licensed) or plug in a reasoner when ontologies need more than subclass closure.
3. **Auth.** Done for the API: tokens from any OpenID Connect provider, Entra ID first, with five roles (`service/README.md`, "Sign-in"). Readers and agents get need-to-know answers: roles and limits, not personal details. Attestations and corrections are recorded as the signed-in person. An agent's sign-in is linked to its Agent in the model by its client ID (`GET /api/v1/me`). Decisions are recorded in an append-only, hash-chained log (`service/README.md`, "Decision log"). The decision check (`POST /api/v1/decisions/check`) answers allow, consult or refuse for an agent's action and writes to that log. Agents reach it through MCP at `/mcp` (docs/copilot-studio.md). People answer and overrule decisions, agents report outcomes, and the first measures come from the log (`service/README.md`, "After a decision"). People answer on the steward screen (`/steward`). Next: DataHub calls made as the caller rather than with a service token.
4. **Ontology versioning.** Store `owl:versionInfo` on bindings and add a diff/impact endpoint ("which assets are affected if this class changes").
5. **Production deploy.** Done as charts: DataHub's official Helm charts (Apache-2.0) plus a Veromesh chart for the service and Fuseki, with Fuseki replicated through Kafka (one writer, read replicas; see deploy/helm/README.md), nightly backups encrypted to the customer's keys with a tested restore, encryption at rest with the customer's keys (storage classes, managed databases), and encryption in transit through Istio's ambient mesh with certificates from OpenBao. Field-level encryption of personal data is in the backlog (issue #35). Not yet installed anywhere but kind in CI and local Docker; the first cloud install (Azure, beside the assessment) is what the Copilot Studio test waits for (issue #19).
6. **Enforcement.** The decision check is advisory: an agent could skip it. A gateway in front of target systems, and optionally refusing agents with no current approved AI use case, come next.
