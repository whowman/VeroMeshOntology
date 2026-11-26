# Connectors

Every fact enters the VeroMesh model through the REST API (`/api/v1`). A **connector** is a client that reads one kind of source (a workbook, a CSV export, an HR or CMDB system) and submits what it read as a **changeset**, which the service validates against the whole model before anything is written.

Connectors are built on a small SDK (`connectors/src/veromesh_connectors/core`) organized by the seven layers of the connector model. A new connector only implements the source-specific parts. Everything else (retries, authentication, resuming after a crash, refusing to overwrite concurrent changes, reporting findings against source locations) comes from the SDK and the service.

## The seven layers

| # | Layer | Responsibility | Where it lives |
| --- | --- | --- | --- |
| 1 | Physical | Network, hosts, files on disk | Infrastructure: Docker network `datahub_network`, the `connectors` container, the files mounted at `/work` |
| 2 | Transport | Reliable delivery: connections, timeouts, retries, TLS | `core/transport.py`: retries connection failures and 429/502/503/504 with exponential backoff, honours `Retry-After`, trusts a private CA (`VEROMESH_CA_BUNDLE`) |
| 3 | Presentation | Encoding and formats | Source side: each plugin's reader (openpyxl for `.xlsx`, `csv` with delimiter sniffing and BOM handling). VeroMesh side: JSON bodies in `core/session.py` |
| 4 | Session | Identity, and the state of one conversation | `core/session.py`: static bearer token, or OAuth 2.0 client credentials (e.g. a Keycloak service account) refreshed before expiry and once more on a 401. The conversation is the **changeset**; its idempotency key lets a crashed run resume it, and `cleanup` discards ones left open |
| 5 | Interaction | Pattern of exchange | `core/contract.py`: `BatchConnector` (read everything, submit one changeset: workbooks, exports) or `EventConnector` (one small merge changeset per change event: webhooks, queues, CDC) |
| 6 | Coordination | Ordering, batching, duplicates, concurrency | `core/runner.py` with server support in `service/src/veromesh/changesets.py`: duplicate and conflicting records caught before sending; items sent in batches of 500; idempotent open and commit; a version guard so a run never overwrites a change made since it started; a per-source lock on commit |
| 7 | Semantic | Meaning: source vocabulary to the model | `core/mapping.py`: `TabularMapper` for sources whose columns are the model's fields, `DeclarativeMapper` for any other vocabulary via a mapping file. The service then checks the result against the OWL meta-model and SHACL rules |

```
 source system / file
        │  (1 physical)
 ┌──────▼─────────────────────────── plugin ──────────────┐
 │ read()        (3 presentation, source side)            │
 │ map()         (7 semantic: TabularMapper/Declarative)  │
 │ locate()      finding -> "Sheet KPIs, row 7, column X" │
 └──────┬─────────────────────────────────────────────────┘
 ┌──────▼─────────────────────────── core SDK ────────────┐
 │ Runner        (6 coordination, 5 interaction)          │
 │ ApiSession    (4 session, 3 presentation: JSON)        │
 │ Transport     (2 transport)                            │
 └──────┬─────────────────────────────────────────────────┘
        │  HTTPS /api/v1/changesets ...
 ┌──────▼─────────────────────────── VeroMesh service ────┐
 │ changesets: idempotency, version guard, lock (6)       │
 │ validation: whole model, OWL + SHACL (7)               │
 │ Fuseki: source graphs + provenance                     │
 └────────────────────────────────────────────────────────┘
```

## What a run does

A batch run (`Runner.run`):

1. **Read** the source into records, each with a reference to where it came from (`Sheet KPIs row 7`, `workers.csv line 12`). Structural problems (a missing sheet, an unknown column) are collected.
2. **Map** each record to model entities. Two records that produce the same entity with different values are an error.
3. If anything failed so far, **nothing is sent**; the problems are printed against the source.
4. **Open** a changeset with an idempotency key, add the items in batches, **validate**.
5. Findings come back keyed by entity type, ID and field; the connector's `locate` turns them into source locations. Errors and violations: the changeset is discarded and nothing is written. Warnings are shown but do not block.
6. **Commit**. If the source changed since the run started, the service refuses (409) and the run reports it rather than overwriting.

An event run (`Runner.run_events`) does steps 2–6 once per event, as a `merge` changeset keyed by the event ID, so a redelivered event is applied once.

### Re-running is safe

