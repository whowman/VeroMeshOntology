#!/usr/bin/env bash
# VeroMesh regression test harness: one command for every component's tests.
#
#   scripts/test.sh                     quick tier: lint, unit, contract snapshots (default)
#   scripts/test.sh integration         + real Apache Jena Fuseki and the container images
#   scripts/test.sh build               + the DataHub custom-model build (slow)
#   scripts/test.sh e2e                 end-to-end against the running stack (scripts/dev-up.sh)
#   scripts/test.sh all --start-stack   everything; starts the stack if it is not running
#   scripts/test.sh connectors          one suite or component
#   scripts/test.sh --list              tiers and suites
#   scripts/test.sh --help              all options
#
# Needs Docker (tests run in a test-runner image) and any python3 to drive the
# run; with --local, tests run in a virtualenv instead (needs Python 3.11+).
# Results go to .build/test-results/<timestamp>/ (and .build/test-results/latest).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3 || command -v python || true)"
if [[ -z "$PY" ]]; then
  echo "python3 is required to run the test harness (any version 3.9 or newer)." >&2
  exit 2
fi
exec "$PY" "$ROOT/tests/harness/run.py" "$@"
