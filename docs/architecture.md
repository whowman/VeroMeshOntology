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

## The two ontology modes

- **Glossary in the catalog:** `POST /glossary/publish` turns every `owl:Class` in an ontology into a DataHub glossary term under *Veromesh Ontology → \<ontology name\>* (one node per ontology; `rdfs:subClassOf` stays in the triple store). Binding an asset also attaches the term, so it appears in DataHub search, filters and lineage views. This is a business glossary in the catalog, not a semantic layer: DataHub does not define metrics or the meaning of data. Where a customer has a semantic layer, it is layer 2 of the reference architecture and an input to the ontology.
- **Data products in the catalog:** `POST /api/v1/catalog/data-products` (stewards) writes each data product's organizational context onto its DataHub data product (the model's *Catalog URN*), as `veromesh.*` custom properties on the Properties tab: owner and steward roles, capabilities producing and consuming it, KPIs computed from it, agents consuming it, what it feeds and is fed by, systems, data elements, sensitivity and bounding policies. Roles only, never people. Other properties are kept; VeroMesh's are replaced on each publish. `{"ids": [...]}` limits it to some data products; the report lists those published, those whose URN is not in DataHub, and those with no URN.
- **Columns in the catalog:** `POST /api/v1/catalog/columns` (stewards) writes each column binding onto its DataHub column, in the `veromeshOntologyBindings` aspect with relation `holdsDataElement`, so a column's page shows the business data element it holds. Class bindings on the same column are kept. How complete the inventory is: `GET /api/v1/data-elements/coverage` (defined, owned, sourced, classified, bound, attested, per element and in total; `?dataProduct=` for one value stream, `?criticalOnly=true`).
- **Formal OWL/RDF:** the ontologies themselves stay in the triple store with full fidelity. `POST /sparql` runs read-only queries across ontologies and bindings; `GET /classes/assets` already follows `rdfs:subClassOf` transitively.

## Custom DataHub aspect

`datahub-model/` defines `veromeshOntologyBindings` (on datasets, schema fields, dashboards, charts and containers). It records class IRI, ontology IRI and version, which glossary terms alone can't carry. It's built as a DataHub custom-model plugin and mounted into GMS from `~/.datahub/plugins/models/` (or a ConfigMap on Kubernetes); see DataHub's `metadata-models-custom` docs.

## Next steps

1. **DataHub → Veromesh events.** Subscribe to DataHub's change stream (DataHub Actions framework, Apache-2.0) to keep bindings in sync when someone edits glossary terms in the DataHub UI, and to remove bindings for deleted assets.
2. **Reasoning.** Add an OWL 2 RL materialization step (e.g. owlrl, W3C-licensed) or plug in a reasoner when ontologies need more than subclass closure.
3. **Auth.** Done for the API: tokens from any OpenID Connect provider, Entra ID first, with five roles (`service/README.md`, "Sign-in"). Readers and agents get need-to-know answers: roles and limits, not personal details. Attestations and corrections are recorded as the signed-in person. An agent's sign-in is linked to its Agent in the model by its client ID (`GET /api/v1/me`). Decisions are recorded in an append-only, hash-chained log (`service/README.md`, "Decision log"). The decision check (`POST /api/v1/decisions/check`) answers allow, consult or refuse for an agent's action and writes to that log. Agents reach it through MCP at `/mcp` (docs/copilot-studio.md). People answer and overrule decisions, agents report outcomes, and the first measures come from the log (`service/README.md`, "After a decision"). People answer on the steward screen (`/steward`). Next: DataHub calls made as the caller rather than with a service token.
4. **Ontology versioning.** Store `owl:versionInfo` on bindings and add a diff/impact endpoint ("which assets are affected if this class changes").
5. **Production deploy.** Done as charts: DataHub's official Helm charts (Apache-2.0) plus a Veromesh chart for the service and Fuseki, with Fuseki replicated through Kafka (one writer, read replicas; see deploy/helm/README.md). Not yet installed anywhere but kind in CI and local Docker; the first cloud install (Azure, beside the assessment) is what the Copilot Studio test waits for (issue #19).
6. **Enforcement.** The decision check is advisory: an agent could skip it. A gateway in front of target systems, and optionally refusing agents with no current approved AI use case, come next.
