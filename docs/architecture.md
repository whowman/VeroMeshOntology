# Architecture

Veromesh adds a formal ontology layer to DataHub without forking it.

```
                 ┌──────────────────────────────┐
  users / apps → │   Veromesh service (FastAPI)  │ ← SPARQL, REST
                 └───────┬──────────────┬───────┘
                         │              │
          SPARQL 1.1     │              │  DataHub SDK (REST / GraphQL)
                         ▼              ▼
             ┌──────────────────┐   ┌──────────────────────────────────┐
             │ Jena Fuseki      │   │ DataHub v1.8.0 (unmodified)       │
             │ ontologies +     │   │  GMS + frontend + actions         │
             │ bindings graph   │   │  + veromesh-ontology-model plugin │
             └──────────────────┘   │  Postgres · OpenSearch            │
                                    └──────────────────────────────────┘
```

## Responsibilities

| Concern | Lives in | Why |
| --- | --- | --- |
| Ontology classes, properties, hierarchy, axioms | Apache Jena Fuseki, TDB2 storage (one named graph per ontology) | OWL/RDF, SPARQL and reasoning need a triple store; DataHub has none |
| Data assets, lineage, ownership, schemas | DataHub | That's what DataHub is for |
| Business vocabulary for search and the UI | DataHub glossary (published from the ontology) | People find concepts where they already look |
| Asset ↔ class bindings | Both: `https://veromesh.ai/graph/bindings` graph **and** DataHub (`veromeshOntologyBindings` aspect + `glossaryTerms`) | SPARQL can answer "all assets that hold any kind of Party"; DataHub shows the concept on the asset page |

## The two ontology modes

- **Business semantic layer:** `POST /glossary/publish` turns every `owl:Class` in an ontology into a DataHub glossary term under *Veromesh Ontology → \<ontology name\>* (one node per ontology; `rdfs:subClassOf` stays in the triple store). Binding an asset also attaches the term, so it appears in DataHub search, filters and lineage views.
- **Formal OWL/RDF:** the ontologies themselves stay in the triple store with full fidelity. `POST /sparql` runs read-only queries across ontologies and bindings; `GET /classes/assets` already follows `rdfs:subClassOf` transitively.

## Custom DataHub aspect

`datahub-model/` defines `veromeshOntologyBindings` (on datasets, schema fields, dashboards, charts and containers). It records class IRI, ontology IRI and version, which glossary terms alone can't carry. It's built as a DataHub custom-model plugin and mounted into GMS from `~/.datahub/plugins/models/` (or a ConfigMap on Kubernetes); see DataHub's `metadata-models-custom` docs.

## Next steps

1. **DataHub → Veromesh events.** Subscribe to DataHub's change stream (DataHub Actions framework, Apache-2.0) to keep bindings in sync when someone edits glossary terms in the DataHub UI, and to remove bindings for deleted assets.
2. **Reasoning.** Add an OWL 2 RL materialization step (e.g. owlrl, W3C-licensed) or plug in a reasoner when ontologies need more than subclass closure.
3. **Auth.** Put the API behind the same SSO as DataHub; pass the caller's DataHub token through instead of a service token.
4. **Ontology versioning.** Store `owl:versionInfo` on bindings and add a diff/impact endpoint ("which assets are affected if this class changes").
5. **Production deploy.** DataHub's official Helm charts (Apache-2.0) plus a Veromesh chart for the service and Fuseki, with RDF Delta for Fuseki high availability.
