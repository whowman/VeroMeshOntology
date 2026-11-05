# Veromesh Ontology

Formal ontologies (OWL/RDF) and a business semantic layer on top of the open source [DataHub](https://github.com/datahub-project/datahub) metadata platform.

- Load OWL ontologies into a SPARQL store (Apache Jena Fuseki)
- Publish ontology classes to DataHub as glossary terms
- Bind DataHub assets (datasets, columns, dashboards…) to ontology classes, visible in DataHub and queryable with SPARQL

DataHub runs **unmodified** from its official images; Veromesh extends it with a custom-model plugin and its APIs. See [docs/architecture.md](docs/architecture.md).

## Repository layout

| Path | What |
| --- | --- |
| `service/` | Veromesh API (Python, FastAPI): REST model API, changesets, validation |
| `loader/` | Spreadsheet loader, a client of the REST API (CLI and container) |
| `datahub-model/` | Custom DataHub aspect `veromeshOntologyBindings` (PDL + entity registry) |
| `ontology/` | Veromesh core vocabulary, the organizational meta-model (draft) with its validation rules, and examples; see [docs/meta-model.md](docs/meta-model.md) |
| `deploy/` | Pinned versions and the compose file for Veromesh services |
| `scripts/` | Local stack helpers |
| `docs/` | Architecture and licensing |
| `third_party/` | Upstream license and notice files we redistribute |

## Quick start (local)

Requires git and Docker (Docker Desktop on macOS) with Compose ≥ 2.20 and **at least 8 GB of memory assigned to Docker**. No Java or Python install is needed; the DataHub plugin is built in a JDK 25 container. The first start downloads several GB of images and takes roughly 15–20 minutes; later starts take a few minutes. Ports used: 8080, 9002, 9200, 5432, 3030, 8700.

```bash
scripts/dev-up.sh          # DataHub (Postgres + OpenSearch) + Jena Fuseki + Veromesh API
scripts/load-examples.sh   # load example ontology, publish it to the DataHub glossary

# bind a dataset to a class
curl -X POST localhost:8700/bindings -H 'Content-Type: application/json' -d '{
  "datahub_urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,crm.public.customers,PROD)",
  "class_iri": "https://example.com/ontology/retail#Customer"
}'

# every asset holding any kind of Party (follows subclasses)
curl 'localhost:8700/classes/assets?class_iri=https://example.com/ontology/retail%23Party'
```

### Load your organizational model from a spreadsheet

All facts enter the model through the REST API (`/api/v1`, documented at http://localhost:8700/docs). The spreadsheet loader is one client of it: it reads a workbook and submits it as a **changeset** that is validated against the whole model before anything is written.

```bash
scripts/veromesh-loader.sh template -o model.xlsx                  # blank workbook, built from the API's schema
scripts/veromesh-loader.sh template --example -o example.xlsx      # filled-in procure-to-pay example
scripts/veromesh-loader.sh import example.xlsx --source p2p        # validate, then commit
scripts/veromesh-loader.sh import model.xlsx --source hr --dry-run # validate only
scripts/veromesh-loader.sh export --source p2p -o p2p.xlsx         # edit and re-import
scripts/veromesh-loader.sh sources
```

The wrapper runs the loader in Docker; with Python 3.11+ you can `pip install ./loader` and run `veromesh-loader` directly.

- Errors (unknown IDs, bad values) and rule violations stop the load and are reported by sheet, row and column. Gaps such as a KPI without an owner are warnings.
- Each workbook is one **source**. `replace` (default) makes the workbook the whole source; `--mode merge` upserts into it.
- References may point at entities from other sources, e.g. a `p2p` workbook can use roles loaded from an `hr` workbook.

| REST API | |
| --- | --- |
| `GET /api/v1/schema` | Every entity type with its fields, references and help text |
| `GET /api/v1/{type}`, `GET /api/v1/{type}/{id}` | Read entities (e.g. `kpis`, `roles`, `authorities`), optionally `?source=` |
| `PUT` / `POST` / `DELETE /api/v1/{type}/{id}?source=` | Change one entity; validated and committed as a one-item changeset |
| `POST /api/v1/changesets` → `/items` → `/validate` → `/commit` | Bulk changes, all or nothing |
| `GET /api/v1/sources` | Sources with kind, file, last update and the changeset that wrote them |

DataHub UI: http://localhost:9002 (login `datahub` / `datahub`) · Veromesh API docs: http://localhost:8700/docs

Stop with `scripts/dev-down.sh` (add `--volumes` to wipe data). The CI `stack` job runs this same flow on every push.

Service-only development (no Docker): see [service/README.md](service/README.md).

## Licensing

Veromesh is proprietary (see `LICENSE`). DataHub and the other bundled components are open source under permissive licenses; obligations and the approved-license policy are in [docs/licensing.md](docs/licensing.md) and [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
