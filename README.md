# Veromesh Ontology

The organizational ontology (OWL/RDF): who owns what, who may decide what, and what depends on it. It sits beside the open source [DataHub](https://github.com/datahub-project/datahub) catalog and publishes its vocabulary there as glossary terms. DataHub is a catalog with a glossary, not a semantic layer; where a customer has a semantic layer (metric definitions, entity meaning), the ontology takes it as an input.

What it does today (main, 27 November 2026):

- **The organizational model:** people, roles, delegated authority, co-approvals, capabilities, processes, objectives and KPIs, policies, systems, data products, data elements, AI agents and approved AI use cases, validated by SHACL rules ([docs/meta-model.md](docs/meta-model.md)).
- **Kept true:** loaded from spreadsheets, CSV, Workday (workers, change feeds, business process approval rules and organization goals), Microsoft Entra ID, Oracle ERP, ServiceNow (CMDB and Enterprise Architecture capabilities), SAP LeanIX, HaloITSM, and the catalogs dbt and Databricks Unity Catalog (tables, owners, sensitivity, lineage and the KPIs dbt computes), through their public APIs ([docs/connectors.md](docs/connectors.md)). Each source has its own named graph. **Domains** say which kind of source decides each question (HR decides employment and the objective cascade; the workflow in force decides authority; the enterprise architecture tool decides the capability map) and which only gives evidence; the domains are configuration, not code, and each organization can change them. Owners attest or dispute facts, on the steward screen or in the meetings they already hold, where a steward records what they confirmed, they are told by email or in Teams (an Adaptive Card with Confirm and Object, from a bot that runs wherever VeroMesh runs), with one reminder, and they can object for five business days; every notice is on the decision log ([docs/provenance.md](docs/provenance.md), [deploy/teams/README.md](deploy/teams/README.md)).
- **Changes wait for their owner:** a change to who reports to whom, the roles someone fills, an authority or an agent's principal is a proposal until its owner attests it. Narrowing takes effect at once. Every change to the model is a publish with a model version, and every decision quotes the version it was made on.
- **Findings, routed to owners:** a leaver who still has access, someone employed with no access, a source out of sync with the policy, an owner who has left, a degraded capability, an unsettled objective conflict, and changes waiting for attestation.
- **Source fidelity:** how well each source is kept, measured per question from corrections, lateness, gaps, contradictions and loads (high, medium or low; declared until there is history). Low makes the decision check consult the source's owner.
- **Sign-in and need to know:** tokens from any OpenID Connect provider (Entra ID first), five roles, and answers that leave out personal details for callers not allowed them.
- **The agent decision check:** before acting, an agent asks and gets allow, consult (named owners) or refuse, with reasons, decided from attested facts only. It covers whether the principal is employed, the agent's approved use, its authority (above the limit, the next authority up), co-approvals and approval chains, the impact on KPIs and capabilities (unmapped impact is consulted), trade-offs between objectives, and degraded capability health. **Approvals beyond money:** limits in percent, grade steps or headcount, authority scoped to a reporting line, department or cost centre, and chains such as the manager's manager then the HR partner, for promotions, raises, hires and terminations. Every answer, and every attestation behind one, goes into an append-only, hash-chained decision log that a steward can verify at any time; people approve or reject, agents report outcomes, and the first measures come from the log. Over REST and MCP (`/mcp`, [docs/copilot-studio.md](docs/copilot-studio.md)). **Enforced at a gateway:** agents can call their tools through VeroMesh (`/gateway/mcp`), so a call reaches the system only once it is allowed; a call that needs the owners is held until they approve ([docs/gateway.md](docs/gateway.md)).
- **The data element inventory:** columns bound to business data elements (in bulk from spreadsheets or the API, or accepted from the catalog's own classifications, from DataHub or reported by the dbt and Databricks connectors, so suggestions work without DataHub), and coverage: which critical data elements are owned, sourced, classified, bound and attested.
- **The steward screen** (`/steward`): what waits for you, facts to confirm, decisions, measures, source fidelity and the data element inventory. Agents' reads are logged with their purpose, and the model's own measures (facts sourced automatically, change latency) and data-change impact (what is downstream, and whom to tell) are in the API and MCP.
- **Into DataHub:** ontology classes as glossary terms, assets bound to classes, and each data product's owner roles, consumers and lineage on its catalog page.
- **Formal OWL/RDF** in Apache Jena Fuseki, replicated through Kafka, with read-only SPARQL for administrators. At 500,000 people the read replicas trail the writer by about half a second, decision checks carry on from them while the writer restarts (379 a second, none failed), and a new replica builds itself in under six minutes ([docs/testing.md](docs/testing.md#replication-under-load)).
- **Enterprise scale:** measured on the Azure reference machines with Fuseki and the PostgreSQL decision log. At 500,000 people (E16s v6, Premium SSD v2) a decision check takes 4 ms and an edit 226 ms, a nightly re-sync 104 s and the first load 46 minutes. One copy of the service answers 412 checks a second; two copies, each following the other's changes from records in the store, answer 544 while the model is not being edited and 425 with an edit every second. Fuseki needs low-latency storage ([docs/testing.md](docs/testing.md#capacity-on-fuseki), [deploy/azure-capacity/README.md](deploy/azure-capacity/README.md)).
- **Licensing and usage:** a licence signed by VeroMesh and checked offline, with warnings, 30 days of grace, then read-only (decision checks, the decision log and attestations keep working); a signed, chained monthly usage report of identities in scope, sent to VeroMesh by SMTP, sendmail or Microsoft 365; and an Administration area (`/admin`) for both ([service/README.md](service/README.md)).
- **Backups and encryption:** nightly backups of the model and the decision log, encrypted to the customer's own key, with a tested restore; encryption at rest with the customer's keys; mutual TLS between every pod through Istio's ambient mesh, with certificates from the customer's OpenBao ([deploy/helm/README.md](deploy/helm/README.md#backups-and-restore)).

- **On Azure (dev):** running at https://dev-ontology.veromesh.ai beside the assessment, with DataHub, Entra sign-in and the decision log on Azure PostgreSQL ([deploy/azure/README.md](deploy/azure/README.md)). Rebuilt from nothing on 26 November with no manual steps (`setup.sh reset`, then a deploy); every problem met on the first install is now handled by the scripts, and the few steps still done by hand are listed there. It is turned on and off from GitHub, off every night at about 11 PM Eastern, while the assessment keeps running.

Not yet: the Copilot Studio test on the Azure install (issue #19; its steps and script are in [docs/copilot-studio-test.md](docs/copilot-studio-test.md)), the Teams bot registered for dev (built; `deploy/teams/register-bot.sh`), linking capabilities to the applications that support them, and VeroMesh's own licensing system (issue #56). The plan is the Delivery Plan; open work is in the GitHub issues.

Administrator and user guides: [docs/guides/](docs/guides/) (CI builds Word copies). Connectors are tested against the [VeroMesh test harness](https://github.com/whowman/VeroMeshTestHarness), a simulated company whose systems serve their public APIs.

DataHub runs **unmodified** from its official images; Veromesh extends it with a custom-model plugin and its APIs. See [docs/architecture.md](docs/architecture.md).

## Repository layout

| Path | What |
| --- | --- |
| `service/` | Veromesh API (Python, FastAPI): REST model API, changesets, validation |
| `connectors/` | Connector SDK and connectors (spreadsheet, CSV, Workday, Entra ID, Oracle ERP, ServiceNow, SAP LeanIX, HaloITSM): clients of the REST API, CLI and container; see [docs/connectors.md](docs/connectors.md) |
| `datahub-model/` | Custom DataHub aspect `veromeshOntologyBindings` (PDL + entity registry) |
| `branding/` | VeroMesh palette, logos, and the DataHub theme; see [branding/README.md](branding/README.md) |
| `ontology/` | Veromesh core vocabulary, the organizational meta-model (draft) with its validation rules, and examples; see [docs/meta-model.md](docs/meta-model.md) |
| `deploy/` | Pinned versions, the compose file for Veromesh services, the Helm charts for Kubernetes ([deploy/helm/README.md](deploy/helm/README.md)), the dev install on Azure ([deploy/azure/README.md](deploy/azure/README.md)), and the capacity runs on Azure ([deploy/azure-capacity/README.md](deploy/azure-capacity/README.md)) |
| `scripts/` | Local stack helpers, and `test.sh`, the regression test harness |
| `tests/` | Regression harness, contract snapshots and end-to-end tests; see [docs/testing.md](docs/testing.md) |
| `docs/` | Architecture, connectors, testing and licensing |
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

### Load your organizational model (spreadsheet, CSV, other systems)

All facts enter the model through the REST API (`/api/v1`, documented at http://localhost:8700/docs). Connectors are its clients: each reads a source and submits it as a **changeset** that is validated against the whole model before anything is written.

```bash
scripts/veromesh-connect.sh spreadsheet template -o model.xlsx                  # blank workbook, built from the API's schema
scripts/veromesh-connect.sh spreadsheet template --example -o example.xlsx      # filled-in procure-to-pay example
scripts/veromesh-connect.sh spreadsheet import example.xlsx --source p2p        # validate, then commit
scripts/veromesh-connect.sh spreadsheet import model.xlsx --source hr --dry-run # validate only
scripts/veromesh-connect.sh spreadsheet export --source p2p -o p2p.xlsx         # edit and re-import
scripts/veromesh-connect.sh csv import exports/ --source cmdb                   # one CSV per entity type
scripts/veromesh-connect.sh csv import workers.csv --source hr --mapping workday.yaml  # another system's columns
scripts/veromesh-connect.sh sources                                             # what is loaded, and its version
scripts/veromesh-connect.sh runs --source p2p                                   # run history
scripts/veromesh-connect.sh cleanup                                             # discard runs a crash left open
```

The wrapper runs the connectors in Docker; with Python 3.11+ you can `pip install ./connectors` and run `veromesh-connect` directly. How connectors are built, and how to write a new one, is in [docs/connectors.md](docs/connectors.md).

- Errors (unknown IDs, bad values) and rule violations stop the load and are reported by sheet, row and column (or file and line). Gaps such as a KPI without an owner are warnings.
- Each workbook (or CSV folder) is one **source**. `replace` (default) makes the workbook the whole source; `--mode merge` upserts into it.
- References may point at entities from other sources, e.g. a `p2p` workbook can use roles loaded from an `hr` workbook.

| REST API | |
| --- | --- |
| `GET /api/v1/schema` | Every entity type with its fields, references and help text |
| `GET /api/v1/{type}`, `GET /api/v1/{type}/{id}` | Read entities (e.g. `kpis`, `roles`, `authorities`), optionally `?source=` |
| `PUT` / `POST` / `DELETE /api/v1/{type}/{id}?source=` | Change one entity; validated and committed as a one-item changeset |
| `POST /api/v1/changesets` → `/items` → `/validate` → `/commit` | Bulk changes, all or nothing |
| `GET /api/v1/sources` | Sources with kind, file, last update and the changeset that wrote them |

DataHub UI: http://localhost:9002 (login `datahub` / `datahub`), in the VeroMesh theme · VeroMesh API docs: http://localhost:8700/docs. `SKIP_BRANDING=1 scripts/dev-up.sh` gives the stock DataHub look.

Stop with `scripts/dev-down.sh` (add `--volumes` to wipe data).

## Kubernetes

Helm charts for a customer install are in [deploy/helm/](deploy/helm/README.md): the `veromesh` chart (the service, and Fuseki as a writer plus read replicas kept in step through Kafka), plus overlays for DataHub's official charts that add the VeroMesh model plugin, theme and PostgreSQL. `deploy/helm/build-images.sh` builds every image the install needs.

## Testing

```bash
scripts/test.sh                     # lint, unit tests and contract snapshots for every component
scripts/test.sh integration         # + a real Fuseki and the container images
scripts/test.sh e2e                 # end-to-end against the running stack
scripts/test.sh all --start-stack   # everything
```

Capacity (decision checks per second, memory, re-syncs on a real Fuseki) runs by hand: **Actions → Capacity** on a GitHub runner, **Actions → Capacity on Azure** on the reference machines ([deploy/azure-capacity/README.md](deploy/azure-capacity/README.md)).

Needs only Docker and `python3`; add `--local` to run without Docker. CI runs the same commands. Suites, snapshots and how to add a component: [docs/testing.md](docs/testing.md).

Service-only development (no Docker): see [service/README.md](service/README.md).

## Licensing

The organizational model and everything in it belong to the customer: it is held in open W3C standards (RDF, OWL, SHACL) in the customer's own environment, and can be read out whole at any time (`/sparql`, the workbook export). The software that keeps it true (connectors, precedence, attestation, the decision check) is VeroMesh's, licensed and proprietary (see `LICENSE`). DataHub and the other bundled components are open source under permissive licenses; obligations and the approved-license policy are in [docs/licensing.md](docs/licensing.md) and [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
