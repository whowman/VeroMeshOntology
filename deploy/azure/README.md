# VeroMesh on Azure (dev)

The dev install of VeroMesh, running beside the assessment on the assessment's AKS cluster in West US 2 (issue #19). It gives Copilot Studio an HTTPS address to reach and Entra ID sign-in, for the end-to-end test in [docs/copilot-studio-test.md](../../docs/copilot-studio-test.md).

Status, 27 November 2026: **running**, with the meetings attestation and notices (G9) deployed on 26 November. First installed on 25 November; rebuilt from nothing on 26 November (`reset`, `all`, then a deploy) with no manual steps beyond the ones this install still needs by design ([What is still done by hand](#what-is-still-done-by-hand)). Every problem met on the first install is now handled by the scripts: [Problems met on the first install](#problems-met-on-the-first-install). **One step is due on this install:** `./deploy/azure/setup.sh power-identity`, so the nightly off works (PR #111).

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

## Starting again from a clean install

To prove that a fresh install works, or to clear out test data, remove the install from the
cluster and deploy again. The Azure resources stay: the node pool, Key Vault (purge-protected,
so its name could not be reused for 90 days), the PostgreSQL server, the Entra apps (their
client IDs stay the same), GitHub's settings, and Scott's DNS record and certificate.

```bash
./setup.sh on                        # if it is off
./setup.sh reset                     # lists what goes, then asks you to type veromesh-dev
./setup.sh reset --decision-log      # the same, and empties the decision log too
./setup.sh all                       # recreates the namespace and its Secrets; skips the rest
```

Then **Actions > Deploy to Azure (dev) > Run workflow**. `reset` uninstalls the three helm
releases (VeroMesh, DataHub, its prerequisites) and deletes the `veromesh-dev` namespace with
every volume and Secret in it; `--decision-log` also drops the decision log's database (its
user stays). It never touches the assessment, its gateway or certificate. Set
`CONFIRM=veromesh-dev` to run it without the question.

## Turning it off and on

The ontology tool runs on its own node pool (`veromesh`), so it can be turned off without touching the assessment, which keeps its own two nodes, its database and its gateway.

| | From GitHub (also the GitHub app on a phone) | From Cloud Shell |
| --- | --- | --- |
| Off | Actions > **Ontology on/off** > Run workflow > `off` | `./deploy/azure/setup.sh off` |
| On | the same, `on` (it waits until VeroMesh answers, about 5 to 10 minutes) | `./deploy/azure/setup.sh on` |
| Which is it? | the same, `status` | `./deploy/azure/setup.sh status` |

- **Off** scales the pool to zero nodes. VeroMesh, Fuseki and DataHub stop. Their data stays on its disks, and so does the decision log on the PostgreSQL server. Turning it back on brings everything back as it was.
- **Cost:** off, about $8 a month (the disks that keep its data). On, about $0.27 an hour; running it 40 hours a month comes to roughly $20 with the disks. Azure bills the nodes by the hour.
- **Off every night:** the workflow also turns the tool off at about 11 PM Eastern (04:07 UTC, so about midnight during daylight saving time), and again at 06:37 UTC in case GitHub started the first run late or not at all, so a day of testing never runs on overnight. Turn it on again when you need it. To change the time, edit the `cron` lines in `.github/workflows/ontology-power.yml`. GitHub starts scheduled runs late when it is busy (we have seen hours), and it pauses schedules in a repository with no activity for 60 days (Actions shows a button to turn it back on).
- **No approval needed:** the on/off workflow runs from `main` with its own identity, `id-github-ontology-power`. That identity may scale this one node pool and nothing else: not the assessment's nodes, and not deploys. Because the pool sits in our own virtual network, scaling it also needs the right to join that pool's subnet, so the identity has Network Contributor on that one subnet. An install made before that was added fails at turning off with `LinkedAuthorizationFailed`; fix it with `./deploy/azure/setup.sh power-identity`.
- **A deploy** needs the tool on: the deploy workflow stops with a message if it is off.
- **Never stop the whole cluster** (`az aks stop`): that would take the assessment's live site down too.
- Every part of the ontology tool, DataHub's one-off set-up jobs included, is pinned to the `veromesh` pool (`nodeSelector: workload: veromesh`). None of it runs on the assessment's nodes, and all of it stops when the pool is off.

## What is still done by hand

These steps are outside what `setup.sh` and the workflows can do, or need a person on purpose.

| Step | Who | When | How |
| --- | --- | --- | --- |
| Raise the B-series vCPU quota | Rich | First install, if `setup.sh` says "Not enough quota" | Azure portal > Quotas > Compute, West US 2, "Standard BSv2 Family vCPUs": ask for the number it prints (10 here: the assessment's 4 and this pool's 6). Usually granted in minutes. Then run `setup.sh all` again |
| The Entra role Application Developer | Entra administrator | First install | Needed to register the two Entra apps |
| DNS record `dev-ontology.veromesh.ai` | Scott (Cloudflare) | First install | Proxied, to the assessment's IP (`setup.sh show` prints it) |
| Origin certificate for the name | Scott, then Rich | First install, if `setup.sh` says it is not covered | Scott issues it; `setup.sh tls <cert.pem> <key.pem>` installs it; delete the files |
| Approve each deploy | Rich | Every deploy | Actions > Deploy to Azure (dev) waits for the `azure-dev` environment's reviewer |
| Give the on/off identity its subnet right | Rich | Once, on installs made before 27 November | `./deploy/azure/setup.sh power-identity` (PR #111) |
| Teams for notices (optional) | Rich, a Teams administrator, Scott | When notices should go to Teams | `./deploy/azure/setup.sh teams` (asks before creating; free tier; keeps the secret and tells the deploy workflow), deploy again, then upload the app, a setup policy, and a Cloudflare rule that lets `POST /api/v1/teams/messages` through without a challenge ([deploy/teams/README.md](../teams/README.md)) |
| Mail for notices | Rich | When notices should go by email | Administration > Usage: the mail adapter (SMTP, sendmail or Microsoft 365); Administration > Notices: the channel |

## Problems met on the first install

Each problem below stopped the first install on 25 November and was then fixed in the scripts or values, so a new install does not meet it: the rebuild on 26 November ran with none of these steps. The last column is only for an install that is still in the broken state.

| What you saw | Why | Fixed in | On an install already in that state |
| --- | --- | --- | --- |
| `setup.sh` said there was not enough quota when there was, or failed reading it | The quota query printed one value per line | #86; the check is skipped once the pool exists (#88) | Run `setup.sh all` again |
| `az postgres flexible-server db create`: unrecognized argument `-d` | Azure CLI 2.86 renamed it `--name` | #87 | Run `setup.sh all` again |
| GitHub deploy: `AADSTS700213` (no matching federated identity) | GitHub now sends an ID-based OIDC subject; only the name-based one was trusted | #89: each identity trusts both | `setup.sh all` adds the missing credential |
| DataHub's PostgreSQL pod: `ErrImagePull` / `ImagePullBackOff` | Bitnami removed its images from Docker Hub | #91, #93: a pinned archive build, copied into the registry by `setup.sh all` | A StatefulSet does not replace a pod stuck pulling an image: after the fixed deploy, `kubectl -n veromesh-dev delete pod prerequisites-postgresql-0` |
| DataHub's setup job: database `datahub` does not exist | The database was not created when PostgreSQL first started | #94: `auth.database` creates it | Only made on first start, so an existing volume needs it by hand: `kubectl -n veromesh-dev exec prerequisites-postgresql-0 -- bash -c 'PGPASSWORD=$POSTGRES_PASSWORD psql -U postgres -c "CREATE DATABASE datahub"'` |
| DataHub's system update and GMS failed authenticating | With token sign-in off, DataHub 1.8 still needs `DATAHUB_SYSTEM_CLIENT_SECRET` | #95, #96 | Deploy again |
| DataHub's UI image: pulled from `docker.io/...acrveromesh...` | The chart prefixes the registry unless `image.registry` is set | #96 | Deploy again |
| The API rejected every token after a deploy | `helm --set` split the audience list at its comma | #98: escaped | Deploy again |
| The steward screen showed the yellow development bar with sign-in on | A CSS rule overrode `hidden` | #100 | Deploy again |
| Actions > Ontology on/off did nothing for `on` or `off` | YAML read the unquoted choices as true and false | #101 | Nothing: run it again |
| The nightly off failed: `LinkedAuthorizationFailed` on `subnets/join/action` | Scaling a pool in our own virtual network needs the right to join its subnet | #111 | `./deploy/azure/setup.sh power-identity` |
| The nightly off started nine hours late, or not at all | GitHub delays scheduled runs at busy times, worst on the hour | #111: 04:07 UTC, and again at 06:37 UTC | Nothing |

## Notes

- **The gateway.** The listener lives on the assessment's gateway, which the assessment's `setup.sh gateway` re-applies from its own file. After that, run `./deploy/azure/setup.sh gateway` here again.
- **DataHub** has no public route and no tokens in dev: GMS is reachable only inside the namespace. To see the UI, run `kubectl -n veromesh-dev port-forward svc/datahub-datahub-frontend 9002:9002`.
- **Backups** run nightly to a volume in the cluster, encrypted to an SSH key whose private half is in the Key Vault (`backup-key`). This is for dev only; production backs up off-site (deploy/helm/README.md).
- **Fuseki** runs as one node (no read replicas) to save memory.
- **Removing it:** `helm -n veromesh-dev uninstall veromesh datahub prerequisites`, delete the namespace, then `az aks nodepool delete -g VeroMeshAI --cluster-name aks-veromesh -n veromesh`. The database, the vault and the Entra apps cost nothing to keep.
