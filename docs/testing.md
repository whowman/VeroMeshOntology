# Testing

One command runs every component's tests and prints one summary:

```bash
scripts/test.sh                     # quick tier: lint, unit, contract snapshots (about 2 minutes)
scripts/test.sh integration         # + real Apache Jena Fuseki, the container images and a kind cluster
scripts/test.sh build               # + the DataHub custom-model plugin build (slow)
scripts/test.sh e2e                 # end-to-end against the running stack
scripts/test.sh all --start-stack   # everything; starts the stack first if it is not running
```

You need Docker (Docker Desktop on a Mac) and any `python3`; the tests run inside a test-runner image built from the components' declared dependencies, so nothing else needs installing. The image (which also carries Helm and kubeconform for the chart suite) is rebuilt only when a `pyproject.toml` or `deploy/versions.env` changes. To run without Docker, add `--local`: the tests then run in a virtualenv under `.build/test-venv` (needs Python 3.11+; your conda environment works).

CI runs exactly these commands (`.github/workflows/ci.yml`), so a green run on your machine means the same checks as a green PR.

## What runs

| Tier | Suite | Protects | Checks |
| --- | --- | --- | --- |
| quick | `lint` | all Python | ruff lint and formatting |
| quick | `service` | `service/` | REST model API, changesets, validation, bindings, glossary (in-memory store) |
| quick | `connectors` | `connectors/` | connector SDK, spreadsheet, CSV, Workday and Entra ID connectors, against the real API in-process (Workday and Entra ID against fakes of their public APIs: paging, token renewal, failures, change feeds) |
| quick | `ontology` | `ontology/` | SHACL validation rules fire as intended; every Turtle file parses |
| quick | `branding` | `branding/` | DataHub theme generator (palette mapping, contrast) and asset injector |
| quick | `contracts` | interfaces | golden snapshots (below) |
| quick | `perf` | the service's speed | a synthetic organization of about 200 people loads, and an edit, a decision check, a person lookup and reading all people stay within the budgets in `tests/perf/targets.json` |
| quick | `licenses` | dependencies | no GPL / AGPL / SSPL / Elastic License package; report in the results folder |
| quick | `helm` | `deploy/helm/` | the chart lints and renders for every value set in `ci/`; bad values are refused by the schema; services, selectors and the Fuseki NetworkPolicy line up; backups need the customer's key and go where they are told; with the mesh on, every pod is enrolled, Fuseki takes mutual TLS from the service's and backup job's identities only, and the NetworkPolicy lets the mesh through; every container runs unprivileged with limits; manifests pass Kubernetes schemas (kubeconform); the DataHub overlays render against DataHub's official charts at the pinned versions and load the plugin, brand and PostgreSQL |
| quick | `scripts`, `compose` | `scripts/`, `deploy/` | shell scripts parse; compose file is valid |
| integration | `fuseki` | `service/`, `connectors/` | the whole service and connector suites again, on a real Fuseki |
| integration | `perf-fuseki` | the service's speed on Fuseki | the small synthetic organization (about 2,000 people) on a real Fuseki: timings only, in `perf-fuseki-small.json` among the results |
| integration | `images` | Dockerfiles | service and connector images build, start, and carry their license files |
| integration | `datahub-frontend` | `branding/datahub/` | the VeroMesh-branded DataHub frontend image builds with the theme and logos in place |
| integration | `k8s` | `deploy/helm/` | the chart installs into a throwaway kind cluster with a single-node Kafka; `helm test` passes; the worked example loads through the API; both read replicas hold exactly what the writer Fuseki holds; reads keep working while the writer is down; the model survives a writer restart and later writes reach the replicas; a replica with an empty volume rebuilds from Kafka; a pod other than the service cannot reach the writer or the replicas; an encrypted backup restores the graph on the writer and both replicas, keeps a decision log that holds entries, and brings back a lost one with its hash chain intact (needs kind, kubectl and Helm) |
| integration | `k8s-mesh` | `deploy/mesh/`, `deploy/helm/` | OpenBao, cert-manager, istio-csr and Istio ambient in kind (needs internet), set up with the files in `deploy/mesh/`: istiod has no CA of its own and the service's certificate is signed by OpenBao; the chart's `helm test` passes through the mesh; backups work through it; a pod outside the mesh cannot reach the service; a pod with another identity cannot reach Fuseki |
| integration | `backup` | `deploy/backup/` | the backup image against a real Fuseki and PostgreSQL: named and default graphs, blank nodes and literals survive exactly; files are encrypted to the key; retention; a decision log that holds entries is left alone and an empty one is restored, still append-only; a wrong key, no key or a damaged backup is refused before anything changes |
| integration | `fuseki-replication` | `deploy/fuseki/replication/` | the Fuseki writer module compiles and passes its tests (publish before commit, failures, fencing, splitting, catch-up, seeding) in the image build (needs Docker) |
| build | `datahub-model` | `datahub-model/` | the custom aspect compiles inside DataHub's Gradle build |
| e2e | `e2e` | the running stack | health and plugin loaded; DataHub serves the VeroMesh theme, logos and favicon; ontologies load and publish to the DataHub glossary; bindings reach Fuseki and DataHub and are removed again; the worked example loads through the spreadsheet connector, re-runs as a no-op, exports and re-imports, and a breaking delete is refused |
| e2e | `stack-scripts` | developer scripts | `load-examples.sh` and `veromesh-connect.sh` work against the stack |
| e2e | `persistence` | Fuseki volume | the model survives a Fuseki restart |

