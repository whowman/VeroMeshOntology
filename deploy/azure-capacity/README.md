# Capacity runs on the Azure reference machines

The sizing proposal sells each tier on a tested machine size. These scripts run the capacity measurements on those machines in Azure:
- edits, decision checks per second under load, the nightly re-sync and memory;
- at 20,000, 100,000 and 500,000 people;
- against a real Fuseki and a PostgreSQL decision log.

The same measurements on a GitHub runner are in `docs/testing.md` ("Capacity on Fuseki").

Each run creates one machine, runs the benchmark on it, puts the results in the GitHub run, and deletes the machine, even when the run fails. Machines exist only during a run.

| File | What it is | Where it runs |
| --- | --- | --- |
| `setup.sh` | One-time setup: resource group, network, the GitHub sign-in identity, quota check | Azure Cloud Shell |
| `../../.github/workflows/capacity-azure.yml` | A run: create the machine, measure, delete it | GitHub Actions, started by hand |
| `vm-run.sh` | What runs on the machine: Docker, Fuseki, PostgreSQL, the service, the benchmark | The test machine, through Azure's run command |
| `teardown.sh` | Clean up: list, delete leftover machines, or remove everything | Azure Cloud Shell |

## Cost

The machines are the v6 sizes. The sizing proposal named v5, but this subscription has no v5 quota in West US 2, and Azure refuses to grant it (`QuotaNotAvailableForResource`). The v6 sizes have the same vCPU and memory.

