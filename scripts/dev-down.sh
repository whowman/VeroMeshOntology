#!/usr/bin/env bash
# Stop the local stack. Pass --volumes to also delete data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../deploy/versions.env
source "$ROOT/deploy/versions.env"
export DATAHUB_VERSION OXIGRAPH_VERSION
PROFILES_DIR="$ROOT/.build/datahub-profiles-$DATAHUB_VERSION"

EXTRA=()
[[ "${1:-}" == "--volumes" ]] && EXTRA=(-v)

docker compose -f "$ROOT/deploy/docker-compose.veromesh.yml" down "${EXTRA[@]}"
if [[ -d "$PROFILES_DIR/docker/profiles" ]]; then
  (cd "$PROFILES_DIR/docker/profiles" && docker compose --profile "$DATAHUB_PROFILE" down "${EXTRA[@]}")
fi
