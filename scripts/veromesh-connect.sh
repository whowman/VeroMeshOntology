#!/usr/bin/env bash
# Run VeroMesh connectors in Docker against the running stack.
# Files are read from and written to the current folder.
#
#   scripts/veromesh-connect.sh spreadsheet template -o model.xlsx [--example]
#   scripts/veromesh-connect.sh spreadsheet import model.xlsx --source p2p [--mode merge] [--dry-run]
#   scripts/veromesh-connect.sh spreadsheet export --source p2p -o p2p.xlsx
#   scripts/veromesh-connect.sh csv import exports/ --source cmdb
#   scripts/veromesh-connect.sh csv import workers.csv --source hr --mapping workday.yaml
#   scripts/veromesh-connect.sh sources | runs | cleanup
#
# With Python 3.11+ you can also install it directly: pip install ./connectors
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec docker compose -f "$ROOT/deploy/docker-compose.veromesh.yml" --profile tools run --rm --quiet-pull \
  --build -v "$PWD:/work" -u "$(id -u):$(id -g)" connectors "$@"
