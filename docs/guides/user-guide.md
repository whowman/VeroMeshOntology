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

To write:

1. Download a template or an export of a source (`veromesh-connect spreadsheet template` / `export`).
2. Edit: one sheet per kind, IDs, references between sheets.
3. Check it with a dry run, and read the findings (each points to a sheet, row and column).
4. Import it.

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

To write: the shared terms (attestation, authority, changeset, conflict, effective model, source, steward and so on).
