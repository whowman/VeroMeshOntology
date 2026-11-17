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
| `VEROMESH_OIDC_UI_CLIENT_ID` | – | The steward screen's browser sign-in: a public (single-page application) registration, redirect URI `https://<host>/steward/` |
| `VEROMESH_OIDC_UI_SCOPE` | `openid profile email` | The scope the steward screen asks for; Entra ID: `api://<the API's ID URI>/access_as_user` |
| `VEROMESH_OIDC_JWKS_URL` | from discovery | Signing keys, when the issuer's discovery document is not reachable |
| `VEROMESH_OIDC_PERSON_CLAIMS` | `email,preferred_username,upn` | Token claims that identify the signed-in person, first present wins |
| `VEROMESH_PERSON_MATCH_FIELD` | `email` | People field those claims are matched against: `email` (ignoring case) or `employeeId` |
| `VEROMESH_DECISION_LOG` | `memory` | The decision log: `memory` (development; lost on restart) or `postgresql://user:password@host:5432/database[?sslmode=require]`, its own database |
| `VEROMESH_DECISION_REQUIRES_ATTESTED` | `true` | The check refuses when the facts behind the agent's authority are not all attested and current. `false` only to demonstrate on unreviewed data |
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

**Decision log.** Every decision about an agent's action (allow, consult or refuse, with reasons, the principal, the authority used and the facts relied on), and every later override or outcome that refers to it, is appended to the decision log (`src/veromesh/decisions.py`). Entries are never changed: in PostgreSQL a trigger refuses UPDATE, DELETE and TRUNCATE, and each entry carries the SHA-256 of the one before it, so `GET /api/v1/decisions/verify` finds any entry changed, removed or inserted, even by someone who bypasses the trigger. Concurrent writers (several API replicas) take an advisory lock and form one chain. Give the log its own database; the service creates its table on first use. Export to write-once storage for regulated customers follows.

**Decision check.** `POST /api/v1/decisions/check` (`src/veromesh/decide.py`) answers, on the effective model:

1. For whom: the agent's one principal.
2. Set up for it: the agent is registered to perform the action.
3. Authority: one the agent holds covers the action, is valid today, is in the amount's currency and within its limit, and so are the authorities it derives from. Above the limit: refuse, and escalate to the principal.
4. Attested facts only: every fact behind that authority is attested and current, or refuse.
5. Co-approval: a tier on the authority or a parent applies above its amount: consult the approver role.
6. Impact: per KPI or capability the action affects, below the materiality threshold act and inform the owner; above it, consult the owner first. Money thresholds ("USD per invoice") compare the amount; per-day thresholds ("invoices per day") count today's allowed decisions in the log; a threshold that cannot be checked counts as material.

Refuse if any step refuses, else consult if anyone must be consulted, else allow. Each answer names the roles and the people filling them, and is appended to the decision log. It is advisory: an agent could skip it; enforcement at a gateway comes later. An agent asks as itself (its sign-in implies the agent, and the day is today); an administrator may test a check on an agent's behalf with `agent` and `onDate`.

**After a decision** (`src/veromesh/followup.py`), everything is appended to the decision log, referring to the decision:

- A consulted owner, filling a consulted role, approves or rejects; the decision is approved once every consulted role has approved, rejected as soon as one rejects.
- Above the agent's limit, the principal may approve (taking responsibility; a candidate for extending the delegation). A refusal is never "rejected": it stands until approved.
- The principal or an informed owner may stop an allowed action (rejected).
- The agent reports `done` or `not_done`, once the decision stands as allowed or approved. Per-day thresholds count actions taken: allowed ones, and consulted ones once done.
- Measures (working definitions, to confirm against the white paper): **override rate**, decisions a person overruled (an allow stopped, a refusal approved) over all decisions; **consultation precision**, of consultations answered, the share the owner rejected (low: owners are asked about things they always approve); **delegation extended**, the share of decisions within the agent's own delegation, and escalations the principal approved.

