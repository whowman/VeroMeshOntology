# veromesh service

FastAPI service that stores OWL/RDF ontologies in a SPARQL store (Apache Jena Fuseki in deployments; embedded Oxigraph for tests) and binds their classes to DataHub assets.

```bash
cd service
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
VEROMESH_RDF_STORE=memory uvicorn veromesh.api:app --reload --port 8700   # http://localhost:8700/docs
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `VEROMESH_DATAHUB_GMS_URL` | `http://localhost:8080` | DataHub GMS endpoint |
| `VEROMESH_DATAHUB_TOKEN` | – | DataHub personal access token (needed when metadata auth is on) |
| `VEROMESH_RDF_STORE` | `memory` | `memory` (embedded, not persisted), `fuseki:<dataset URL>` (e.g. `fuseki:http://localhost:3030/veromesh`) or `oxigraph:<server URL>` |
| `VEROMESH_BINDINGS_GRAPH` | `https://veromesh.ai/graph/bindings` | Named graph for asset bindings |
| `VEROMESH_GLOSSARY_ROOT_NAME` | `Veromesh Ontology` | Root glossary node in DataHub |

| `VEROMESH_INSTANCE_BASE` | `urn:veromesh:org:` | IRI prefix for model rows imported from workbooks |
| `VEROMESH_ONTOLOGY_DIR` | repo `ontology/` (image: `/app/ontology`) | Meta-model, validation rules and examples |

## Model API

All writes go through changesets (`src/veromesh/changesets.py`); entity types and their fields come from one spec (`src/veromesh/entities.py`), which also produces `GET /api/v1/schema`. The service never reads spreadsheets; connectors in `connectors/` (spreadsheet, CSV, ...) are its clients.

| Endpoint | Does |
| --- | --- |
| `GET /api/v1/schema` | Entity types, fields, kinds, reference targets, help text |
| `GET /api/v1/{type}[/{id}]` | Read the effective model (per field, the winning source's value), one source with `?source=`, or an entity as believed at a time with `?asOf=YYYY-MM-DD` |
| `POST /api/v1/{type}?source=`, `PUT` / `DELETE /api/v1/{type}/{id}?source=` | Single-entity writes as one-item merge changesets; 422 with the report when invalid |
| `POST /api/v1/changesets` | Open a changeset: `{"source", "mode": "replace"\|"merge", "kind", "connector", "file", "idempotencyKey"}`. Records the source's current version as `baseVersion`. With a key already used for that source (and not discarded), returns that changeset (200, `"reused": true`) instead of a new one |
| `GET /api/v1/changesets[?source=&status=&limit=]` | Run history, newest first |
| `POST /api/v1/changesets/{id}/items` | Add `{"type", "op": "upsert"\|"delete", "entity", "observedAt"?, "validFrom"?, "validUntil"?}` items |
| `POST /api/v1/changesets/{id}/validate` | Validate against the whole model; nothing written |
| `POST /api/v1/changesets/{id}/commit` | Re-validate and write; 409 with the report when invalid, or when the source changed since the changeset was opened (another load or a single-entity edit). Committing again returns the same report with `"alreadyCommitted": true` |
| `DELETE /api/v1/changesets/{id}` | Discard |
| `GET /api/v1/sources` | Sources with category, provenance and `version` (the changeset that last wrote each) |
| `PUT /api/v1/sources/{name}` | Register a source's category (`hr`, `iam`, `cmdb`, `erp`, `catalog`, `spreadsheet`), owner role and description |
| `GET /api/v1/{type}/{id}/provenance` | Why we believe it: per field, the winning source, observed and valid times, attestation status, other sources' values, and history |
| `POST /api/v1/{type}/{id}/attestations` | Confirm or dispute fields: `{"verdict": "attested"\|"disputed", "attester", "role", "fields"?, "source"?, "note"?}` |
| `POST /api/v1/{type}/{id}/corrections` | Steward correction of fields a steward may correct: `{"values", "attester", "role"}` |
| `GET /api/v1/attestations?status=&type=&ownerRole=` | Review queue |
| `GET /api/v1/conflicts?type=` | Fields where sources disagree |
| `GET /api/v1/authorities/{id}/confirmation` | Whether every fact behind an authority is attested and current |
| `GET /api/v1/governance` · `POST /api/v1/effective/rebuild` | Governance settings in force · recompute the effective model |
| `GET /api/v1/examples/procure-to-pay` | The worked example as changeset items |
