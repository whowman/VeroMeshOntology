#!/usr/bin/env bash
# Start a local stack: DataHub (Postgres + OpenSearch profile) + Oxigraph + Veromesh service.
#
#   scripts/dev-up.sh            # start everything
#   SKIP_MODEL=1 scripts/dev-up.sh   # skip building the custom DataHub model
#
# Requires Docker Compose >= 2.20 and ~8 GB RAM for Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../deploy/versions.env
source "$ROOT/deploy/versions.env"
export DATAHUB_VERSION OXIGRAPH_VERSION

PROFILES_DIR="$ROOT/.build/datahub-profiles-$DATAHUB_VERSION"

# 1. Fetch DataHub's official compose profiles for the pinned release (unmodified).
if [[ ! -d "$PROFILES_DIR/docker/profiles" ]]; then
  echo ">> Fetching DataHub $DATAHUB_VERSION compose profiles"
  git clone --depth 1 --branch "$DATAHUB_VERSION" --filter=blob:none --sparse \
    https://github.com/datahub-project/datahub.git "$PROFILES_DIR"
  git -C "$PROFILES_DIR" sparse-checkout set docker/profiles
fi

# 2. Build + deploy the Veromesh custom aspect into ~/.datahub/plugins (mounted into GMS).
if [[ -z "${SKIP_MODEL:-}" ]]; then
  "$ROOT/datahub-model/build.sh" --deploy
fi

# 3. Start DataHub.
echo ">> Starting DataHub ($DATAHUB_PROFILE)"
(cd "$PROFILES_DIR/docker/profiles" && docker compose --profile "$DATAHUB_PROFILE" up -d --wait)

# 4. Start Veromesh services on DataHub's network.
echo ">> Starting Veromesh services"
docker compose -f "$ROOT/deploy/docker-compose.veromesh.yml" up -d --build

cat <<EOF

DataHub UI:        http://localhost:9002   (datahub / datahub)
DataHub GMS:       http://localhost:8080
Oxigraph SPARQL:   http://localhost:${VEROMESH_OXIGRAPH_PORT:-7878}
Veromesh API:      http://localhost:${VEROMESH_SERVICE_PORT:-8700}/docs
EOF
