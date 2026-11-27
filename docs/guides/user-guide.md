---
title: VeroMesh Ontology — User Guide
---

<!--
Source of the Word copy: scripts/guides/build_docx.py builds user-guide.docx from this file
in CI. Edit here, never in Word. Sections marked "To write" are still skeleton.
-->

# About this guide

This guide is for the people who keep the VeroMesh model right and the people who build on it:

- **Owners and stewards**, who confirm or correct what the model says about the things they own;
- **Model editors**, who maintain parts of the model in the workbook;
- **Agent builders**, who connect AI agents to VeroMesh so they check before they act.

Installing and running VeroMesh is covered in the **Administrator Guide**.

# Concepts

## The model

The model describes how your organization works, in terms an AI agent can check:

| Kind | Examples | What it answers |
| --- | --- | --- |
| Objectives and KPIs | Reduce days payable outstanding; DPO | What the organization is trying to achieve, and how it is measured |
| Capabilities and processes | Procure to pay; approve an invoice | What the organization does, and how |
| Roles and people | AP manager; who fills it, who they report to | Who is accountable |
| Authorities | Approve invoices up to 25,000 USD | Who may decide what, up to what limit, until when |
| Policies and regulations | Delegation of authority policy | The rules authorities sit within |
| Systems, data products and data elements | ERP; invoices; invoice amount | Where the work and its data live, and who owns them |
| Agents and AI use cases | An invoice assistant; approved to draft approvals | What each agent may do, and for whom |

To write: one worked example (an agent asking to approve an invoice) traced through the model.

## Sources, the effective model and conflicts

