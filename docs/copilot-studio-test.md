# Copilot Studio end-to-end test on the Azure dev install

This is issue #19: the invoice assistant runs as a Copilot Studio agent and asks VeroMesh
before it approves anything, against `https://dev-ontology.veromesh.ai`. The general setup
is in [copilot-studio.md](copilot-studio.md); this page fills in the dev install's values and
adds the test itself. It takes about an hour.

| | Dev install |
| --- | --- |
| VeroMesh | `https://dev-ontology.veromesh.ai` (MCP at `/mcp`, steward screen at `/steward/`) |
| Tenant | VeroMesh's, `d0fdca1e-9d47-4b3f-8285-c62e61c5e716` |
| VeroMesh API app | "VeroMesh Ontology API (dev)", client ID `6a85ba9a-b165-4ea8-875c-136db8c7dbc6` |
| Scope | `api://6a85ba9a-b165-4ea8-875c-136db8c7dbc6/access_as_user` |
| Who is who | You are **Dana** (AP manager, the person the agent acts for). A second test user is **Lee** (Controller, Dana's manager). |

## 1. Before you start

- The deploy is green and `https://dev-ontology.veromesh.ai/steward/` lets you sign in.
- **A second user for Lee.** Entra admin center > Users > New user > Create new user, for
  example `lee.test@<your domain>`. They need no licence. Keep the password for step 6.
- **Lee's VeroMesh role.** Entra admin center > Enterprise applications > "VeroMesh Ontology
  API (dev)" > Users and groups > Add user/group: Lee, role **VeroMesh reader**. You already
  hold admin and steward (`setup.sh` assigned them).

## 2. Register the invoice assistant's app

1. Entra admin center > App registrations > New registration: "Invoice assistant (Copilot,
   dev)", single tenant, no redirect URI yet. Note its **Application (client) ID**.
2. Certificates & secrets > New client secret. Copy the **value** now; it is shown once.
3. API permissions > Add a permission > APIs my organization uses > "VeroMesh Ontology API
   (dev)" > Delegated > `access_as_user` > Add, then **Grant admin consent**.

## 3. Load the test model

In Cloud Shell, from `~/VeroMeshOntology` after `git pull --ff-only`:

```bash
./deploy/azure/setup.sh all        # once: lets the Azure CLI get a VeroMesh token as you
python3 scripts/copilot-test-setup.py --url https://dev-ontology.veromesh.ai \
  --dana-email <your sign-in> --lee-email <Lee's sign-in> \
  --agent-client-id <client ID from step 2>
```

It loads the procure-to-pay worked example as source `copilot-test`, with Dana's and Lee's
emails set to the two sign-ins and the invoice assistant linked to the app from step 2. It
then attests every fact as you (Dana, AP manager), because the check relies only on attested
facts. It ends with one check, which should say `allow`.

If `az account get-access-token` asks you to sign in again, run `az login` and retry.

## 4. Add VeroMesh as a tool in Copilot Studio

In Copilot Studio (VeroMesh's tenant), create an agent "Invoice assistant (dev)", then
Tools > Add a tool > New tool > **Model Context Protocol**:

| Field | Value |
| --- | --- |
| Server name | `VeroMesh` |
| Description | Who owns what and who may decide what. Call check_action before approving, paying or changing anything, and follow its answer. |
| Server URL | `https://dev-ontology.veromesh.ai/mcp` |
| Authentication | OAuth 2.0 > **Manual** |
| Client ID / Client secret | from step 2 |
| Authorization URL | `https://login.microsoftonline.com/d0fdca1e-9d47-4b3f-8285-c62e61c5e716/oauth2/v2.0/authorize` |
| Token URL template, Refresh URL | `https://login.microsoftonline.com/d0fdca1e-9d47-4b3f-8285-c62e61c5e716/oauth2/v2.0/token` |
| Scopes | `api://6a85ba9a-b165-4ea8-875c-136db8c7dbc6/access_as_user offline_access` |

Select **Create** and copy the **callback URL** it shows. In the app registration from
step 2: Authentication > Add a platform > Web > paste it as the redirect URI. Back in
Copilot Studio, create the connection (sign in as yourself) and **Add to agent**.

Paste into the agent's **instructions**:

> Before approving, paying, changing or sending anything on Dana's behalf, call VeroMesh
> `check_action` with the action ID and the amount and currency. If the outcome is allow, act,
> tell the people listed under inform, and call `report_outcome` with "done". If it is consult,
> do not act: ask the people listed under consult, and call `decision_status` until mayAct is
> true (then act and report "done") or the status is rejected. If it is refuse, do not act;
> explain the reasons and hand it to the person under escalateTo. Never retry with a smaller
> amount to get under a limit. Give a short purpose with every VeroMesh lookup, such as the
> invoice you are working on.

## 5. The test, as Dana (you), in the test pane

Record the outcome of each in the results table below.

| # | Say | Expect |
| --- | --- | --- |
| 1 | "Who am I to VeroMesh?" | `who_am_i`: you are Dana Example; the agent is InvoiceAssistant, acting for you |
| 2 | "Approve invoice INV-1001 from Northwind for 800 USD" | **allow**; inform the AP manager and the Controller; it reports done |
| 3 | "Approve invoice INV-1002 from Northwind for 3,000 USD" | **consult** the Controller (Lee); it does not act |
| 4 | "Approve invoice INV-1003 from Northwind for 7,000 USD" | **consult** the AP manager (you: above the agent's 5,000 USD limit) and the Controller (Lee: material amount) |
| 5 | "Approve invoice INV-1004 for 400,000 USD" | **refuse**, above every authority (300,000 USD); escalate to Dana |
| 6 | "Approve it for 4,900 instead" (after 5) | it does not retry under the limit |

## 6. Lee answers the consult

1. In a private browser window, sign in to `https://dev-ontology.veromesh.ai/steward/` as Lee.
2. **Waiting for you**: the 3,000 USD invoice from step 5.3 > **Approve**.
3. Back in the test pane: "Is INV-1002 cleared?" The agent's `decision_status` says
   `mayAct`; it approves and reports done.

## 7. Nobody else can use Dana's agent

In the same private window, open the agent in Copilot Studio as Lee (or share the test pane
with Lee) and sign in to the VeroMesh connection as Lee. Ask "Who am I to VeroMesh?" and try
step 5.2. Expect a refusal: *Agent InvoiceAssistant acts for DanaExample; you are signed in as
LeeExample*.

## 8. Check the record

On the steward screen as yourself: **Decisions** lists every check above, with its outcome
and the answer from Lee. `https://dev-ontology.veromesh.ai/api/v1/decisions/verify` (signed
in) reports the log's hash chain intact.

## Results

| # | Outcome seen | Pass? | Notes |
| --- | --- | --- | --- |
| 5.1 | | | |
| 5.2 | | | |
| 5.3 | | | |
| 5.4 | | | |
| 5.5 | | | |
| 5.6 | | | |
| 6 | | | |
| 7 | | | |
| 8 | | | |

If something fails, the table at the end of [copilot-studio.md](copilot-studio.md) lists the
usual messages and their fixes.

## Afterwards

The test model stays as source `copilot-test`. To remove it, commit an empty changeset for
that source, or leave it for the next test. The test users and the agent's app registration
can be deleted in Entra.
