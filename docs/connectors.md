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
| Workday | `workday import \| changes` | Workers, positions and managers from Workday's public APIs; changes since a date, one event each |
| Microsoft Entra ID | `entra import \| changes` | Users, managers, role groups and AI agents through Microsoft Graph; changes through delta queries |

Tests for the SDK and the connectors run against the real API in-process (`connectors/tests`), including retries, token refresh, crash-resume, concurrent edits and event mode. The Workday and Entra ID connectors are tested against small fakes of the public APIs, and against the VeroMesh test harness (a simulated company with each system's quirks) when it is installed: `test_against_harness.py`.

### Source systems' APIs

Connectors that call a source system's API use `core/source.py`: `SourceApi` reads JSON with the same transport as the VeroMesh side (backoff, `Retry-After`; for reads a 500 is retried too) and renews the source's OAuth token once on a 401. Grants: `ClientCredentials` (Entra ID) and `RefreshToken` (Workday). A source that refuses a request or cannot be reached is reported as a `SourceError` naming the system.

### Workday

```bash
export WORKDAY_CLIENT_ID=... WORKDAY_CLIENT_SECRET=... WORKDAY_REFRESH_TOKEN=...
veromesh-connect workday import --url https://wd2-impl-services1.workday.com --tenant acme
veromesh-connect workday changes --since 2026-11-01      # prints the --since for the next run
```

- **Sign-in:** an API client registered in Workday, with a refresh token for the integration system user (without `WORKDAY_REFRESH_TOKEN`, client credentials), at `/ccx/oauth2/{tenant}/token`.
- **Full load** (`import`): by default the custom report `ISU_VeroMesh/VeroMesh_Workers` through Reports as a Service (`--report-owner`, `--report`); it needs the columns `Employee_ID`, `Preferred_Name`, `Email_Primary_Work`, `Business_Title`, `Manager_Employee_ID`, `Hire_Date`, `Termination_Date` and `Active_Status`. `--via rest` reads `/ccx/api/v1/{tenant}/workers` instead, 100 a page.
- **Writes:** people (ID = employee ID; the preferred name; email; manager; the role filled; valid from the hire date and until a termination date ahead) and a role per business title (`--role-map` maps titles to existing role IDs; `--no-roles` writes none). Workers whose termination has taken effect are left out. A manager who is not in the run (a contractor) is left out of the reference, and the run says how many.
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

Register each source once with its category (`hr` for Workday, `iam` for Entra ID) so precedence applies; see `docs/provenance.md`.
