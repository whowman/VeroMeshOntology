# A Copilot Studio agent that asks VeroMesh first

A Copilot Studio agent uses VeroMesh as an MCP tool. Before a consequential action it
calls `check_action` and follows the answer: act and inform, consult the named owners
first, or refuse. Every answer is in the decision log. This page sets up the procure-to-pay
example's **invoice assistant**, acting for Dana (the AP manager), in a Microsoft tenant.

Nothing here is Microsoft-only on the VeroMesh side: `/mcp` takes OpenID Connect tokens
from whichever provider the install uses (`service/README.md`, "Sign-in").

## What you need

- VeroMesh running with sign-in through Entra ID (`api.auth` in the Helm chart), reachable
  over HTTPS from Copilot Studio, at `https://<veromesh>/mcp`.
- The VeroMesh API's app registration (from the sign-in setup), with:
  - app roles `VeroMesh.Admin`, `.Steward`, `.Connector`, `.Reader`, `.Agent`;
  - an Application ID URI, e.g. `api://veromesh-<customer>`;
  - a delegated scope, e.g. `access_as_user` (Expose an API > Add a scope).
- Copilot Studio with permission to add tools to an agent.

## Two ways the agent can sign in

| | The person, through the agent (recommended for Copilot) | The agent alone |
| --- | --- | --- |
| Token | Delegated: Dana signs in once, in the chat | App-only: client credentials |
| VeroMesh sees | Dana, through the agent's app registration | The agent |
| Who may use it | Only the person the agent acts for; anyone else is refused | Whoever runs the agent |
| Copilot Studio auth | OAuth 2.0, **Manual** | Not offered for MCP tools; for agents run elsewhere |

VeroMesh links either to the Agent in the model by the agent's **client ID** (`azp`).

## Steps (delegated, the recommended way)

1. **Register the agent's app** (Entra admin center > App registrations > New registration),
   e.g. "Invoice assistant (Copilot)", single tenant. Note its **Application (client) ID** and
   create a **client secret** (Certificates & secrets).
2. **Let it call VeroMesh for the user:** API permissions > Add a permission > My APIs >
   the VeroMesh API > Delegated > `access_as_user`; grant admin consent.
3. **Give Dana a VeroMesh role** (Enterprise applications > the VeroMesh API > Users and
   groups): `VeroMesh.Reader` is enough. Her sign-in must match her person in the model by
   email (`VEROMESH_PERSON_MATCH_FIELD`).
4. **Register the agent in the model:** on the Agent row (`InvoiceAssistant`), set
   **Sign-in client ID** to the client ID from step 1, through the spreadsheet loader or the
   API, and have the facts behind its authority attested (the check relies only on attested facts).
5. **Add VeroMesh as a tool** in Copilot Studio (the agent > Tools > Add a tool > New tool >
   Model Context Protocol):
   - Server name: `VeroMesh`; description: "Who owns what and who may decide what. Call
     check_action before approving, paying or changing anything, and follow its answer."
   - Server URL: `https://<veromesh>/mcp`
   - Authentication: **OAuth 2.0 > Manual**
     - Client ID and Client secret: from step 1
     - Authorization URL: `https://login.microsoftonline.com/<tenant ID>/oauth2/v2.0/authorize`
     - Token URL template and Refresh URL: `https://login.microsoftonline.com/<tenant ID>/oauth2/v2.0/token`
     - Scopes: `api://veromesh-<customer>/access_as_user offline_access`
   - Select **Create**, copy the **callback URL** it shows, and add it to the app
     registration from step 1 (Authentication > Add a platform > Web > Redirect URI).
   - Create the connection (Dana signs in) and **Add to agent**.
6. **Tell the agent to ask first.** Paste into the agent's instructions:

   > Before approving, paying, changing or sending anything on Dana's behalf, call
   > VeroMesh `check_action` with the action ID and the amount and currency. If the outcome is
   > allow, act, tell the people listed under inform, and call `report_outcome` with "done". If
   > it is consult, do not act: ask the people listed under consult, and call `decision_status`
   > until mayAct is true (then act and report "done") or the status is rejected. If it is
   > refuse, do not act; explain the reasons and hand it to the person under escalateTo. Never
   > retry with a smaller amount to get under a limit.

   (The same guidance is in the MCP server's own instructions; Copilot Studio may not show
   those to its orchestrator, so put it in the agent's.)
7. **Try it** in the test pane, as Dana: "Approve invoice INV-1001 from Northwind for 800 USD",
   then 3,000 USD, then 7,000 USD. Expect allow (inform the Controller and the AP manager),
   consult the Controller, and consult the AP manager (Dana), the next authority up above the
   agent's 5,000 USD limit. Above every authority (300,000 USD) the check refuses and escalates
   to Dana. A steward sees each one under
   `GET /api/v1/decisions`. For the consult, the Controller (Lee) answers on the steward screen
   (`https://<veromesh>/steward/`, Waiting for you > Approve); the agent's next
   `decision_status` says it may act. `GET /api/v1/decisions/measures` shows the measures.

## The agent alone (client credentials)

For an agent that runs without a person present (not a Copilot Studio MCP tool today): give
its app registration the **application** permission `VeroMesh.Agent` on the VeroMesh API, with
admin consent, and set the Agent's Sign-in client ID to its client ID. It gets tokens with
the client credentials grant (scope `api://veromesh-<customer>/.default`) and calls `/mcp`
or `POST /api/v1/decisions/check` as itself.

## If it does not work

`who_am_i` (or `GET /api/v1/me`) says what VeroMesh sees: the person, the agent, the
principal, or what is missing. The usual causes:

| Message | Fix |
| --- | --- |
| No agent in the model signs in with client ID ... | Step 4: the Agent's Sign-in client ID |
| Agent InvoiceAssistant acts for DanaExample; you are signed in as ... | Only Dana may use her agent |
| Your sign-in (...) does not match exactly one person | Her email in the model, or the person claim settings |
| Signed in, but no VeroMesh role is assigned | Step 3 |
| The facts behind this authority are not all attested | A steward or owner attests them |
| 401 at /mcp | The scope in step 5, or the API's issuer and audience settings |

## Purpose and the query log

Every read the agent makes is logged on the decision log's hash chain with the agent, the person it acts for and the purpose it states. The MCP read tools (`who_am_i`, `impact_of_action`, `who_owns`, `get_entity`, `decision_status`, `impact_of_data_change`) take an optional `purpose`; tell the agent in its instructions to give one, for example "Give a short purpose with every VeroMesh lookup, such as the invoice you are working on." A lookup without a purpose is still answered.