**The simulated company.** The [VeroMesh test harness](https://github.com/whowman/VeroMeshTestHarness) generates a company and serves its systems (Workday, Entra ID, ServiceNow, Databricks, the ERP, the EMR, dbt, SharePoint, a semantic layer) through mocks of their public APIs, with each system's quirks recorded as an answer key. With it installed, `connectors/tests/test_against_harness.py` loads a 300-person company through the Workday and Entra ID connectors, plays a month of changes through both change feeds, and repeats the load with 30% of requests misbehaving (429, 500, 503, expired tokens, slow). Its `harness run` puts the harness's scenarios to VeroMesh's API and scores the answers by gate; on 23 November, with Workday and Entra ID loaded, everyone working and everyone with no HR record were right, and the other scenarios name the connector they wait for.

`scripts/test.sh --list` prints the same table from the registry. You can run one suite or one component by name: `scripts/test.sh connectors`, `scripts/test.sh fuseki`, `scripts/test.sh stack`.

The e2e suites work against your development stack. Everything they create is named `regtest`, and they remove it afterwards, so your own sources are left alone.

## Performance

`tests/perf/bench.py` builds a synthetic organization (copies of the worked example, one per domain, plus people filling its roles) and times what users and agents do: load it one domain at a time, edit one fact, read all people, find a person, ask the decision check, read straight after an edit, and re-sync the whole source with one change.

```bash
PYTHONPATH=service/src python tests/perf/bench.py --size small      # smoke, small, medium, large
PYTHONPATH=service/src python tests/perf/bench.py --size smoke --check
PYTHONPATH=service/src python tests/perf/bench.py --domains 20 --people 500 --store fuseki:http://localhost:3030/veromesh
```

CI runs the smoke size against `budgets` in `tests/perf/targets.json`, which are tightened as the work lands so it cannot slip back. `goals` are what each size must reach; `baseline` and `measured` record before and after. On the in-memory store:

| Size | People | Edit one fact | Decision check | Read all people | Find a person |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small, before | 2,020 | 13.1 s | 1.82 s | 300 ms | 100 ms |
| Small | 2,020 | 53 ms | 3 ms | 3 ms | 1 ms |
| Medium | 20,080 | 25 ms | 4 ms | 18 ms | 11 ms |
| Large | 100,200 | 35 ms | 23 ms | 137 ms | under 1 ms |
| Small, on Fuseki (CI) | 2,020 | 78 ms | 32 ms | 4 ms | 2 ms |

Loading the large organization (100 domains, one changeset each) takes 4.5 minutes, 2.4 s for the last domain of 1,000 people; it took 22.5 minutes before loads were made proportional to the batch. A nightly sync of the whole source as one replace load with one change takes 0.7 s for 2,290 entities and 30 s for 102,900.

### Capacity on Fuseki

The capacity job (Actions → Capacity, `.github/workflows/capacity.yml`) runs the benchmark against a real Fuseki and a PostgreSQL decision log, and measures decision checks per second with 1, 4 and 16 agents asking at once, with and without an edit every second. On a GitHub runner (4 CPU, 16 GB, Fuseki with a 4 GB heap), after PR #44:

| Size | People | Checks a second (1 / 4 / 16 agents) | 16 agents with edits | One check | One edit | Nightly re-sync | Memory |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Medium | 20,080 | 235 / 355 / 357 | 287 a second, p99 187 ms | 4 ms | 111 ms | 6.4 s | 0.7 GB |
| Large | 100,200 | 178 / 275 / 213 | 230 a second, p99 258 ms | 5 ms | 130 ms | 26 s | 2.6 GB |
| Large, before PR #44 | 100,200 | 26 / 28 / 28 | 3 a second, p99 15.8 s | 50 ms | 78 ms | 31 s | 6.3 GB |

Runner timings vary by up to about 50% between runs. The Azure reference machines give steadier figures: `deploy/azure-capacity/README.md` sets them up and runs the same measurements there, up to 500,000 people.

On Azure (21 November 2026, one copy of the service, the PostgreSQL decision log on the same machine; the figures are in `tests/perf/targets.json` under `azure_*`):

| Machine, data on | People | Checks a second (1 / 4 / 16 agents) | 16 agents with edits | One check | One edit | Nightly re-sync | First load | Memory |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D4s v6, premium OS disk | 100,200 | 135 / 269 / 246 | 260 a second, p99 460 ms | 7 ms | 382 ms | 27 s | 15 min | 2.6 GB |
| D4ds v6, local NVMe drive | 100,200 | 161 / 325 / 374 | 314 a second, p99 196 ms | 9 ms | 68 ms | 22 s | 6 min | 2.6 GB |
| E16s v6, Premium SSD v2 | 501,000 | 213 / 361 / 412 | 331 a second, p99 308 ms | 4 ms | 236 ms | 106 s | 48 min | 12.1 GB |
| E16s v6, Premium SSD v2, two copies | 501,000 | 218 / 352 / 495; 544 with 32 agents | 425 a second (32 agents), p99 340 ms | 4 ms | 226 ms | 104 s | 46 min | 12.1 GB a copy |

Fuseki needs low-latency storage: an edit waits on Fuseki's writes, and on the premium OS disk it took five to six times as long as on the local drive (a 1 TB OS disk, with ten times the IOPS, made no difference, so it is latency, not throughput). The local drive is fastest but is lost when the machine is stopped or moved, so it suits only a copy that can be rebuilt; Premium SSD v2 keeps the data and needs an availability zone. At 500,000 people every goal set for Large is met except two: reading all people takes 6.1 s (0.6 s once cached) and the nightly re-sync 106 s. One copy answers up to 412 checks a second. Two copies on the same machine answer 544 with 32 agents asking at once (each copy follows the other's changes from records in the store instead of reading the model again; docs/architecture.md, "Performance"), so the Extra large tier's 500 holds while the model is not being edited. With an edit every second attempted they answer 425: each edit takes about 2.6 s under that load and both copies slow alike, so the cost is the edit, not the following. The sizing proposal states the guarantee that way (issue #46, decided 22 November). `bench.py --copies 2` (and the `copies` setting of both capacity workflows) spreads the agents over two copies of the service on one Fuseki and PostgreSQL; the `perf-fuseki` suite runs it at the smoke size, with edits, and fails if a copy stops answering.