These are pay-as-you-go Linux prices in West US 2. D4s v6 is listed at $0.202 an hour; the others are estimated at the same ratio over v5. Check the [pricing page](https://azure.microsoft.com/pricing/details/virtual-machines/linux/) for current figures. A deleted machine costs nothing; the network and the identity are free.

| Machine | vCPU / memory | Per hour | Run | About | Cost |
| --- | ---: | ---: | --- | ---: | ---: |
| D4s v6 | 4 / 16 GB | $0.202 | `medium large` | 1.5 h | $0.30 |
| D8s v6 | 8 / 32 GB | about $0.40 | `medium large` | 1.5 h | $0.61 |
| E8s v6 | 8 / 64 GB | about $0.53 | `large`, then `grow` | 3 h | $1.59 |
| E16s v6 | 16 / 128 GB | about $1.06 | `xl` (500,000 people) | 3 h | $3.18 |
| **All four** | | | | **9 h** | **about $5.70** |

Allow twice that for runs repeated after a fix. Each run has a ceiling (`hours`, default 6). The most a single run can cost is therefore 6 hours of its machine: about $6.40 for an E16s v6.

## 1. Set up Azure (once, about 5 minutes)

In the [Azure portal](https://portal.azure.com), open Cloud Shell (Bash), and switch to the subscription to use. The assessment's subscription is fine: everything goes in its own resource group, `veromesh-capacity`, and touches nothing else.

```bash
az account set --subscription <subscription id>
git clone --depth 1 https://github.com/whowman/VeroMeshOntology.git
cd VeroMeshOntology/deploy/azure-capacity
./setup.sh                        # or: LOCATION=eastus ./setup.sh
```

It creates:

- the resource group `veromesh-capacity`;
- a network whose security group lets nothing in (the machines can reach the internet for packages and images; nothing can reach them);
- a managed identity for GitHub Actions. It signs in with OIDC, so no password is stored anywhere. It may act only in this resource group, and only for this repository's `capacity` environment.

At the end it checks the vCPU quota, and prints the five values for GitHub.

**Quota.** One machine runs at a time, so each family needs room for its largest machine:
- Dsv6: 8 vCPU, for the D8s v6. The subscription has 10.
- Esv6: 16 vCPU, for the E16s v6 (500,000 people). The subscription has 10.
- Total regional vCPUs: 16 free, room for the largest machine beside whatever already runs in the region (in West US 2 the assessment's AKS uses 4).

**Zones.** Premium SSD v2 (`storage: premium-ssd-v2`) needs a machine in an availability zone. This subscription may not use zones in West US 2 (`NotAvailableForSubscription`; check with `az vm list-skus -l <region> --size Standard_D4s_v6 --all`), so the runs moved to West US 3: `RESOURCE_GROUP=veromesh-capacity-wus3 LOCATION=westus3 ./setup.sh`, then the new client ID, resource group and location in the GitHub environment.

If `setup.sh` reports "TOO LOW", run `./setup.sh raise`, which asks Azure for the Esv6 increase, or raise it in the portal under **Quotas → Compute**. In the portal, choose the region and set the usage filter to show all, since families with nothing in use are hidden by default. It's free and usually approved in minutes. The D runs and the E8s run fit the quota as it is, so they can go ahead meanwhile.

## 2. Set up GitHub (once)

1. In the repository, open **Settings → Environments → New environment**, and name it `capacity`.
2. Under **Required reviewers**, add yourself. Every run then waits for your approval before it creates a machine, which is the guard on spending.
3. Under **Environment variables**, add the five values `setup.sh` printed: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP` and `AZURE_LOCATION`. `./setup.sh show` prints them again.

They are identifiers, not secrets: without the federated credential, nobody can sign in with them.

## 3. Run

**Actions → Capacity on Azure → Run workflow**, then approve it when GitHub asks.

| Input | What it does |
| --- | --- |
| `machine` | The Azure size: `Standard_D4s_v6`, `Standard_D8s_v6`, `Standard_E8s_v6` or `Standard_E16s_v6` |
| `mode` | `bench` times each size. `grow` loads 20,000 people at a time, measuring after each step, until the service holds 60% of the machine's memory |
| `sizes` | For `bench`, the sizes in order: `medium` (20,080 people), `large` (100,200), `xl` (500,000; E machines only) |
| `throughput` | Concurrencies for checks per second, default `1,4,16` |
| `disk_gb` | The machine's premium disk, which Fuseki writes to: `128` (P10, 500 operations a second), `512` (P20, 2,300) or `1024` (P30, 5,000). Premium disks get faster with size. The first D4s v6 run, on 128 GB, wrote 2–4 times slower than a GitHub runner; a larger disk costs about $0.03 (512) or $0.17 (1024) an hour more |
| `storage` | Where Fuseki and PostgreSQL keep their data. `os-disk` is the machine's network-attached premium disk. `local-nvme` is the local NVMe drive of a size that has one (`Standard_D4ds_v6`); it's wiped when the machine stops, so it's for measuring only. `premium-ssd-v2` is an attached 128 GB Premium SSD v2 disk (low latency, durable), with the machine placed in availability zone 1, which that disk needs. Every Fuseki commit waits for the disk: on a D4s v6 an edit took 380–440 ms on the OS disk and 68–83 ms on the local drive |
| `fuseki_heap` | Fuseki's Java heap. `auto` is a quarter of the machine's memory |
| `hours` | The most the run may take on the machine (1–6). It is stopped after that, and the machine is still deleted |
| `keep_machine` | Leave the machine running at the end, to look around. Delete it afterwards with `teardown.sh machines` |

The run order that answers the sizing proposal:

1. `Standard_D4s_v6`, `bench`, `medium large`: the Small and Medium tiers, and how far a 4-vCPU machine carries Large.
2. `Standard_D8s_v6`, `bench`, `medium large`: what doubling the vCPUs buys.
3. `Standard_E8s_v6`, `bench`, `large`. Then the same machine with `grow`, to find where 64 GB runs out.
4. `Standard_E16s_v6`, `bench`, `xl`: the Extra large tier, 500,000 people (issue #46). This needs the Esv6 quota raised to 16. Loading takes most of the run, roughly 45–90 minutes.

Runs go one at a time: a run started while another is going waits for it, because the quota does not fit two machines at once.

## 4. Read the results

The results show in the run's summary page and at the end of the **Measure** step.

**A `bench` run** prints, for each size:
- the load time;
- an edit, a check and finding a person;
- the nightly re-sync and memory;
- the checks-per-second table: 1, 4 and 16 agents, then 16 with an edit every second, with p50, p95, p99, the slowest check and garbage-collector pauses;
- the check as the day's decision log grows.

**A `grow` run** prints one line per step.

Azure returns only the last 4 KB of what the machine prints. Two sizes fit; if a report is cut short, run one size per run. The machine also keeps its full log in `/var/log/veromesh-capacity.log` (use `keep_machine` to read it). Record the numbers in `tests/perf/targets.json` and `docs/testing.md`, and confirm or revise the machine table in the sizing proposal.

## 5. Clean up

The workflow deletes each machine with its disk, network card and public address when its run ends, also on failure or timeout. A run cancelled in GitHub still runs the delete step. Each machine also carries a `deleteAfter` tag (run start + `hours` + 1).

To check, or to clean up after anything unusual, run these in Cloud Shell:

```bash
./teardown.sh list        # what is there, and whether any machine is still running
./teardown.sh machines    # delete every test machine left behind; keeps the setup
./teardown.sh all         # delete the resource group and everything in it (asks first)
```

## If something goes wrong

| What you see | What to do |
| --- | --- |
| "Set the AZURE_* variables on the capacity environment" | Step 2: the variables go on the `capacity` environment, not the repository |
| `AADSTS700213` / `AADSTS70021`: no matching federated identity | The environment must be named exactly `capacity`. The log shows the subject GitHub sent; `setup.sh` registers both forms (by name, and with the numeric IDs). Run it again to add one that is missing |
| `OperationNotAllowed` / quota exceeded when creating the machine | Raise the quota (step 1: `./setup.sh raise`), or run a smaller size meanwhile |
| `QuotaNotAvailableForResource` when raising a quota | That family is not offered to this subscription in the region: choose another family or region |
| `SkuNotAvailable` | That size is not offered in the region now. Run `LOCATION=<other region> ./setup.sh`, then update `AZURE_LOCATION` |
| "The run on the machine ended: Failed" | The output ends with the last 3 KB of the machine's log and the step that failed |
| "TimedOut" | Raise `hours` (up to 6), or run fewer sizes per run |
