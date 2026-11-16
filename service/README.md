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
| `VEROMESH_OIDC_ISSUER` | – | Identity provider issuer. Entra ID: `https://login.microsoftonline.com/<tenant ID>/v2.0`; Keycloak: `https://<host>/realms/<realm>` |
| `VEROMESH_OIDC_AUDIENCE` | – | What tokens are issued for: the API's application (client) ID or Application ID URI; several comma-separated |
| `VEROMESH_OIDC_ROLES_CLAIM` | `roles` | Claim with the caller's roles; dotted for nested claims (`realm_access.roles` in Keycloak) |
| `VEROMESH_OIDC_ROLE_PREFIX` | – | Prefix stripped from role names, e.g. `VeroMesh.` for Entra app roles `VeroMesh.Admin` |
| `VEROMESH_OIDC_JWKS_URL` | from discovery | Signing keys, when the issuer's discovery document is not reachable |
| `VEROMESH_OIDC_PERSON_CLAIMS` | `email,preferred_username,upn` | Token claims that identify the signed-in person, first present wins |
| `VEROMESH_PERSON_MATCH_FIELD` | `email` | People field those claims are matched against: `email` (ignoring case) or `employeeId` |
| `VEROMESH_AUTH_DISABLED` | `false` | `true`: no sign-in, every request acts as an administrator. Local development and tests only |

## Sign-in

Every route except `/health` and the docs pages needs `Authorization: Bearer <access token>` from the customer's OpenID Connect provider: Entra ID first (Copilot agents sign in there), Keycloak for customers without Microsoft, or the customer's own provider. The service checks the token's signature against the provider's published keys, its issuer, audience and expiry, and reads the caller's roles from one claim (`src/veromesh/auth.py`). It refuses to start with sign-in neither configured nor turned off.

| Role | May |
| --- | --- |
| `admin` | Everything, including `POST /sparql`, loading ontologies, publishing the glossary, registering sources and rebuilding the effective model |
| `steward` | Read everything, including personal details; review queue, conflicts, provenance; attest and correct any entity; bindings |
| `connector` | Write changesets and single entities (the spreadsheet and CSV connectors, HR and CMDB feeds); read personal details |
| `reader` | Read the model on a need-to-know basis (below); attest the entities whose owner or steward role the person fills |
| `agent` | An AI agent: reads like a reader. The decision check comes next |

A token without any of these roles gets 403.

**Need to know.** Readers and agents get what acting within an authority needs: roles, authorities and their limits, scopes and dates, co-approvals, owners, actions, KPIs, and who fills which role. They do not get personal details: fields the schema marks `"personal": true` (a person's email, employee ID and reporting line) are left out of every entity answer, including `?asOf=` and `?source=`, and their names are listed under `withheld`. Stewards, connectors and administrators get the whole record. Raw SPARQL, which could read anything, stays with administrators.

**Attesting as yourself.** Attestations and corrections are recorded as the signed-in person: the first of the token claims in `VEROMESH_OIDC_PERSON_CLAIMS` (default `email,preferred_username,upn`) is matched to exactly one person in the model by `VEROMESH_PERSON_MATCH_FIELD` (`email`, ignoring case, or `employeeId`). Owners attest their own facts: a person without the steward role (any VeroMesh role, e.g. `reader`) may attest an entity when the role they act in is one of its owner or steward roles and they fill it; stewards may attest anything. Corrections stay with stewards. A request naming someone else as attester is refused, as is acting in a role the person does not fill, a sign-in that matches no one, and a service (app-only) token. With sign-in off, the request names the attester and a role they do not fill is only a warning.

**Entra ID setup:** register the API as an app, set its access tokens to v2 (`"accessTokenAcceptedVersion": 2` in the manifest), expose an Application ID URI, and define app roles `VeroMesh.Admin`, `VeroMesh.Steward`, `VeroMesh.Connector`, `VeroMesh.Reader` and `VeroMesh.Agent` for users and applications. Assign people to roles under Enterprise applications; give each connector or agent its own app registration with the role it needs (client credentials). Then set the issuer, the audience (the Application ID URI or client ID) and `VEROMESH_OIDC_ROLE_PREFIX=VeroMesh.`.

**Connectors** send a token with `--token` / `VEROMESH_TOKEN`, or get one themselves with client credentials (`VEROMESH_TOKEN_URL`, `VEROMESH_CLIENT_ID`, `VEROMESH_CLIENT_SECRET`, `VEROMESH_SCOPE`; for Entra ID the scope is `<Application ID URI>/.default`).

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
| `POST /api/v1/{type}/{id}/attestations` | Confirm or dispute fields: `{"verdict": "attested"\|"disputed", "role", "fields"?, "source"?, "note"?}`. The attester is the signed-in person (with sign-in off, `"attester"`: their person ID) |
| `POST /api/v1/{type}/{id}/corrections` | Steward correction of fields a steward may correct: `{"values", "role"}`, by the signed-in person (with sign-in off, `"attester"` too) |
| `GET /api/v1/attestations?status=&type=&ownerRole=` | Review queue |
| `GET /api/v1/conflicts?type=` | Fields where sources disagree |
| `GET /api/v1/authorities/{id}/confirmation` | Whether every fact behind an authority is attested and current |
| `GET /api/v1/governance` · `POST /api/v1/effective/rebuild` | Governance settings in force · recompute the effective model |
| `GET /api/v1/examples/procure-to-pay` | The worked example as changeset items |
