"""The regression suite registry: every component's tests, in one place.

Adding a component? Add a ``Suite`` here (and list its test folder in
``covers``). The harness refuses to run while any ``*/tests`` folder in the
repository is not covered by a suite, so nothing can silently drop out of the
regression run.

Fields
    name        unique; also what you type: ``scripts/test.sh service``
    component   repository folder (or area) it protects; ``scripts/test.sh connectors``
                runs every suite of that component
    tier        quick | integration | build | e2e  (see TIERS)
    steps       shell commands, run from the repository root. Placeholders:
                  {results}    absolute path of this run's results folder
                  {fuseki}     dataset URL of the throwaway Fuseki (needs="fuseki")
                  {api} {gms} {fuseki_stack} {token_file}   the running stack (needs="stack")
    where       runner  - inside the test-runner (Docker image, or the --local venv)
                host    - on your machine (docker, git, bash)
    needs       extra things the suite requires: docker, fuseki, stack, jdk25-or-docker
    env         extra environment for the steps
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Tier order matters: asking for a tier runs it and every tier before it,
# except e2e, which only runs when asked for (it needs the running stack).
TIERS = ("quick", "integration", "build", "e2e")
TIER_HELP = {
    "quick": "lint, unit and contract tests: no servers needed (about 2 minutes)",
    "integration": "quick + real Apache Jena Fuseki and the container images",
    "build": "integration + the DataHub custom-model plugin build (slow, ~10 minutes)",
    "e2e": "end-to-end against the running local stack (scripts/dev-up.sh)",
    "all": "every suite, including e2e",
}

PYTEST = "python -m pytest -q -rs"


@dataclass(frozen=True)
class Suite:
    name: str
    component: str
    tier: str
    description: str
    steps: tuple[str, ...]
    where: str = "runner"
    needs: frozenset[str] = frozenset()
    env: dict[str, str] = field(default_factory=dict)
    covers: tuple[str, ...] = ()


SUITES: list[Suite] = [
    # ------------------------------------------------------------------ quick
    Suite(
        name="lint",
        component="repo",
        tier="quick",
        description="ruff lint + format check on all Python code",
        steps=(
            "cd service && ruff check . && ruff format --check .",
            "cd connectors && ruff check . && ruff format --check .",
            "ruff check --config service/pyproject.toml tests branding && ruff format --config service/pyproject.toml --check tests branding",
        ),
    ),
    Suite(
        name="service",
        component="service",
        tier="quick",
        description="Veromesh API: REST model API, changesets, bindings, glossary (in-memory store)",
        steps=(f"cd service && {PYTEST} --junitxml={{results}}/service.xml",),
        covers=("service/tests",),
    ),
    Suite(
        name="connectors",
        component="connectors",
        tier="quick",
        description="Connector SDK, spreadsheet and CSV connectors, against the real API in-process",
        steps=(f"cd connectors && {PYTEST} --junitxml={{results}}/connectors.xml",),
        covers=("connectors/tests",),
    ),
    Suite(
        name="ontology",
        component="ontology",
        tier="quick",
        description="Meta-model validation rules (SHACL) and every Turtle file parses",
        steps=(
            f"{PYTEST} ontology/tests --junitxml={{results}}/ontology.xml",
            "python tests/harness/check_turtle.py ontology",
        ),
        covers=("ontology/tests",),
    ),
    Suite(
        name="branding",
        component="branding",
        tier="quick",
        description="DataHub theme generator (palette mapping, contrast) and asset injector",
        steps=(f"{PYTEST} branding/tests --junitxml={{results}}/branding.xml",),
        covers=("branding/tests",),
    ),
    Suite(
        name="contracts",
        component="repo",
        tier="quick",
        description="Golden snapshots: API schema, routes, ontology inventory, example report, CLI",
        steps=(f"{PYTEST} tests/contracts --junitxml={{results}}/contracts.xml",),
        covers=("tests/contracts",),
    ),
    Suite(
        name="licenses",
        component="repo",
        tier="quick",
        description="No GPL / AGPL / SSPL / ELv2 dependency in the Python components",
        steps=("python tests/harness/check_licenses.py {results}/python-licenses.md",),
    ),
    Suite(
        name="scripts",
        component="deploy",
        tier="quick",
        where="host",
        description="Shell scripts parse (bash -n)",
        steps=(
            'for f in scripts/*.sh datahub-model/build.sh tests/e2e/*.sh branding/datahub/*.sh; do bash -n "$f" || exit 1; done',
        ),
    ),
    Suite(
        name="compose",
        component="deploy",
        tier="quick",
        where="host",
        needs=frozenset({"docker"}),
        description="Compose file is valid",
        steps=("docker compose -f deploy/docker-compose.veromesh.yml config -q",),
    ),
    # ------------------------------------------------------------------ integration
    Suite(
        name="fuseki",
        component="service",
        tier="integration",
        needs=frozenset({"fuseki"}),
        description="Service and connector suites again, on a real Fuseki instead of the in-memory store",
        env={"VEROMESH_TEST_STORE": "fuseki:{fuseki}", "VEROMESH_TEST_FUSEKI_URL": "{fuseki}"},
        steps=(
            f"cd service && {PYTEST} --junitxml={{results}}/fuseki-service.xml",
            f"cd connectors && {PYTEST} --junitxml={{results}}/fuseki-connectors.xml",
        ),
    ),
    Suite(
        name="images",
        component="deploy",
        tier="integration",
        where="host",
        needs=frozenset({"docker"}),
        description="Service and connectors images build and start",
        steps=(
            "docker build -q -f service/Dockerfile -t veromesh/service:test . >/dev/null",
            "docker run --rm veromesh/service:test python -c 'import veromesh.api'",
            "docker run --rm veromesh/service:test ls /app/licenses/LICENSE /app/licenses/THIRD_PARTY_LICENSES.md",
            "docker build -q -t veromesh/connectors:test connectors >/dev/null",
            "docker run --rm veromesh/connectors:test --version",
        ),
    ),
    Suite(
        name="datahub-frontend",
        component="branding",
        tier="integration",
        where="host",
        needs=frozenset({"docker"}),
        description="VeroMesh-branded DataHub frontend image builds with the theme and logos in place",
        steps=("branding/datahub/build.sh", "branding/datahub/verify_image.sh"),
    ),
    # ------------------------------------------------------------------ build
    Suite(
        name="datahub-model",
        component="datahub-model",
        tier="build",
        where="host",
        needs=frozenset({"jdk25-or-docker"}),
        description="Custom DataHub aspect compiles inside DataHub's Gradle build (JDK 25 or Docker)",
        steps=("datahub-model/build.sh", "ls datahub-model/dist/*.zip"),
    ),
    # ------------------------------------------------------------------ e2e
    Suite(
        name="e2e",
        component="stack",
        tier="e2e",
        needs=frozenset({"stack"}),
        description="Running stack: health, ontology + glossary, bindings in Fuseki and DataHub, model load via connector",
        env={
            "VEROMESH_E2E_API": "{api}",
            "VEROMESH_E2E_GMS": "{gms}",
            "VEROMESH_E2E_FUSEKI": "{fuseki_stack}",
            "VEROMESH_E2E_TOKEN_FILE": "{token_file}",
            "VEROMESH_E2E_FRONTEND": "{frontend}",
        },
        steps=(f"{PYTEST} tests/e2e --junitxml={{results}}/e2e.xml",),
        covers=("tests/e2e",),
    ),
    Suite(
        name="stack-scripts",
        component="stack",
        tier="e2e",
        where="host",
        needs=frozenset({"stack", "docker"}),
        description="Developer scripts work against the stack: load-examples.sh, veromesh-connect.sh",
        steps=(
            "scripts/load-examples.sh",
            "scripts/veromesh-connect.sh sources",
            "scripts/veromesh-connect.sh runs --limit 5",
        ),
    ),
    Suite(
        name="persistence",
        component="stack",
        tier="e2e",
        where="host",
        needs=frozenset({"stack", "docker"}),
        description="Model data survives a Fuseki restart",
        steps=("tests/e2e/fuseki_restart.sh",),
    ),
]
