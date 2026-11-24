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
| `VEROMESH_OIDC_UI_CLIENT_ID` | – | The steward screen's browser sign-in: a public (single-page application) registration, redirect URIs `https://<host>/steward/` and `https://<host>/admin/` (the Administration area) |
| `VEROMESH_OIDC_UI_SCOPE` | `openid profile email` | The scope the steward screen asks for; Entra ID: `api://<the API's ID URI>/access_as_user` |
| `VEROMESH_OIDC_JWKS_URL` | from discovery | Signing keys, when the issuer's discovery document is not reachable |
| `VEROMESH_OIDC_PERSON_CLAIMS` | `email,preferred_username,upn` | Token claims that identify the signed-in person, first present wins |
| `VEROMESH_PERSON_MATCH_FIELD` | `email` | People field those claims are matched against: `email` (ignoring case) or `employeeId` |
| `VEROMESH_DECISION_LOG` | `memory` | The decision log: `memory` (development; lost on restart) or `postgresql://user:password@host:5432/database[?sslmode=require]`, its own database |
| `VEROMESH_DECISION_REQUIRES_ATTESTED` | `true` | The check refuses when the facts behind the agent's authority are not all attested and current. `false` only to demonstrate on unreviewed data |
| `VEROMESH_TIME_ZONE` | `UTC` | The customer's calendar, as an IANA name such as `America/New_York`: authority validity dates, per-day limits ("25 invoices per day") and measures periods are days in this zone. Timestamps are stored in UTC either way. An unknown name stops the service at start |
| `VEROMESH_VALIDATION` | `incremental` | `incremental` validates only what a change can affect; `full` validates the whole model on every change (slow; same answers, for checking) |
| `VEROMESH_INCREMENTAL` | `on` | `on` plans and commits small changesets from the entities they touch (docs/architecture.md, "Performance"); `off` always takes the full path (same answers) |
| `VEROMESH_VERSION_MAX_AGE` | `0.25` | Seconds a replica reuses the model version it last read before asking the store again. A decision check then usually needs no round trip to Fuseki for it. The replica's own edits are seen at once, another replica's within this time; writes always compare against the store's current version. `0` asks every time |
| `VEROMESH_VALIDATION_WORKER` | `on` | An edit's validation (pyshacl) runs in a worker process (`python -m veromesh.validation_worker`, started with the service), so decision checks keep the service's interpreter while an edit validates. The worker adds about 150 MB. If it fails, validation runs in the service and a new worker starts. `off` validates in the service |
| `VEROMESH_WARM_ON_START` | `true` | The service builds the planner state edits and checks share in the background as it starts, so the first of them after a start does not (12.6 s on a laptop at 20,000 people, 1.3 s with it; minutes at 500,000). Requests are served meanwhile; an edit that arrives first waits for that build. `/health` reports `planner`: `warming`, `ready`, or `not warmed` if the build failed (the first request builds it). `false` leaves it to the first request |
| `VEROMESH_LICENCE` | unset | The licence VeroMesh issued for this install (the signed JSON document), from a Secret. Licences can also be loaded on the Administration area's Licence page |
| `VEROMESH_LICENSED_TIER` | empty | The licensed tier, for the usage report's share of tier: `small` (5,000 identities), `medium` (25,000), `large` (100,000) or `extra-large` (500,000 and up). Empty: not set. Read from the licence file once it exists |
| `VEROMESH_INSTALL_ID` / `VEROMESH_CUSTOMER_NAME` / `VEROMESH_HOSTING` | generated / empty / `self-hosted` | Who the usage report is from. Without an install ID one is generated and kept on first use; `hosted` when VeroMesh runs the install |
| `VEROMESH_USAGE_SIGNING_KEY` | generated | The install's Ed25519 key (PEM, PKCS#8) that signs its usage reports, from a secret (the Helm chart makes and keeps one). Without it a key is generated on first use and kept in the usage tables |
| `VEROMESH_USAGE_SMTP_PASSWORD` / `VEROMESH_USAGE_M365_CLIENT_SECRET` | unset | The mail adapter's secrets. The rest of the mail settings is set on the Administration area's Usage page (`/admin/`) or with `PUT /api/v1/usage/settings` |
| `VEROMESH_USAGE_JOB` / `VEROMESH_USAGE_INTERVAL_S` | `true` / `3600` | Count and make due reports in the background, this often (every copy runs it; one at a time does the work) |
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