Each run has a content key (connector, source, mode and the exact items) and an idempotency key (content key plus the source version the run started from).

| Situation | What happens |
| --- | --- |
| Same input, and the source was last written by that same input | Nothing to do ("already up to date") |
| The run crashed after opening its changeset | The re-run finds the changeset by its key and resumes it |
| The same input, but someone changed the source since | A new changeset; the input is applied again |
| Two runs against the same source at once | The first to commit wins; the second gets a 409 and writes nothing |
| `--dry-run` | Validated in its own changeset, then discarded |

`veromesh-connect runs` lists recent changesets (who, what, status); `veromesh-connect cleanup --older-than-hours 24` discards changesets that crashed runs left open.

## Mapping files

For a source with its own vocabulary, a YAML mapping file tells the `DeclarativeMapper` how to build entities. One source row can produce several entities. `{Column}` inserts a source column; `values` translates vocabularies; `optional: true` skips a rule when its ID comes out empty.

```yaml
# workday.yaml: an HR worker export -> people and roles
rules:
  - type: people
    id: "{Worker ID}"
    fields:
      name: "{First Name} {Last Name}"
      employeeId: "{Worker ID}"
      email: "{Work Email}"
      rolesFilled: "{Position ID}"
      reportsTo: "{Manager ID}"
  - type: roles
    id: "{Position ID}"
    fields:
      name: "{Job Title}"
values:
  systems.criticality: {"Tier 1": critical, "Tier 2": high}
```

```bash
scripts/veromesh-connect.sh csv import workers.csv --source hr --mapping workday.yaml --dry-run
```

Rule types and field keys are checked against `GET /api/v1/schema` before anything is read, so a typo in the mapping file is reported up front.

## Writing a connector

A connector implements the source-facing parts only:

```python
from veromesh_connectors.core import BatchConnector, Item, Record


class CmdbConnector(BatchConnector):
    name = "cmdb"                   # recorded on every changeset it opens
    kind = "connector"

    def __init__(self, client):
        super().__init__()
        self.client = client

    def describe(self) -> str:      # shown in "Read N records from ..."
        return "CMDB"

    def read(self, schema):         # layers 1-3, source side
        for row in self.client.applications():
            yield Record(ref=f"CMDB {row['sys_id']}", data=row, type="systems")

    def map(self, record, schema):  # layer 7
        return [
            Item(
                type="systems",
                entity={"id": record.data["u_app_code"], "name": record.data["name"]},
                ref=record.ref,
            )
        ]

    def locate(self, ref, type_, field):  # optional: where to point the user
        return f"{ref}" + (f", field {field}" if field else "")
```

Run it with the SDK's runner:

```python
from veromesh_connectors.core import ApiSession, Runner, RunOptions, StaticToken, Transport

api = ApiSession(Transport("https://veromesh.example.com"), StaticToken(token))
Runner(api).run(CmdbConnector(client), RunOptions(source="cmdb", mode="replace"))
```

For a source that pushes changes, subclass `EventConnector` and yield `Event(id, records, deletes)` from `events(schema)`; run it with `Runner.run_events`.

Guidelines:

- Keep `read` free of model knowledge and `map` free of I/O; this keeps mapping testable with plain records.
- Use the shared mappers where you can: `TabularMapper` when the columns already are the model's fields, `DeclarativeMapper` (mapping file) when they are not.
- Give every record a `ref` a person can find in the source. All findings are reported against it.
- One source per system of record. `replace` makes the input the whole source; `merge` upserts into it. References may point at entities in other sources.

## Connectors included