**Steward screen** (`/steward`): plain HTML and JavaScript in the service, in the VeroMesh palette, signed in in the browser with the same identity provider (authorization code with PKCE; `VEROMESH_OIDC_UI_CLIENT_ID`). **Waiting for you**: consultations to answer (Approve, Reject), escalations above an agent's limit (Approve), allowed actions you can still Stop, each with a note. For stewards: **Facts to confirm** (attest or dispute, as a role you fill), **Decisions** (the log with where each stands, and whether the chain is intact), **Measures**. With sign-in off (development) it asks whom to act as.

**MCP.** `/mcp` serves VeroMesh as an MCP server (Streamable HTTP, stateless, JSON responses) for Copilot Studio, Claude or any MCP client, behind the same sign-in and need-to-know rules as the REST routes; each tool calls the function behind a REST route (`src/veromesh/mcp_server.py`). The server's instructions tell the agent to call `check_action` before any consequential action and follow the answer. Setting up a Copilot Studio agent: [docs/copilot-studio.md](../docs/copilot-studio.md).

**Agents.** Each AI agent signs in as its own application (client credentials) with the `agent` role, and is linked to the model by the Agent entity's **Sign-in client ID** (`signInClientId`): the client ID its tokens carry (`azp`, or `appid` in Entra ID v1 tokens, or `client_id`). Through that entity VeroMesh knows the one person the agent acts for and the authorities it holds; `GET /api/v1/me` shows the link, or says what is missing. Each agent needs its own client ID; the validation rules refuse two agents sharing one. An agent may also sign in **as the person it acts for, through its own app registration** (a delegated token whose `azp` is the agent's client ID, as a Copilot Studio agent with user sign-in does): VeroMesh then treats the call as the agent's, and refuses it unless the signed-in person is the agent's principal.

**Connectors** send a token with `--token` / `VEROMESH_TOKEN`, or get one themselves with client credentials (`VEROMESH_TOKEN_URL`, `VEROMESH_CLIENT_ID`, `VEROMESH_CLIENT_SECRET`, `VEROMESH_SCOPE`; for Entra ID the scope is `<Application ID URI>/.default`).

## Model API

All writes go through changesets (`src/veromesh/changesets.py`); entity types and their fields come from one spec (`src/veromesh/entities.py`), which also produces `GET /api/v1/schema`. The service never reads spreadsheets; connectors in `connectors/` (spreadsheet, CSV, ...) are its clients.

| Endpoint | Does |
| --- | --- |
| `GET /api/v1/me` | Who the caller is: VeroMesh roles; for a person, their own record; for an agent, its Agent entity, the one person it acts for (need to know: roles, not personal details) and the authorities it holds, or what is missing to link it |
| `POST /api/v1/decisions/check` | Before a consequential action, an agent signed in as itself asks `{"action", "amount"?, "currency"?, "note"?}`: allow, consult (the owners to consult first) or refuse, with reasons, whom to inform and, above its limit, whom to escalate to. Logged |
| `/mcp` | The same, as an MCP server for agents (Streamable HTTP): `who_am_i`, `check_action`, `decision_status`, `report_outcome`, `impact_of_action`, `who_owns`, `get_entity` |
| `GET /api/v1/decisions[?agent=&principal=&action=&kind=&refersTo=&afterSeq=&limit=]` | Decision log entries in order (stewards): decisions, and the overrides and outcomes that refer to them |
| `GET /api/v1/decisions/{id}/status` | Where a decision stands (allowed, pending, approved, rejected, refused), may the agent act, who answered, what was done. For the agent, its principal, the owners involved and stewards |
| `POST /api/v1/decisions/{id}/overrides` | A person answers or overrules, as themselves: `{"verdict": "approved"\|"rejected", "role"?, "note"?}` |
| `POST /api/v1/decisions/{id}/outcome` | The agent reports what it did: `{"outcome": "done"\|"not_done", "note"?}` |
| `GET /api/v1/decisions/inbox` | What waits for the signed-in person: consultations to answer, escalations above their agent's limit, allowed actions they may stop |
| `GET /api/v1/decisions/measures[?since=&until=&agent=]` | Override rate, consultation precision, delegation extended (stewards) |
| `GET /api/v1/decisions/{id}` | One entry and the entries that refer to it |
| `GET /api/v1/decisions/verify` | Walk the hash chain: ok, or the first entry changed, removed or inserted |
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
