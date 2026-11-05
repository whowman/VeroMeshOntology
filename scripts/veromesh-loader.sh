#!/usr/bin/env bash
# Run the VeroMesh spreadsheet loader in Docker against the running stack.
# Files are read from and written to the current folder.
#
#   scripts/veromesh-loader.sh template -o model.xlsx            # blank workbook
#   scripts/veromesh-loader.sh template --example -o example.xlsx
#   scripts/veromesh-loader.sh import example.xlsx --source p2p  # validate + commit
#   scripts/veromesh-loader.sh import model.xlsx --dry-run       # validate only
#   scripts/veromesh-loader.sh export --source p2p -o p2p.xlsx
#   scripts/veromesh-loader.sh sources
#
# With Python 3.11+ you can also install it directly: pip install ./loader
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec docker compose -f "$ROOT/deploy/docker-compose.veromesh.yml" --profile tools run --rm --quiet-pull \
  --build -v "$PWD:/work" -u "$(id -u):$(id -g)" loader "$@"
