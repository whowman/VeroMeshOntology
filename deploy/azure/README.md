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

Pay-as-you-go prices for West US 2, November 2026. Stopping the node pool (`az aks nodepool scale ... --node-count 0`) stops most of it.

## First install

1. **Azure (Rich, about 20 minutes).** In the Azure portal, open Cloud Shell (Bash):

   ```bash
   gh auth login                       # GitHub, so the script can set the environment and its variables
   gh repo clone whowman/VeroMeshOntology && cd VeroMeshOntology
   ./deploy/azure/setup.sh all
   ```

   It checks the B-series vCPU quota first (it needs 6 more), then adds the node pool, the Key Vault, the database and its user, the namespace's Secrets, a listener for the host on the assessment's gateway, the two Entra apps (and gives you VeroMesh.admin and VeroMesh.steward), and the GitHub deploy identity. With `gh` signed in it also creates the `azure-dev` environment with you as its required reviewer, and sets its variables. Registering the Entra apps needs the Entra role Application Developer.
2. **Cloudflare (Scott).** A proxied DNS record `dev-ontology.veromesh.ai` to the assessment's IP address (`./deploy/azure/setup.sh show` prints it). If the assessment's origin certificate does not cover the name (`setup.sh` says so), Scott issues one. Install it from Cloud Shell, then delete the files: `./deploy/azure/setup.sh tls ontology-origin.pem ontology-origin.key`.
3. **First deploy.** Actions > Deploy to Azure (dev) > Run workflow (DataHub ticked), then approve it. It builds and pushes the images (about 40 minutes with DataHub), installs DataHub's prerequisites, DataHub and VeroMesh, applies the route, and runs `helm test`.
4. **Check.** https://dev-ontology.veromesh.ai/steward/ signs you in with Entra ID. Load the example from https://dev-ontology.veromesh.ai/docs, or load a connector run.
5. **Copilot Studio (issue #19).** Follow [docs/copilot-studio.md](../../docs/copilot-studio.md) with this address: the invoice assistant's app registration, its sign-in client ID in the model, the MCP tool, then the test as Dana and Lee.

## Notes

- **The gateway.** The listener lives on the assessment's gateway, which the assessment's `setup.sh gateway` re-applies from its own file. After that, run `./deploy/azure/setup.sh gateway` here again.
- **DataHub** has no public route and no tokens in dev: GMS is reachable only inside the namespace. To see the UI, run `kubectl -n veromesh-dev port-forward svc/datahub-datahub-frontend 9002:9002`.
- **Backups** run nightly to a volume in the cluster, encrypted to an SSH key whose private half is in the Key Vault (`backup-key`). This is for dev only; production backs up off-site (deploy/helm/README.md).
- **Fuseki** runs as one node (no read replicas) to save memory.
- **Removing it:** `helm -n veromesh-dev uninstall veromesh datahub prerequisites`, delete the namespace, then `az aks nodepool delete -g VeroMeshAI --cluster-name aks-veromesh -n veromesh`. The database, the vault and the Entra apps cost nothing to keep.