Every fact comes from a **source** (Workday, Entra ID, the CMDB, a workbook, a steward's correction). When sources disagree, precedence decides which value VeroMesh uses (the **effective model**) and the disagreement is listed as a **conflict** for the owner to resolve. Nothing is silently overwritten, and the model can be read as it stood on any past date.

## Attestation

Owners confirm (**attest**) that what the model says about their entities is right, or **dispute** it with a correction. Attestations expire and must be renewed. Each one is recorded on the decision log's hash chain, so it cannot be changed afterwards without it showing.

## Attesting in meetings

Owners do not have to visit a screen. In a meeting they already hold (a monthly review, a governance board), a **steward** records what each owner confirmed:

1. **Before the meeting**, on the steward screen's **Meetings** tab: create the meeting (name, date, a link to its minutes or recording), choose who attends, and press **Pre-read**: for each person, the facts waiting for them in the roles they fill.
2. **In the meeting**, tick what each owner confirmed and press **Record what was confirmed**. Each attestation is the owner's, with who recorded it and the meeting beside it. It counts at once.
3. **Afterwards**, the owner is told by email or by a card in Teams: what was recorded, by whom, in which meeting, and until when they may object (five business days). One reminder follows after three business days if they have not answered.
4. **The owner confirms or objects**, in Teams, or on the steward screen under *Waiting for you*, *Recorded for you in meetings*. Objecting is an ordinary dispute. When the window closes, what was not objected to **stands**.

The Meetings tab shows, for each recorded fact, how its owner was told and where it stands now: open, confirmed, disputed or stands.

# Signing in

Sign in with your organization account. What you can do depends on your role: `reader`, `steward`, `connector`, `agent` or `admin` (see the Administrator Guide).

# Reviewing the model (owners and stewards)

To write, with screenshots of the steward screen:

- **Your review queue:** entities with fields that are unconfirmed, disputed or expired, filtered to the roles you fill.
- **Attesting:** confirming fields, and what the expiry date means.
- **Correcting:** disputing a value and entering the right one; when a correction takes effect.
- **Conflicts:** where two sources disagree, which value is being used, and how to fix it at the source.
- **History:** the provenance of a value, and the model as of a date.

# Maintaining the model in the workbook (model editors)

The **VeroMesh model workbook** is the standard format in which an organization gives VeroMesh what its systems of record do not hold: who may decide what and up to what limit, which AI uses are approved, which actions move which KPIs, and the operating model around them. It is an Excel workbook with one sheet per kind of thing; every column is explained in the **Workbook Reference**, generated from the same rules VeroMesh checks against.

## What goes in the workbook, and what comes from systems

Use the workbook for facts no system holds, and let connectors bring the rest:

| Facts | Usually from | Workbook sheets |
| --- | --- | --- |
| People, managers, job roles | Workday or another HR system, through its connector | People and Roles only for roles HR does not hold (boards, committees) |
| Accounts, groups, AI agents' sign-ins | Microsoft Entra ID, through its connector | Agents, for what Entra ID does not say: the actions an agent performs |
| Approval limits and delegations | Often a delegation of authority policy, not a system | Authorities, Co-approvals, Policies |
| Approved AI uses | The AI review board's register or minutes | AI use cases |
| Consequential actions and what they affect | The process owners | Actions, Impacts, Processes |
| Objectives, KPIs, capabilities | The operating model, the strategy office | Objectives, KPIs, Capabilities |
| Systems, data products, data elements | The CMDB and the data catalog, once connected | Systems, Data products, Data elements, Column bindings |

A workbook may cover only some sheets. Each workbook is loaded as its own **source** (for example `governance`), so the AI board's sheets and finance's sheets can be kept by different teams and loaded separately.

## Filling it in

1. **Get the template.** `veromesh-connect spreadsheet template -o model.xlsx` writes an empty workbook built from your installation's own rules, with a "How to use" sheet; `--example` fills it with a worked procure-to-pay example to copy from. Both are also in the repository, ready to download: [docs/guides/examples](examples/). To change a source that is already loaded, start from its export: `veromesh-connect spreadsheet export --source governance -o governance.xlsx`.
2. **Fill the sheets in order.** Rows refer to each other by ID, so fill the things referred to first:
    1. Roles (only those HR does not hold), Objectives, KPIs, Capabilities, Processes;
    2. Policies, then Actions, then Impacts (which KPI or capability each action moves, and from what amount it is material);
    3. Authorities (who may take which actions, up to what limit, from when to when), then Co-approvals (second sign-offs above an amount);
    4. Agents (each acts for one person), then AI use cases (which agents may run, for which actions).
3. **Follow the conventions.** IDs are short and stable: letters, digits, `.`, `_` or `-` (`APManager`, `ApproveSupplierInvoice`). Several values in one cell are separated by `;`. Dates are `YYYY-MM-DD`; amounts are plain numbers with the currency in its own column. Required columns are in dark blue; hover over a header for its meaning.
4. **Refer to what VeroMesh already holds.** A reference may point at a person loaded from Workday or a role from Entra ID: use their ID in VeroMesh (an employee ID, or a role's ID). You do not need to copy them into the workbook.

## Checking and loading it

1. **Check it first:** `veromesh-connect spreadsheet import governance.xlsx --source governance --dry-run`. Nothing is written. Every finding names the sheet, row and column, for example *Authorities row 7, Limit amount: not a number*, or *Agents row 3, Acts for: No entity with ID 'E1234'*.
2. **Fix and repeat** until it reports no errors or violations. Warnings (for example a person with no employee ID) do not block a load.
3. **Load it** with the same command without `--dry-run`. The workbook becomes the whole of that source: a row removed from the workbook is removed from VeroMesh. Loading the same file again changes nothing.
4. **Keep it current.** Change the workbook and load it again whenever a limit, a delegation or an approval changes. Owners then confirm what the workbook says on the steward screen, like any other fact.

## Common findings

| Finding | What it means | Fix |
| --- | --- | --- |
| No entity with ID '…' | The row refers to an ID that is in neither this workbook nor VeroMesh | Check the spelling, or load the source that holds it first |
| Required value missing | A dark blue column is empty | Fill it in |
| Only one value allowed | A single-value column has several, separated by `;` | Keep one |
| Unknown column | A header does not match the template | Start from a fresh template, or correct the header |
| Still referenced by … | A row you removed is used elsewhere (for example a person an agent acts for) | Change the rows that use it first |

# Data elements and column bindings

To write: defining critical data elements, binding them to catalog columns, and what agents see about sensitivity and data categories.

# How agents check before they act

An agent asks VeroMesh whether an action is allowed **before** it takes it. VeroMesh answers allow, deny or escalate, with the reasons: the authority relied on, its limit and validity, the approved use case, and any conditions. Every answer is written to the decision log.

To write:

- Reading a decision and its reasons.
- Overrides: who may override a denial, and how it is recorded.
- Looking up past decisions, and checking the log has not been altered.

# Building on VeroMesh (agent builders)

To write:

- **REST API:** signing in as an agent, the decision check, impact queries. The interactive reference is at `/docs` on your installation.
- **MCP:** the tools the MCP server offers and how to connect an MCP client.
- **Microsoft Copilot Studio:** see `docs/copilot-studio.md`.

# Glossary

See also the **Workbook Reference** for every sheet and column of the model workbook.


To write: the shared terms (attestation, authority, changeset, conflict, effective model, source, steward and so on).
