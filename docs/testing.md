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
| quick | `connectors` | `connectors/` | connector SDK, spreadsheet and CSV connectors, against the real API in-process |
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

Runner timings vary by up to about 50% between runs; the reference machines (`docs/architecture.md`, sizing) will give steadier figures. The longest pause the service's garbage collector made in these runs was 1.5 s (once), where it was 16 to 22 s.

With `diagnose` set (seconds), the job also profiles 300 checks, prints the stack of any check slower than that, and takes a py-spy dump every half second from outside the process; `tests/perf/gil_holds.py` reports any thread that held Python's interpreter lock for that long while others ran. Mode `grow` loads step after step until the machine is exhausted.

Two tests keep the fast paths honest: `test_validation.py` checks that validating only the affected nodes finds what pyshacl with RDFS inference finds on the whole model, and `test_incremental.py` runs the same changes through the incremental and the full path and compares every report, stored graph and claim. The service suite also passes with `VEROMESH_VALIDATION=full` and with `VEROMESH_INCREMENTAL=off`.

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
