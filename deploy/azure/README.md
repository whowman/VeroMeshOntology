# VeroMesh on Azure (dev)

The dev install of VeroMesh, running beside the assessment on the assessment's AKS cluster in West US 2 (issue #19). It gives Copilot Studio an HTTPS address to reach and Entra ID sign-in, for the end-to-end test in [docs/copilot-studio.md](../../docs/copilot-studio.md).

| | |
| --- | --- |
| Address | https://dev-ontology.veromesh.ai: `/steward/`, `/docs`, `/mcp` |
| Namespace / releases | `veromesh-dev`: `prerequisites` and `datahub` (DataHub), `veromesh` |
| Updated | By hand: Actions > Deploy to Azure (dev) > Run workflow; each run waits for your approval |
| Data | Test data only |

```
Copilot Studio, a browser --HTTPS--> Cloudflare --> the assessment's gateway (listener for dev-ontology.veromesh.ai)
  --> veromesh-api (service, /mcp, /steward) --> Fuseki (the model)
                                              --> DataHub GMS (in the namespace, no public route)
                                              --> PostgreSQL Flexible Server (the assessment's): database veromesh_decisions_dev (the decision log)
  Sign-in: Entra ID, app "VeroMesh Ontology API (dev)" (app roles VeroMesh.admin, .steward, .connector, .reader, .agent)
```

What it reuses from the assessment: the cluster, the container registry, the PostgreSQL server, the fixed IP and the gateway. What it adds:

| Added | Why | About a month |
| --- | --- | ---: |
| Node pool `veromesh`: 3 x B2s_v2 (2 vCPU, 8 GB), 64 GB disks | VeroMesh, Fuseki and DataHub (OpenSearch, Kafka, PostgreSQL, GMS, UI) | $182 + $15 disks |
| Disks for Fuseki, OpenSearch, Kafka, DataHub's PostgreSQL, backups | Persistent volumes, about 45 GB | $7 |
| A database on the existing PostgreSQL server | The decision log | $0 |
| A Key Vault, a managed identity, two Entra apps | Secrets, the GitHub deploy, sign-in | under $1 |
| **Total** | | **about $205** |

Pay-as-you-go prices for West US 2, November 2026. Turned off it costs about $8 a month: see [Turning it off and on](#turning-it-off-and-on).

## First install

1. **Azure (Rich, about 20 minutes).** In the Azure portal, open Cloud Shell (Bash):

   ```bash
   gh auth login                       # GitHub, so the script can set the environment and its variables
   gh repo clone whowman/VeroMeshOntology && cd VeroMeshOntology
   ./deploy/azure/setup.sh all
   ```

   It checks the B-series vCPU quota first (it needs 6 more), then adds the node pool, the Key Vault, the database and its user, the namespace's Secrets, a listener for the host on the assessment's gateway, the two Entra apps (and gives you VeroMesh.admin and VeroMesh.steward), the GitHub deploy identity, and the on/off identity. With `gh` signed in it also creates the `azure-dev` environment (with you as its required reviewer) and the `azure-dev-power` environment (no reviewer, `main` only), and sets their variables. Registering the Entra apps needs the Entra role Application Developer.
2. **Cloudflare (Scott).** A proxied DNS record `dev-ontology.veromesh.ai` to the assessment's IP address (`./deploy/azure/setup.sh show` prints it). If the assessment's origin certificate does not cover the name (`setup.sh` says so), Scott issues one. Install it from Cloud Shell, then delete the files: `./deploy/azure/setup.sh tls ontology-origin.pem ontology-origin.key`.
3. **First deploy.** Actions > Deploy to Azure (dev) > Run workflow (DataHub ticked), then approve it. It builds and pushes the images (about 40 minutes with DataHub), installs DataHub's prerequisites, DataHub and VeroMesh, applies the route, and runs `helm test`.
4. **Check.** https://dev-ontology.veromesh.ai/steward/ signs you in with Entra ID. Load the example from https://dev-ontology.veromesh.ai/docs, or load a connector run.
5. **Copilot Studio (issue #19).** Follow [docs/copilot-studio.md](../../docs/copilot-studio.md) with this address: the invoice assistant's app registration, its sign-in client ID in the model, the MCP tool, then the test as Dana and Lee.

## Turning it off and on

The ontology tool runs on its own node pool (`veromesh`), so it can be turned off without touching the assessment, which keeps its own two nodes, its database and its gateway.

| | From GitHub (also the GitHub app on a phone) | From Cloud Shell |
| --- | --- | --- |
| Off | Actions > **Ontology on/off** > Run workflow > `off` | `./deploy/azure/setup.sh off` |
| On | the same, `on` (it waits until VeroMesh answers, about 5 to 10 minutes) | `./deploy/azure/setup.sh on` |
| Which is it? | the same, `status` | `./deploy/azure/setup.sh status` |

- **Off** scales the pool to zero nodes. VeroMesh, Fuseki and DataHub stop. Their data stays on its disks, and so does the decision log on the PostgreSQL server. Turning it back on brings everything back as it was.
- **Cost:** off, about $8 a month (the disks that keep its data). On, about $0.27 an hour; running it 40 hours a month comes to roughly $20 with the disks. Azure bills the nodes by the hour.
- **No approval needed:** the on/off workflow runs from `main` with its own identity, `id-github-ontology-power`. That identity may scale this one node pool and nothing else: not the assessment's nodes, and not deploys.
- **A deploy** needs the tool on: the deploy workflow stops with a message if it is off.
- **Never stop the whole cluster** (`az aks stop`): that would take the assessment's live site down too.
- Every part of the ontology tool, DataHub's one-off set-up jobs included, is pinned to the `veromesh` pool (`nodeSelector: workload: veromesh`). None of it runs on the assessment's nodes, and all of it stops when the pool is off.

## Notes

- **The gateway.** The listener lives on the assessment's gateway, which the assessment's `setup.sh gateway` re-applies from its own file. After that, run `./deploy/azure/setup.sh gateway` here again.
- **DataHub** has no public route and no tokens in dev: GMS is reachable only inside the namespace. To see the UI, run `kubectl -n veromesh-dev port-forward svc/datahub-datahub-frontend 9002:9002`.
- **Backups** run nightly to a volume in the cluster, encrypted to an SSH key whose private half is in the Key Vault (`backup-key`). This is for dev only; production backs up off-site (deploy/helm/README.md).
- **Fuseki** runs as one node (no read replicas) to save memory.
- **Removing it:** `helm -n veromesh-dev uninstall veromesh datahub prerequisites`, delete the namespace, then `az aks nodepool delete -g VeroMeshAI --cluster-name aks-veromesh -n veromesh`. The database, the vault and the Entra apps cost nothing to keep.
