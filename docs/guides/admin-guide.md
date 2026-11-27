---
title: VeroMesh Ontology — Administrator Guide
---

<!--
Source of the Word copy: scripts/guides/build_docx.py builds admin-guide.docx from this file
in CI. Edit here, never in Word. Sections marked "To write" are still skeleton.
-->

# About this guide

This guide is for the people who install and run VeroMesh in their own environment: platform engineers, identity administrators and the team that owns the data platform. It covers installing, signing in, connecting source systems, keeping the service healthy and recovering it.

For the people who use VeroMesh day to day (stewards, owners, agent builders), see the **User Guide**.

| You want to | Go to |
| --- | --- |
| Install VeroMesh for the first time | [Planning](#planning-an-installation), then [Installing with Helm](#installing-with-helm) |
| Connect your identity provider | [Sign-in and access](#sign-in-and-access) |
| Load people and systems from Workday, Entra ID or a workbook | [Connecting source systems](#connecting-source-systems) |
| Restore after a failure | [Backup and restore](#backup-and-restore) |
| Find out why something is wrong | [Monitoring and troubleshooting](#monitoring-and-troubleshooting) |

# What you are installing

VeroMesh keeps a model of how your organization works (objectives, capabilities, processes, roles, people, authorities, systems, data products, data elements and AI agents) and answers one question for AI agents before they act: *is this action allowed, for whom, and on whose authority?*

| Component | What it does | Runs as |
| --- | --- | --- |
| VeroMesh service | REST API, MCP server, steward screen, validation, the decision check | `api` Deployment |
| Model store (Apache Jena Fuseki, TDB2) | Source graphs, provenance and the effective model | `fuseki` StatefulSet; optionally a writer and read replicas over Kafka |
| Decision log (PostgreSQL) | Every decision and attestation on a hash chain | Your PostgreSQL, or the bundled one |
| Connectors | Read source systems and submit changesets through the API | `veromesh-connect` jobs or CronJobs |
| DataHub (optional) | Catalog integration: glossary, data products, ownership | Your DataHub |

To write: a one-page picture of the components and the network flows between them.

# Planning an installation

## Sizing

Choose a tier from the number of people and systems in the model. The figures below are the measured targets from the load tests (`docs/testing.md`).

To write: the tier table (people, triples, memory, disk, replicas) from `tests/perf/targets.json`, including the 500,000-person run.

## Network

- Inbound: HTTPS to the API from users' browsers, agents and connectors.
- Outbound: your identity provider (OIDC discovery and keys), your source systems' APIs, DataHub if used, and the usage-report mailbox if used.
- Inside the cluster: the API to Fuseki and PostgreSQL only. The chart's NetworkPolicy enforces this.

## Before you start

- A Kubernetes cluster (1.28 or later) and Helm 3.
- An OIDC identity provider (Microsoft Entra ID, Keycloak, Okta and so on) where you can register two applications.
- A PostgreSQL database for the decision log, or use the bundled one.
- The licence file from VeroMesh.

# Installing with Helm

To write: step by step, with the minimum `values.yaml`, from `deploy/helm/README.md`.

1. Create the namespace and the secrets (OIDC client, decision-log database, licence).
2. Install the chart.
3. Wait for `/health` to report `planner: ready`. On a large model the service builds its planner state in the background after it starts; until then the first decision check is slower.
4. Register your sources and run the first connector loads.

# Sign-in and access

## Identity provider

VeroMesh validates OIDC access tokens. Set `VEROMESH_OIDC_ISSUER`, `VEROMESH_OIDC_AUDIENCE` (and `VEROMESH_OIDC_JWKS_URL` if discovery is not available), and `VEROMESH_OIDC_UI_CLIENT_ID` for the steward screen.

To write: Entra ID app registrations step by step (API app with app roles, UI app, connector client), and the Keycloak equivalent.

## Roles

| Role | Can |
| --- | --- |
| `reader` | Read the model, run impact queries |
| `agent` | Read, and ask for decisions (the decision check) |
| `connector` | Read, and submit changesets |
| `steward` | Read, attest and correct, review conflicts and the attestation queue, verify the decision log |
| `admin` | Everything, including registering sources and rebuilding the effective model |

Roles come from the token's roles claim (`VEROMESH_OIDC_ROLES_CLAIM`, with an optional prefix `VEROMESH_OIDC_ROLE_PREFIX`). The signed-in person is matched to a person in the model by `VEROMESH_PERSON_MATCH_FIELD` (email by default).

## Need to know

Readers and agents see who fills which role, but not people's personal details: by default a person's email, employee ID and reporting line are withheld from them and named under `withheld`. Stewards, connectors and administrators see everything. Each organization decides what else is withheld:

- `VEROMESH_PERSONAL_FIELDS` (Helm `api.needToKnow.personalFields`): the People fields withheld from readers and agents, comma-separated. Add `name` to withhold names too: `email,employeeId,reportsTo,name`.
- `VEROMESH_AGENT_PERSONAL_FIELDS` (Helm `api.needToKnow.agentPersonalFields`): a different list for agents, for example names shown to readers but not to agents.

Roles filled always stay answerable, so the decision check and the steward screen work either way. `GET /api/v1/schema` marks the fields withheld from the caller as `personal`. A setting that names no People field stops the service at start.

## AI agents

Each agent signs in with its own client ID, recorded on the agent in the model (`signInClientId`). An agent can only act within the use cases approved for it and the authority of the person it acts for.

The decision check alone is advisory. To enforce it, give the agent no credentials of its own for the systems it acts on: register each system's MCP server as a gateway target (`PUT /api/v1/gateway/settings`, its token in the Secret named by `api.gateway.existingSecret`) and point the agent at `/gateway/mcp`. Each consequential tool is mapped to a VeroMesh action; a call reaches the system only once it is allowed, and one that needs the owners waits until they approve. Set it up on **Administration > Gateway**: add each system's MCP server, then choose for each of its tools whether it is not offered, read-only, or checked, and for a checked tool the VeroMesh action it takes and which arguments carry the amount, currency, person and note. See `docs/gateway.md`.

# Licence and usage reports

To write: installing the licence file (`VEROMESH_LICENCE`), what the usage report contains, how it is signed, and how to send it (`VEROMESH_USAGE_*`). See `docs/licensing.md`.

# Connecting source systems

## Sources and precedence

Every fact comes from a named **source**. Register each source once with a category (`hr`, `iam`, `cmdb`, `erp`, `catalog`, `governance`, `ea`, `spreadsheet`), an owner role and a description:

```bash
curl -X PUT https://veromesh.example.com/api/v1/sources/workday \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"category": "hr", "ownerRole": "HRISLead", "description": "Workday HCM workers"}'
```

When two sources disagree, the category decides which value wins in the effective model; the disagreement is listed as a conflict. See `docs/provenance.md`.

## Connectors

| Connector | Reads | Source category |
| --- | --- | --- |
| Spreadsheet | The VeroMesh workbook | `spreadsheet` |
| CSV | Exports, one file per type or with a mapping file | any |
| Workday | Workers, positions and managers (Reports as a Service or REST), and worker changes | `hr` |
| Microsoft Entra ID | Users and managers, role groups and AI agents' service principals (Microsoft Graph), and user changes (delta) | `iam` |
| Workday Goals | Organization goals as objectives, with their owners and cascade | `hr` |
| Oracle ERP Cloud | Approval limits and user accounts | `erp` |
| ServiceNow CMDB | Applications, their owners and users | `cmdb` |
| ServiceNow Enterprise Architecture, SAP LeanIX | The business capability map and its owners | `ea` |
| HaloITSM | Assets, their owners and agents | `cmdb` |
| dbt | Models as data products, owners, sensitivity, lineage, the KPIs its metrics compute, and every column with its tags | `catalog` |
| Databricks Unity Catalog | Tables as data products, owners and stewards, sensitivity, lineage, and every column with its tags | `catalog` |

To write: credentials each connector needs in the source system (Workday API client and integration system user; Entra ID application permissions `User.Read.All`, `GroupMember.Read.All`, `Application.Read.All`), running them as CronJobs, and what to do when a run reports problems. See `docs/connectors.md`.

# Notices to owners

When a steward records in a meeting what owners confirmed (User Guide, *Attesting in meetings*), each owner is told what was recorded and until when they may object, with one reminder. Set it up on **Administration > Notices**:

- **Tell owners by:** email; Teams, or email when Teams cannot reach them; Teams only; or nobody (the steward screen only).
- **Notices show:** the values recorded, or only which facts. Personal details (an email, an employee ID, a reporting line) are never shown.
- **Reminder:** none, or after one to four business days (default three).
- **Decisions waiting for owners:** when an agent's action needs an owner's approval (above a materiality threshold or the agent's limit, or a tool call the MCP gateway holds), each person filling a consulted role is told what the agent asks, for whom and why they are asked. In Teams the card has **Approve** and **Reject**; by email it links to the steward screen. Set to "Steward screen only" to turn this off.

Email goes through the mail adapter set on Administration > Usage, to each person's email in the model. Links in notices go to `VEROMESH_PUBLIC_URL` (Helm `api.publicUrl`). Teams needs a bot registered in your Microsoft tenant (a free Azure Bot registration, whatever cloud VeroMesh runs on) and the VeroMesh app installed for owners by a Teams administrator: see `deploy/teams/README.md`. Every notice sent is on the decision log; a notice that cannot be sent is retried every hour, and the Meetings tab says why. Decision notices are sent straight after the decision and cover decisions of the last 14 days.

# Replicas and failover

To write: the replicated store (one writer, read replicas fed through Kafka), `read_fallback`, the measured lag and failover times, and rebuilding a replica.

# Backup and restore

To write: what is backed up (Fuseki datasets, the decision log), the backup CronJob in the chart, `scripts/veromesh-backup.sh`, and a tested restore procedure.

# Security

- **In transit:** TLS everywhere; connectors trust a private CA with `VEROMESH_CA_BUNDLE`.
- **At rest:** encrypted volumes for Fuseki and PostgreSQL (your storage class).
- **Audit:** every decision and attestation is appended to a hash chain in the decision log. Stewards can check it at any time: `GET /api/v1/decisions/verify` and `GET /api/v1/attestations/verify` report entries that were altered, missing or never recorded.
- **Personal data:** which fields hold personal data, and how they are handled in exports and logs.

To write: secrets handling and rotation, and the penetration-test summary once available.

# Upgrades

To write: version policy, reading the release notes, the upgrade with Helm, migrations of the store and the decision log, and rolling back.

# Monitoring and troubleshooting

## Health

`GET /health` reports the store, the decision log and the planner (`not started`, `warming`, `ready`, `not warmed`).

## Common problems

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Every call answers 401 after an upgrade | The API's audience or issuer changed, or a list of audiences was split by `helm --set` at its comma | Check `api.auth`; escape commas in `--set` (`\,`) or use a values file |
| A connector run is refused with 409 | Another run of the same source is still committing | Wait for it, then run again |
| Validation violations after a load | Rows that break the model's rules | The run lists each one with its sheet or record; fix at the source and load again |
| The first decision after a start is slow | The planner state is still being built | `/health` shows `planner: warming` until `ready` |
| A replica is behind | It is rebuilding after a large load | It follows within seconds once the load commits; see Replicas and failover |
| Owners are not told of recorded attestations | No channel reaches them: no mail adapter, no email in the model, or the Teams app not installed for them | The Meetings tab says why for each; fix it and the hourly run sends it |
| A pod stays in `ImagePullBackOff` after its image was fixed | A StatefulSet does not replace a pod stuck pulling | Delete the pod; it is recreated with the fixed image |

The dev install on Azure lists every problem met on its first install and where each is now handled: `deploy/azure/README.md`.

# Configuration reference

To write: every `VEROMESH_*` setting with its default, from `service/README.md`, and the chart values that set it.