**Attestations on the chain.** Every attestation (a person confirming or disputing facts, and a steward's correction, which is attested as it is made) is appended to the same chain as an `attestation` entry before the model sees it. The entry holds who attested, in which role, the entity, the source, the verdict, when, when it expires, and the SHA-256 of each value attested. The model's attestations stay in the attestations graph, where the decision check reads them. `GET /api/v1/attestations/verify` walks the chain and checks each attestation in the graph against its entry. It reports attestations whose recorded values differ (altered), any in the graph with no entry, for example written straight into the store (unrecorded), and entries whose attestation is no longer in the graph (missing).

**Usage report.** Every install makes a signed usage report each month (`src/veromesh/usage.py`; decided 20 and 22 November). Once an hour it counts what the model holds, keeping each day's highest count of identities in scope: people and AI agents in the effective model, and service accounts once the identity and access system connector arrives (Phase 2; 0 until then). On the first day of a month it makes the report for the month before: identities in scope at month end and at the peak, the share of the licensed tier (from the licence; `VEROMESH_LICENSED_TIER` without one), and, shown but not charged, every decision-log entry by kind and day, attestations, changesets committed and the entities by type. Only identities in scope are charged, and usage over the tier limits nothing. Reports are stored beside the decision log, append-only (a trigger refuses changes), signed with the install's Ed25519 key and chained: each carries the previous report's hash and the decision log's head at month end, so a changed or missing month shows. The month boundary follows a time zone set on the Usage page of the Administration area (`/admin/`) or with `PUT /api/v1/usage/settings` (default `VEROMESH_TIME_ZONE`). With a mail adapter set up (SMTP, a local sendmail, or Microsoft 365 through Graph, sending as a shared mailbox with the Mail.Send application permission), each report is sent to OntologyToolLicensing@veromesh.ai; a failed send is retried hourly and shown on the page. Administrators only: `GET /api/v1/usage` (this month so far, the reports and how sending went), `GET /api/v1/usage/reports/<month>`, `POST /api/v1/usage/reports/<month>/send`, `GET`/`PUT /api/v1/usage/settings`, `POST /api/v1/usage/settings/test`, `POST /api/v1/usage/run`. On the command line: `veromesh-usage verify <report.json> [--previous <month before>] [--public-key <key>]`, `veromesh-usage report --month 2026-11` (writes the signed file, e.g. to carry it out of an air-gapped site) and `veromesh-usage run`.

**Licence.** Every install holds a licence VeroMesh issued (`src/veromesh/licence.py`; decided 18 to 22 November): customer, install ID, tier, dates, grace period, and the install's usage key, signed with VeroMesh's Ed25519 key. The install checks it itself, offline, against the public keys built into the release (`licence_keys.json`; VeroMesh's private key stays in its Azure Key Vault), and refuses a licence that was changed, signed by an unknown key, or issued for another install or usage key. A licence runs a year by default. From 60 days before it ends, a banner warns on every screen; after it ends, 30 days of grace (set per licence) change nothing; then the install is read-only until a renewal is loaded. An install with no licence runs for 30 days from its first start, then goes read-only the same way. Read-only refuses only what loads or changes the model, its bindings and the catalog (HTTP 403, `licence expired`); reads, SPARQL queries, MCP, decision checks, overrides and outcomes, the decision log, attestations, backups and the usage report keep working, and loading a licence ends read-only at once. The licence never limits usage: its tier is what the usage report measures against. `GET /api/v1/licence/status` (anyone signed in: the stage and the banner's message); administrators: `GET /api/v1/licence` (the install's ID and public key to send VeroMesh when ordering, the licence in force and every licence held) and `PUT /api/v1/licence` (load one). On the command line: `veromesh-licence keygen` (once, for the vault), `veromesh-licence issue` (on a signer's machine: `az keyvault secret show ... | veromesh-licence issue --key-file - ...`) and `veromesh-licence verify`.

**Administration** (`/admin/`, administrators): the Licence page (stage, the install's ID and public key with copy buttons, licences held, load a licence) and the Usage page (this month so far, the monthly reports, mail settings). It signs in like the steward screen and shares its sign-in (`static/common/vm.js`).

**Decision check.** `POST /api/v1/decisions/check` (`src/veromesh/decide.py`) answers, on the effective model:

1. For whom: the agent's one principal, who must be employed. With a source of category `hr` registered, employment is HR's to decide: the principal is employed while an `hr` source lists them, or, with no employee ID, while an `iam` source does (a contractor; Entra ID is authoritative for contractors). Otherwise the check refuses, even if another source still lists them. Employed but with no account in any `iam` source (disabled): the agent's action consults the next authority up, skipping the principal's own authorities. `basis.principalStatus` says which (`employed`, `contractor`, `no-access`, `ended`, or `unknown` with no `hr` source). Issue #67; the design is in the source-weighting proposal.
2. Set up for it: the agent is registered to perform the action.
3. Authority: one the agent holds covers the action, is valid today, is in the amount's currency and within its limit, and so are the authorities it derives from. Above the agent's limit: consult the next authority up, the role whose authority for the action covers the amount with the smallest limit (a role with people in it first), and its co-approvers; only when no authority covers the amount, refuse and escalate to the principal (decided 23 November, issue #68).
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

**Steward screen** (`/steward`): plain HTML and JavaScript in the service, in the VeroMesh palette, signed in in the browser with the same identity provider (authorization code with PKCE; `VEROMESH_OIDC_UI_CLIENT_ID`). **Waiting for you**: consultations to answer (Approve, Reject), escalations above an agent's limit (Approve), allowed actions you can still Stop, each with a note. For stewards: **Facts to confirm** (attest or dispute, as a role you fill), **Decisions** (the log with where each stands, and whether the chain is intact), **Measures**, **Data elements** (the inventory's coverage, all or critical, by data product, and columns the catalog marks for an element, to Bind or reject as Not this element). With sign-in off (development) it asks whom to act as.

**MCP.** `/mcp` serves VeroMesh as an MCP server (Streamable HTTP, stateless, JSON responses) for Copilot Studio, Claude or any MCP client, behind the same sign-in and need-to-know rules as the REST routes; each tool calls the function behind a REST route (`src/veromesh/mcp_server.py`). The server's instructions tell the agent to call `check_action` before any consequential action and follow the answer. Setting up a Copilot Studio agent: [docs/copilot-studio.md](../docs/copilot-studio.md).

**Agents.** Each AI agent signs in as its own application (client credentials) with the `agent` role, and is linked to the model by the Agent entity's **Sign-in client ID** (`signInClientId`): the client ID its tokens carry (`azp`, or `appid` in Entra ID v1 tokens, or `client_id`). Through that entity VeroMesh knows the one person the agent acts for and the authorities it holds; `GET /api/v1/me` shows the link, or says what is missing. Each agent needs its own client ID; the validation rules refuse two agents sharing one. An agent may also sign in **as the person it acts for, through its own app registration** (a delegated token whose `azp` is the agent's client ID, as a Copilot Studio agent with user sign-in does): VeroMesh then treats the call as the agent's, and refuses it unless the signed-in person is the agent's principal.

**Connectors** send a token with `--token` / `VEROMESH_TOKEN`, or get one themselves with client credentials (`VEROMESH_TOKEN_URL`, `VEROMESH_CLIENT_ID`, `VEROMESH_CLIENT_SECRET`, `VEROMESH_SCOPE`; for Entra ID the scope is `<Application ID URI>/.default`).

## Model API

All writes go through changesets (`src/veromesh/changesets.py`); entity types and their fields come from one spec (`src/veromesh/entities.py`), which also produces `GET /api/v1/schema`. The service never reads spreadsheets; connectors in `connectors/` (spreadsheet, CSV, ...) are its clients.

| Endpoint | Does |
| --- | --- |
| `GET /api/v1/me` | Who the caller is: VeroMesh roles; for a person, their own record; for an agent, its Agent entity, the one person it acts for (need to know: roles, not personal details) and the authorities it holds, or what is missing to link it |
| `POST /api/v1/decisions/check` | Before a consequential action, an agent signed in as itself asks `{"action", "amount"?, "currency"?, "note"?}`: allow, consult (the owners to consult first; above its limit, the next authority up) or refuse, with reasons, whom to inform and, when no authority covers the amount, whom to escalate to. Logged |
| `/mcp` | The same, as an MCP server for agents (Streamable HTTP): `who_am_i`, `check_action`, `decision_status`, `report_outcome`, `impact_of_action`, `who_owns`, `get_entity` |
| `GET /api/v1/decisions[?agent=&principal=&action=&kind=&refersTo=&afterSeq=&limit=]` | Decision log entries in order (stewards): decisions, and the overrides and outcomes that refer to them |
| `GET /api/v1/decisions/{id}/status` | Where a decision stands (allowed, pending, approved, rejected, refused), may the agent act, who answered, what was done. For the agent, its principal, the owners involved and stewards |
| `POST /api/v1/decisions/{id}/overrides` | A person answers or overrules, as themselves: `{"verdict": "approved"\|"rejected", "role"?, "note"?}` |
| `POST /api/v1/decisions/{id}/outcome` | The agent reports what it did: `{"outcome": "done"\|"not_done", "note"?}` |
| `GET /api/v1/findings` | What needs attention (access without employment, employed without access, out of sync, ended references, domains with no deciding source, and what connectors report), each routed to an owner role and the people in it. Filters `kind`, `severity`, `ownerRole`, `source` |
| `PUT /api/v1/findings/reported/{source}` | A connector's findings for its source (stewards and connectors); each run replaces the last |
| `GET /api/v1/domains` | Which source category decides each question (source weighting): authoritative and evidence categories per domain, the fields it covers, whether this organization changed it |
| `PUT` / `DELETE /api/v1/domains/{name}` | Change a domain, or return it to the default (administrators). Recorded on the decision log's hash chain; the effective model is rebuilt |
| `GET /api/v1/decisions/inbox` | What waits for the signed-in person: consultations to answer, escalations above their agent's limit, allowed actions they may stop |
| `GET /api/v1/decisions/measures[?since=&until=&agent=]` | Override rate, consultation precision, delegation extended (stewards) |
| `GET /api/v1/decisions/{id}` | One entry and the entries that refer to it |
| `GET /api/v1/decisions/verify` | Walk the hash chain: ok, or the first entry changed, removed or inserted |
| `GET /api/v1/attestations/verify` | Check the model's attestations against the chain: ok, or those altered, unrecorded or missing |
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