The longest pause the service's garbage collector made in these runs was 1.5 s (once), where it was 16 to 22 s.

With `diagnose` set (seconds), the job also profiles 300 checks, prints the stack of any check slower than that, and takes a py-spy dump every half second from outside the process; `tests/perf/gil_holds.py` reports any thread that held Python's interpreter lock for that long while others ran. Mode `grow` loads step after step until the machine is exhausted.

Two tests keep the fast paths honest: `test_validation.py` checks that validating only the affected nodes finds what pyshacl with RDFS inference finds on the whole model, and `test_incremental.py` runs the same changes through the incremental and the full path and compares every report, stored graph and claim. The service suite also passes with `VEROMESH_VALIDATION=full` and with `VEROMESH_INCREMENTAL=off`.

### Replication under load

Gate 0's load test: the knowledge graph replicated through Kafka (one Fuseki writer, two read replicas, configured like the Helm chart; `tests/perf/replicated_fuseki.sh`), measured by `tests/perf/replication.py` after a bench load. Set the capacity workflows' `fuseki` input to `replicated`. Figures are in `tests/perf/targets.json` (`replication_ci_small`, `azure_xl_replicated`).

| Measure | 20,000 people (free CI runner) | 500,000 people (Azure E16s v6, Premium SSD v2) |
| --- | --- | --- |
| Replicas after the load | At the writer's version at once; 636,030 triples on each | At the writer's version within 0.2 s; 15,438,990 triples on each |
| Edits, one a second | p50 106 ms, max 286 ms | p50 180 ms, max 265 ms; the first edit after a restart 260 ms |
| Replica lag | p50 505 ms, p95 526 ms | p50 521 ms, p95 559 ms, max 565 ms |
| Writer stopped for 14 s | 341 checks a second from the replicas, none failed; an edit refused; writer back in 5.6 s | 379 checks a second from the replicas, none failed; an edit refused; writer back in 3.9 s; replicas at its version at once |
| A new replica from an empty volume | Matches the writer in 34 s | Matches the writer in 5.7 minutes |
| Planner state as the service starts | | 4.3 minutes, in the background (`/health`: `warming`, then `ready`) |

