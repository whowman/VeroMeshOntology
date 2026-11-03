#!/usr/bin/env bash
# Stop the local stack. Pass --volumes to also delete data.
set -euo pipefail

# shellcheck source=_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
load_datahub_secrets

EXTRA=()
if [[ "${1:-}" == "--volumes" ]]; then EXTRA=(-v); fi

docker compose -f "$ROOT/deploy/docker-compose.veromesh.yml" down ${EXTRA[@]+"${EXTRA[@]}"}
if [[ -d "$PROFILES_COMPOSE_DIR" ]]; then
  (cd "$PROFILES_COMPOSE_DIR" && docker compose --profile "$DATAHUB_PROFILE" down ${EXTRA[@]+"${EXTRA[@]}"})
fi
