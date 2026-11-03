# Veromesh Ontology

Formal ontologies (OWL/RDF) and a business semantic layer on top of the open source [DataHub](https://github.com/datahub-project/datahub) metadata platform.

- Load OWL ontologies into a SPARQL store (Oxigraph)
- Publish ontology classes to DataHub as glossary terms
- Bind DataHub assets (datasets, columns, dashboards…) to ontology classes, visible in DataHub and queryable with SPARQL

DataHub runs **unmodified** from its official images; Veromesh extends it with a custom-model plugin and its APIs. See [docs/architecture.md](docs/architecture.md).

## Repository layout

| Path | What |
| --- | --- |
| `service/` | Veromesh API (Python, FastAPI) |
| `datahub-model/` | Custom DataHub aspect `veromeshOntologyBindings` (PDL + entity registry) |
| `ontology/` | Veromesh core vocabulary and an example retail ontology |
| `deploy/` | Pinned versions and the compose file for Veromesh services |
| `scripts/` | Local stack helpers |
| `docs/` | Architecture and licensing |
| `third_party/` | Upstream license and notice files we redistribute |

## Quick start (local)

Requires Docker with Compose ≥ 2.20 and ~8 GB RAM, git, and JDK 17+ (to build the DataHub model plugin).

```bash
scripts/dev-up.sh          # DataHub (Postgres + OpenSearch) + Oxigraph + Veromesh API
scripts/load-examples.sh   # load example ontology, publish it to the DataHub glossary

# bind a dataset to a class
curl -X POST localhost:8700/bindings -H 'Content-Type: application/json' -d '{
  "datahub_urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,crm.public.customers,PROD)",
  "class_iri": "https://example.com/ontology/retail#Customer"
}'

# every asset holding any kind of Party (follows subclasses)
curl 'localhost:8700/classes/assets?class_iri=https://example.com/ontology/retail%23Party'
```

DataHub UI: http://localhost:9002 · Veromesh API docs: http://localhost:8700/docs · `scripts/dev-down.sh` to stop.

Service-only development (no Docker): see [service/README.md](service/README.md).

## Licensing

Veromesh is proprietary (see `LICENSE`). DataHub and the other bundled components are open source under permissive licenses; obligations and the approved-license policy are in [docs/licensing.md](docs/licensing.md) and [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