Two fixes came from this test: the Fuseki-Kafka connector held each change until a poll came back empty at one edit a second (10 s late on every edit), so the chart sets `fuseki.kafka.low.volume.threshold` to 0; and the first edit after a restart rewrote the whole effective model because Fuseki spells decimals differently, 25 s at 20,000 people (PR #63).

## Contract snapshots

The `contracts` suite keeps a snapshot of the interfaces other parts depend on, in `tests/contracts/golden/`:

| Snapshot | What it pins down |
| --- | --- |
| `api-schema.json` | entity types, fields, kinds and reference targets (`GET /api/v1/schema`) |
| `api-routes.json` | every HTTP route, its parameters, request body shape and status codes |
| `ontology-inventory.json` | classes, properties, domains and ranges; every validation rule with its path, counts and severity |
| `example-procure-to-pay.json` | validation result, entity counts and fact count of the worked example |
| `connectors-cli.json` | `veromesh-connect` commands and options |

A change to any of these fails the run with a diff. When the change is intended, accept it and commit the updated snapshot with the code, so the change is visible in review:

```bash
scripts/test.sh contracts --update-golden
git diff tests/contracts/golden
```

## Results

Each run writes `.build/test-results/<timestamp>/` (also reachable as `.build/test-results/latest`):

- `summary.md` and `summary.json`: one row per suite;
- `<suite>.log`: everything the suite printed;
- `<suite>.xml` and `harness.xml`: JUnit reports for CI dashboards;
- `python-licenses.md`: the dependency license report.

The exit code is 0 when everything passed, 1 when a suite failed and 2 when the harness could not run. Skipped suites (for example `compose` when Docker is not running) are listed under the summary. With `--strict`, as CI uses it, a skipped suite fails the run.

Other options: `-v` streams each suite's output, `-x` stops at the first failure, `--keep` leaves the throwaway Fuseki running for inspection (`localhost:3039`), `--timeout` limits each step.

## Adding a component

1. Put its tests in `<component>/tests/`.
2. Register a suite in `tests/harness/suites.py`: a name, the component, a tier, the commands, and `covers=("<component>/tests",)`.
3. If the component exposes an interface others depend on, add a snapshot test to `tests/contracts/` and create it with `--update-golden`.
4. If it needs a running server, add it to the integration or e2e tier, and to `tests/e2e/` for behaviour across the stack.

The harness refuses to run while any `*/tests` folder is not covered by a suite, so a component's tests cannot silently drop out of the regression run.
