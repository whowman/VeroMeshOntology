# The MCP gateway: enforcing the decision check

The decision check (`POST /api/v1/decisions/check`, the `check_action` MCP tool) is advisory: an agent that skips it can still act. The gateway closes that gap (gap G3, option C). The agent signs in to VeroMesh at `/gateway/mcp` instead of to the systems it acts on, and VeroMesh holds the credentials for those systems. Every call to a consequential tool is checked as the agent, on the decision log, before it reaches the system.

```text
agent ──MCP──▶ VeroMesh /gateway/mcp ──check──▶ allow ──MCP──▶ the system's MCP server
                                         │
                                         ├──▶ refuse: the call never leaves VeroMesh
                                         └──▶ consult: held until the owners answer
```

## What happens to a call

| Decision | What the gateway does | What the agent gets |
| --- | --- | --- |
| allow | Sends the call to the system at once and logs the outcome against the decision: done (with a summary of the system's answer) or not done when the system fails | The system's answer, plus the decision ID |
| refuse | Nothing leaves VeroMesh | The reasons, and who the decision goes to when no authority covers it |
| consult | Holds the call on the decision log (a `gateway` entry) and the owners are asked as for any decision (the steward screen, email or Teams) | A reference; `gateway_status` with it says *still held*, *done* (with the system's answer) or *rejected* |

When the owners approve, the held call goes through straight after the answer. A sweep every minute (`VEROMESH_GATEWAY_INTERVAL_S`) catches approvals given in Teams or while the service was down, and `POST /api/v1/gateway/run` runs it now. A rejection discards the call.

Before sending, the gateway logs a claim (`gateway` entry, outcome `sending`). The first claim on the log wins, so two copies of the service that see the same approval never send the call twice. A call whose sender stopped between the claim and the outcome stays at *being sent*: check the system, then record the outcome on the steward screen.

Because the held call is kept on the hash-chained log, a restart loses nothing. The log also shows what the agent asked, what was decided and what the system answered, in order.

## Which tools are offered

An administrator registers each upstream MCP server (a *target*). Only two kinds of tool are offered to agents, named `<target>__<tool>`:

- **Mapped tools** take a VeroMesh action, and every call is checked. The mapping names the arguments that carry the amount, the person the action is about, the value (percent, grade steps) and a note for the log (for example the invoice number). The currency can be an argument or fixed.
- **Pass-through tools** only read (`passThrough`). They go to the system unchecked and are not logged as decisions.

A tool that is neither is not offered. A target that cannot be reached hides only its own tools.

## Settings

Administrators set the targets with `PUT /api/v1/gateway/settings` (`GET` reads them). Each change goes on the decision log as a configuration entry.

```json
{
  "targets": [
    {
      "name": "erp",
      "url": "https://erp-mcp.example.com/mcp",
      "header": "Authorization",
      "secretEnv": "GATEWAY_ERP_TOKEN",
      "tools": {
        "approve_invoice": {
          "action": "ApproveSupplierInvoice",
          "amount": "amount",
          "currency": "currency",
          "note": "invoice_id"
        },
        "give_raise": {
          "action": "ApproveSalaryIncrease",
          "about": "employee_id",
          "value": "percent"
        }
      },
      "passThrough": ["get_invoice", "list_suppliers"]
    }
  ]
}
```

| Key | Meaning |
| --- | --- |
| `name` | Lowercase letters, digits and `-`, up to 31 characters; prefixes its tools |
| `url` | The system's MCP endpoint (Streamable HTTP) |
| `header`, `secretEnv` | The header sent to the system, and the environment variable holding its value. Secrets are never kept in the settings |
| `tools` | For each mapped tool: `action` (a VeroMesh action ID), and which arguments carry `amount`, `currency`, `about`, `value` and `note`. Use `currencyValue` instead of `currency` for a fixed currency |
| `passThrough` | Tools that only read |

On Kubernetes, put the tokens in a Secret and name it in `api.gateway.existingSecret`; each key becomes an environment variable:

```bash
kubectl -n veromesh create secret generic veromesh-gateway \
  --from-literal=GATEWAY_ERP_TOKEN="Bearer <token>"
helm upgrade veromesh deploy/helm/veromesh --reuse-values --set api.gateway.existingSecret=veromesh-gateway
```

## Signing in

Agents sign in at `/gateway/mcp` as they do at `/mcp`: as themselves (client credentials, the agent role) or as a person through their agent's app registration (docs/copilot-studio.md). The agent is always the one its sign-in names. With sign-in off (development only), the agent is named in the `X-VeroMesh-Agent` header.

For the gateway to enforce anything, the agent must have no other way into the system: give the system's credentials to VeroMesh, not to the agent.

When the licence has lapsed (read-only), the gateway keeps working, as the decision check does.

## Limits

- A held call waits as long as the decision does. Once approved, it is sent with the arguments the agent first gave.
- The gateway sees the tool's arguments, not what the system does with them. Map each consequential tool to the action that describes it, and keep pass-through for tools that only read.
- Upstream servers are reached over Streamable HTTP. A system without an MCP server needs one in front of it (most vendors now publish one).
