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

All writes go through changesets (`src/veromesh/changesets.py`); entity types and their fields come from one spec (`src/veromesh/entities.py`), which also produces `GET /api/v1/schema`. The service never reads spreadsheets; see `loader/` for the spreadsheet client.

| Endpoint | Does |
| --- | --- |
| `GET /api/v1/schema` | Entity types, fields, kinds, reference targets, help text |
| `GET /api/v1/{type}[/{id}]` | Read entities across the model or `?source=` |
| `POST /api/v1/{type}?source=`, `PUT` / `DELETE /api/v1/{type}/{id}?source=` | Single-entity writes as one-item merge changesets; 422 with the report when invalid |
| `POST /api/v1/changesets` | Open a changeset: `{"source", "mode": "replace"\|"merge", "kind", "file"}` |
| `POST /api/v1/changesets/{id}/items` | Add `{"type", "op": "upsert"\|"delete", "entity"}` items |
| `POST /api/v1/changesets/{id}/validate` | Validate against the whole model; nothing written |
| `POST /api/v1/changesets/{id}/commit` | Re-validate and write; 409 with the report when invalid |
| `DELETE /api/v1/changesets/{id}` | Discard |
| `GET /api/v1/sources` | Sources with provenance |
| `GET /api/v1/examples/procure-to-pay` | The worked example as changeset items |
