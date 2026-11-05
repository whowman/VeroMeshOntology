# Testing

One command runs every component's tests and prints one summary:

```bash
scripts/test.sh                     # quick tier: lint, unit, contract snapshots (about 2 minutes)
scripts/test.sh integration         # + real Apache Jena Fuseki and the container images
scripts/test.sh build               # + the DataHub custom-model plugin build (slow)
scripts/test.sh e2e                 # end-to-end against the running stack
scripts/test.sh all --start-stack   # everything; starts the stack first if it is not running
```

You need Docker (Docker Desktop on a Mac) and any `python3`; the tests run inside a test-runner image built from the components' declared dependencies, so nothing else needs installing. The image is rebuilt only when a `pyproject.toml` changes. To run without Docker, add `--local`: the tests then run in a virtualenv under `.build/test-venv` (needs Python 3.11+; your conda environment works).

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
| quick | `licenses` | dependencies | no GPL / AGPL / SSPL / Elastic License package; report in the results folder |
| quick | `scripts`, `compose` | `scripts/`, `deploy/` | shell scripts parse; compose file is valid |
| integration | `fuseki` | `service/`, `connectors/` | the whole service and connector suites again, on a real Fuseki |
| integration | `images` | Dockerfiles | service and connector images build, start, and carry their license files |
| integration | `datahub-frontend` | `branding/datahub/` | the VeroMesh-branded DataHub frontend image builds with the theme and logos in place |
| build | `datahub-model` | `datahub-model/` | the custom aspect compiles inside DataHub's Gradle build |
| e2e | `e2e` | the running stack | health and plugin loaded; DataHub serves the VeroMesh theme, logos and favicon; ontologies load and publish to the DataHub glossary; bindings reach Fuseki and DataHub and are removed again; the worked example loads through the spreadsheet connector, re-runs as a no-op, exports and re-imports, and a breaking delete is refused |
| e2e | `stack-scripts` | developer scripts | `load-examples.sh` and `veromesh-connect.sh` work against the stack |
| e2e | `persistence` | Fuseki volume | the model survives a Fuseki restart |

`scripts/test.sh --list` prints the same table from the registry. You can run one suite or one component by name: `scripts/test.sh connectors`, `scripts/test.sh fuseki`, `scripts/test.sh stack`.

The e2e suites work against your development stack. Everything they create is named `regtest`, and they remove it afterwards, so your own sources are left alone.

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