| Connector | Command | Notes |
| --- | --- | --- |
| Spreadsheet | `spreadsheet template \| import \| export` | The VeroMesh workbook: one sheet per entity type, generated from the API's schema; export writes a source back out for editing |
| CSV | `csv import <file or folder>` | One file per entity type (named after it, or `--type`), or any columns with `--mapping` |
| Workday | `workday import \| changes \| approvals` | Workers, positions and managers from Workday's public APIs; changes since a date, one event each; business process approval rules |
| Microsoft Entra ID | `entra import \| changes` | Users, managers, role groups and AI agents through Microsoft Graph; changes through delta queries |
| ServiceNow CMDB | `servicenow import` | Business applications as systems (criticality, lifecycle), an owner role per application's business and technical owner, filled by the people ServiceNow names; active users (who can sign in) as a second source; likely duplicate applications reported as findings |
| HaloITSM | `halo import` | Business applications (matched to systems VeroMesh already holds by name) and their owners; enabled agent seats as a second source |
| Oracle Fusion Cloud ERP | `erp import \| exceptions` | Who can act in the ERP (active accounts and their roles); approval exceptions (purchase orders their buyer approved, invoices approved above the approver's limit or without a co-approver) reported as findings |

Tests for the SDK and the connectors run against the real API in-process (`connectors/tests`), including retries, token refresh, crash-resume, concurrent edits and event mode. The Workday, Entra ID and Oracle ERP connectors are tested against small fakes of the public APIs, and against the VeroMesh test harness (a simulated company with each system's quirks) when it is installed: `test_against_harness.py`.

### Source systems' APIs

Connectors that call a source system's API use `core/source.py`: `SourceApi` reads JSON with the same transport as the VeroMesh side (backoff, `Retry-After`; for reads a 500 is retried too) and renews the source's OAuth token once on a 401. Grants: `ClientCredentials` (Entra ID), `RefreshToken` (Workday) and `BasicAuth` (Oracle Fusion's integration user). A source that refuses a request or cannot be reached is reported as a `SourceError` naming the system.

### Workday

```bash
export WORKDAY_CLIENT_ID=... WORKDAY_CLIENT_SECRET=... WORKDAY_REFRESH_TOKEN=...
veromesh-connect workday import --url https://wd2-impl-services1.workday.com --tenant acme
veromesh-connect workday changes --since 2026-11-01      # prints the --since for the next run
veromesh-connect workday approvals --tenant acme --role-map groups.yaml   # business process rules
veromesh-connect workday goals --tenant acme --role-map titles.yaml       # organization goals
```

- **Sign-in:** an API client registered in Workday, with a refresh token for the integration system user (without `WORKDAY_REFRESH_TOKEN`, client credentials), at `/ccx/oauth2/{tenant}/token`.
- **Full load** (`import`): by default the custom report `ISU_VeroMesh/VeroMesh_Workers` through Reports as a Service (`--report-owner`, `--report`); it needs the columns `Employee_ID`, `Preferred_Name`, `Email_Primary_Work`, `Business_Title`, `Manager_Employee_ID`, `Hire_Date`, `Termination_Date` and `Active_Status`. `--via rest` reads `/ccx/api/v1/{tenant}/workers` instead, 100 a page.
- **Writes:** people (ID = employee ID; the preferred name; email; manager; the role filled; valid from the hire date and until a termination date ahead) and a role per business title (`--role-map` maps titles to existing role IDs; `--no-roles` writes none). Workers whose termination has taken effect are left out. A manager who is not in the run (a contractor) is left out of the reference, and the run says how many.
- **Placement and groups:** optional report columns `Supervisory_Organization` and `Cost_Center` give each person's department and cost centre (an authority's scope), and `Organization_Roles` the role-based security groups assigned to them (HR Partner), ';'-separated, as roles. `--manager-role Manager` gives everyone with direct reports the group business processes call Manager.
- **Business processes** (`approvals`, step 6b): the custom report `VeroMesh_Approval_Rules` over the business process definitions, one row per rule (`Business_Process_Type`, `Rule_Type` Initiator or Approval, `Security_Group`, `Condition_Measure` Percent_Change, Grade_Steps, Headcount or Amount, `Condition_Above`, `Organization_Scope`, `Step_Order`). An initiator becomes an authority held by the group's role (`{role}-{action}`) with its limit and scope (Supervisory Organization: the reporting line; Organization: the department; Cost Center; Company: anyone). An approval step becomes a co-approval on each initiator's authority: Manager and Manager's Manager as relations to the worker, other groups as roles. Processes map to actions (`--process-map`; by default Request Compensation Change → AdjustPay, Promote Employee Inbound → PromoteEmployee, Hire → HireEmployee, Termination → TerminateEmployee). Register the source (`workday-approvals`) as `hr`: for decisions about people the business process is the workflow in force.
- **Goals** (`goals`): Workday Talent's organization goals as **objectives**, from the custom report `VeroMesh_Goals` (one row per goal: `Goal_ID`, `Goal_Name`, `Owner_Employee_ID`, `Owner_Business_Title`, `Parent_Goal_ID`; optional `Description`, `Goal_Level`, `Status`, `KPI_IDs`). Each objective is owned by the owner's role, the one the workers load gives their business title (or the `--role-map` entry), with the goal it is aligned to as its parent objective. `Goal_Level` (enterprise, function, capability or process) sets the level; without it a goal with no parent is an enterprise objective and the rest function objectives. Cancelled and archived goals are left out; a goal aligned to one not in the report is loaded without its parent and counted. `KPI_IDs` (a calculated field, ';'-separated) links the KPIs that measure it, which must already be in the model. Register the source (`workday-goals`) as `hr`: by default HR decides the objectives domain (names, definitions, owners, levels, the cascade), with the governance workbook as evidence; priorities and which objectives pull against each other still come from the workbook.
- **Changes** (`changes`): the report `VeroMesh_Worker_Changes` with `Updated_From`, by the date each business process was entered, so a termination entered late is still picked up. Each event fetches the worker and upserts them; a termination that has taken effect deletes them. Overlapping runs are safe: each event is applied once.

### Microsoft Entra ID

```bash
export ENTRA_CLIENT_ID=... ENTRA_CLIENT_SECRET=...
veromesh-connect entra import --tenant <tenant ID> --role-group-prefix SG-
veromesh-connect entra changes --tenant <tenant ID> --role-group-prefix SG- --state entra-delta.json
```

- **Sign-in:** an app registration with the application permissions `User.Read.All`, `GroupMember.Read.All` and `Application.Read.All`, signing in with client credentials.
- **Full load** (`import`): `/users` with `$expand=manager`, paged; with `--role-group-prefix`, the groups named `<prefix><role ID>` and their members; service principals tagged `AIAgent` (`--agent-tag`, `''` for none) and their owners.
- **Writes:** people (ID = `employeeId`, the same as the HR system, so both sources describe one person; for accounts with no employee ID, such as contractors, the user principal name's local part, which the service warns about), roles from role groups, and agents (the client ID they sign in with, and the owner they act for). Disabled accounts are left out, along with references to them. An agent whose owners have all left is left out too, so the decision check refuses it.
- **Changes** (`changes`): Graph delta queries. The first run records the current position in the state file; later runs apply each changed user as one event (with their manager and role groups), and delete removed or disabled accounts. The position is saved only when every event applied, so a refused event is retried. A leaver who is still the principal of an agent is refused until the agent is reassigned or the next full load leaves it out.

### Oracle Fusion Cloud ERP

```bash
export ORACLE_USER=... ORACLE_PASSWORD=...
veromesh-connect erp import --url https://acme.fa.us2.oraclecloud.com --role-map erp-roles.yaml
veromesh-connect erp exceptions --url https://acme.fa.us2.oraclecloud.com --since 2026-11-01
```

- **Sign-in:** an integration user with basic authentication, able to read user accounts, purchase orders, invoices and their approval history.
- **Full load** (`import`): `/hcmRestApi/resources/11.13.18.05/userAccounts?expand=userAccountRoles`, 500 a page (`limit`, `offset`, `hasMore`). Run it after HR and Entra ID, because it matches accounts to the people VeroMesh already holds.
- **Writes:** people, one per account that is not suspended and is linked to a person: the person number when it is an employee ID VeroMesh knows, else the user name's local part when that is a person VeroMesh holds (a contractor, as Entra ID names them), else the person number. With `--role-map` (an object of Oracle role codes to role IDs, such as `ORA_AP_ACCOUNTS_PAYABLE_MANAGER_JOB: APManager`), the roles they fill in the ERP. No employee ID (the ERP is not the record of employment), and the name VeroMesh already holds, because Oracle's accounts carry none. A leaver whose ERP account is still active therefore shows as **access without employment** (`GET /api/v1/findings`).
- **Exceptions** (`exceptions`): purchase orders (`purchaseOrders`, buyer and approver) and invoice approvals (`invoices`, `invoiceApprovalsAndNotificationsHistory`) on or after `--since`, checked against the authorities and co-approval tiers in VeroMesh; reported with `PUT /api/v1/findings/reported/erp`, each run replacing the last. Roles are the ones VeroMesh holds today, so someone who has since changed role can show, and an approver who has left since cannot be checked.
- **What Oracle does not publish:** the invoice approval rules themselves (amounts and approver levels are BPM approval rules set up in Functional Setup Manager, with no REST resource to read them). The ERP team exports the limits in force into the standard workbook (Authorities and Co-approvals sheets) and loads it as a source registered as `erp`: the workflow closest to the decision decides, and a difference from the governance workbook is an **out of sync** finding.

### ServiceNow CMDB

```bash
export SERVICENOW_USER=... SERVICENOW_PASSWORD=...   # or SERVICENOW_TOKEN=<OAuth access token>
veromesh-connect servicenow import --url https://acme.service-now.com
```

- **Sign-in:** an integration user with read access to `cmdb_ci_business_app` and `sys_user` (basic authentication), or an OAuth access token.
- **Full load** (`import`): the Table API, `/api/now/table/cmdb_ci_business_app` and `/api/now/table/sys_user` (active users, and every user named as an owner even if inactive), 1,000 a page (`sysparm_limit`, `sysparm_offset`). Run it after HR and Entra ID, because it matches users to the people VeroMesh already holds.
- **Writes:** systems (ID = the application's number, the CMDB ID its sys_id, name, criticality 1 to 4 as critical, high, medium, low, lifecycle active or retired) and, because ownership in VeroMesh sits with a role, one role per application's business owner (`owned_by`) and technical owner (`managed_by`), named "Business owner of ..." and "Technical owner of ...". People: matched by employee number, else by user name (a contractor), with the owner roles they fill, and their legal name and email. No employee ID: ServiceNow copies it from HR.
- **Two sources:** `servicenow` holds the applications, their owner roles and the people they name (active or not); `servicenow-users` (`--users-source`) holds every active user. Register the second with `grantsAccess: true`: an active ServiceNow user can still sign in and act there, so a leaver who is still active shows as access without employment.
- **Findings:** `duplicate-system`, two applications with the same owners, vendor, kind and support group under different names, reported with `PUT /api/v1/findings/reported/servicenow`. An owner whose HR record has gone shows in VeroMesh's own findings as `owner-has-left`.

### ServiceNow Enterprise Architecture (capabilities)

```bash
curl -X PUT https://<veromesh>/api/v1/sources/servicenow-ea -d '{"category": "ea"}' ...   # once
veromesh-connect servicenow ea --url https://acme.service-now.com [--id-field number] [--parent-field parent]
```

- **Sign-in:** the same as the CMDB connector, with read access to `cmdb_ci_business_capability` and `sys_user`.
- **Full load** (`ea`): the Table API, `/api/now/table/cmdb_ci_business_capability`, 1,000 a page, and each owner's `sys_user` record. Retired capabilities (install status retired or absent) are left out. Run it after HR, because it matches owners to the people VeroMesh already holds.
- **Writes**, as source `servicenow-ea` (category `ea`): capabilities with their name, definition (`short_description`), parent capability (`parent`, or the field given with `--parent-field`) and owner **role** ("Owner of Accounts payable", ID `AccountsPayable.Owner`), and each owner filling it. A capability's ID comes from its name in the workbook's style ("Accounts payable" becomes `AccountsPayable`), so a capability the workbook also lists is one entity; `--id-field number` takes it from a field instead. Two capabilities that would get the same ID stop the load until one is renamed or a unique field is used. A capability whose parent is retired is loaded at the top level.
- **Who decides:** by default the EA tool keeps the capability map (the `capabilities` domain: `ea` authoritative, then governance and the workbook as evidence), decided 25 November 2026. Where the workbook names a different owner, the EA's owner is in force and the difference is reported as **out of sync**. An organization whose map lives elsewhere changes the domain, for example `PUT /api/v1/domains/capabilities {"authoritative": ["spreadsheet"], "evidence": ["ea"]}`, and `DELETE` returns to the default.

### dbt (tables, lineage and KPIs)

```bash
curl -X PUT https://<veromesh>/api/v1/sources/dbt -d '{"category": "catalog"}' ...   # once
veromesh-connect dbt import --dir target/ --runs-on LAKE                                  # a dbt build's folder
veromesh-connect dbt import --url https://artifacts.acme.com/dbt --runs-on LAKE           # artifacts served over HTTPS
DBT_CLOUD_TOKEN=... veromesh-connect dbt import --dbt-cloud --account 70 --job 42 --runs-on LAKE
```

- **Reads** dbt's documented run artifacts: `manifest.json` (required) and `run_results.json` (optional), from a `target/` folder, a URL that serves them (`DBT_ARTIFACTS_TOKEN` as a bearer token if needed), or dbt Cloud's Administrative API v2, the artifacts of a job's latest run (`/api/v2/accounts/{account}/jobs/{job}/artifacts/...`, a service token in `DBT_CLOUD_TOKEN`; `--url` for a dbt Cloud address other than cloud.getdbt.com). It only reads.
- **Writes**, as source `dbt` (category `catalog`, which decides what data means): a **data product** per model and source, with the ID `database.schema.name` (the table as the warehouse names it), the data products it feeds (the manifest's `depends_on`, read the other way), the system it runs on (`--runs-on`, for example the lakehouse from the CMDB), its sensitivity (the highest of its columns' `meta.classification`: public, internal, confidential, or restricted for PII, PHI and PCI) and its DataHub dataset URN (`urn:li:dataset:(urn:li:dataPlatform:<adapter>,<table>,<env>)`, `--env` default PROD), so the catalog page and data-change impact meet. Each table with `meta.owner` (an email) gets an owner role, "Owner of <table>", filled by that person, matched by email.
- **KPIs:** each dbt metric (the dbt Semantic Layer) links its KPI to the data products it is computed from (`computedFromDataProducts`), which is what lets a data change reach the KPIs and their owners. The KPI's ID is the metric's `meta.veromesh_kpi`, else its name in the workbook's style (`on_time_payment_rate` becomes `OnTimePaymentRate`). Only KPIs already in the model are linked; dbt says how a KPI is computed, not that it is one the organization tracks.
- **Counted, not loaded:** models that failed in the last run (and what they skipped), owners who match no one, and metrics that are not KPIs in the model. Tests, seeds and snapshots are not data products.

### SAP LeanIX (capabilities)

```bash
export LEANIX_API_TOKEN=...   # a technical user's API token (Administration, Technical Users)
curl -X PUT https://<veromesh>/api/v1/sources/leanix -d '{"category": "ea"}' ...   # once
veromesh-connect leanix import --url https://acme.leanix.net [--id-from externalId] [--owner-role "Business Owner"]
```

- **Sign-in:** the API token is exchanged at `/services/mtm/v1/oauth2/token` (basic authentication as `apitoken`, client credentials) for a short-lived access token; a technical user with read access to Business Capability fact sheets is enough.
- **Full load** (`import`): the GraphQL API, `/services/pathfinder/v1/graphql`, `allFactSheets(factSheetType: BusinessCapability)` in pages of 200 (Relay cursors): name, description, status, the parent (`relToParent`) and the subscriptions. Archived fact sheets are left out. It only reads. Run it after HR, because owners are matched to people by email.
- **Writes**, as source `leanix` (category `ea`), the same shape as ServiceNow EA: capabilities with name, definition, parent and owner role (`AccountsPayable.Owner`, "Owner of Accounts payable"), and each owner filling it. The owner is the RESPONSIBLE subscriber with the subscription role `--owner-role` (default "Business Owner"), else the first RESPONSIBLE subscriber; an owner whose email matches no one in VeroMesh is counted in the run's summary. IDs come from the name in the workbook's style, or the fact sheet's external ID (`--id-from externalId`); two capabilities that would share an ID stop the load. Because both EA connectors give a capability the same ID and owner role, an organization with ServiceNow EA and LeanIX gets one capability, with any disagreement between them reported as a conflict.

### HaloITSM

```bash
export HALO_CLIENT_ID=... HALO_CLIENT_SECRET=...   # HALO_TENANT=<tenant> for a Halo-hosted instance
veromesh-connect halo import --url https://acme.haloitsm.com
```

- **Sign-in:** an API application registered in Halo (Configuration, Integrations, Halo API) with client credentials and the `all` scope, at `/auth/token`.
- **Full load** (`import`): `/api/Asset` (assets of type "Business Application") and `/api/Agent`, 100 a page (`pageinate`, `page_size`, `page_no`, `record_count`). Run it after HR, Entra ID and ServiceNow, because it matches names and emails to what VeroMesh already holds.
- **Writes:** `halo`: systems (under the ID of the system VeroMesh already holds with the same name, so ServiceNow and Halo describe one system; else the inventory number), criticality and lifecycle, and the business owner from the "Business Owner" custom field (a name or an email, matched to one person) filling the same owner role the ServiceNow connector uses; an owner that matches no one is reported. `halo-agents` (`--agents-source`): every enabled agent seat, matched by email; register it with `grantsAccess: true`, so an IT leaver whose seat was not freed shows as access without employment.

Register each source once with its category (`hr` for Workday, `iam` for Entra ID, `erp` for the ERP, `cmdb` for ServiceNow and HaloITSM, `ea` for ServiceNow EA and SAP LeanIX, with `grantsAccess` on `servicenow-users` and `halo-agents`) so precedence applies; see `docs/provenance.md`.
