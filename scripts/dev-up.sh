#!/usr/bin/env bash
# Start a local stack: DataHub (Postgres + OpenSearch profile) + Oxigraph + Veromesh service.
#
#   scripts/dev-up.sh            # start everything
#   SKIP_MODEL=1 scripts/dev-up.sh   # skip building the custom DataHub model
#
# Requires Docker Compose >= 2.20 and ~8 GB RAM for Docker.
set -euo pipefail

# shellcheck source=_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

# 1. Fetch DataHub's official docker configuration for the pinned release.
fetch_datahub_profiles
load_datahub_secrets

# 2. Build + deploy the Veromesh custom aspect into ~/.datahub/plugins (mounted into GMS).
if [[ -z "${SKIP_MODEL:-}" ]]; then
  "$ROOT/datahub-model/build.sh" --deploy
fi

# 3. Start DataHub.
echo ">> Starting DataHub ($DATAHUB_PROFILE)"
(cd "$PROFILES_COMPOSE_DIR" && docker compose --profile "$DATAHUB_PROFILE" up -d)

# wait_for <name> <url> <seconds>
wait_for() {
  local name="$1" url="$2" limit="$3" waited=0
  printf ">> Waiting for %s" "$name"
  until curl -fs -o /dev/null "$url"; do
    if (( waited >= limit )); then
      echo; echo "!! $name not healthy after ${limit}s. Check: docker ps -a ; docker logs <container>" >&2
      exit 1
    fi
    printf "."; sleep 5; waited=$((waited + 5))
  done
  echo " up"
}

# First start pulls images and runs DataHub's one-time setup; allow 15 minutes.
wait_for "DataHub GMS" "http://localhost:${DATAHUB_MAPPED_GMS_PORT:-8080}/health" 900
wait_for "DataHub UI" "http://localhost:9002/admin" 300

# 4. DataHub requires a token for API calls. Create a personal access token for
#    the local dev user and reuse it while it stays valid (kept in .build/, not committed).
GMS="http://localhost:${DATAHUB_MAPPED_GMS_PORT:-8080}"
TOKEN_FILE="$ROOT/.build/datahub-token"

token_valid() {
  [[ -s "$TOKEN_FILE" ]] && curl -fs -o /dev/null -X POST "$GMS/api/graphql" \
    -H "Authorization: Bearer $(cat "$TOKEN_FILE")" -H 'Content-Type: application/json' \
    -d '{"query":"{ me { corpUser { urn } } }"}'
}

create_token() {
  local cookies response
  cookies="$(mktemp)"
  curl -fsS -c "$cookies" -o /dev/null -X POST http://localhost:9002/logIn \
    -H 'Content-Type: application/json' -d '{"username":"datahub","password":"datahub"}' || return 1
  response="$(curl -fsS -b "$cookies" -X POST http://localhost:9002/api/v2/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"mutation { createAccessToken(input: {type: PERSONAL, actorUrn: \"urn:li:corpuser:datahub\", duration: ONE_MONTH, name: \"veromesh-dev\"}) { accessToken } }"}')"
  rm -f "$cookies"
  TOKEN="$(printf '%s' "$response" | sed -n 's/.*"accessToken":"\([^"]*\)".*/\1/p')"
  LAST_RESPONSE="$response"
  [[ -n "$TOKEN" ]]
}

if [[ -z "${VEROMESH_DATAHUB_TOKEN:-}" ]]; then
  if ! token_valid; then
    echo ">> Creating DataHub access token for local dev user 'datahub'"
    # DataHub loads its default access policies into a cache that refreshes on a
    # timer, so right after startup token creation can be refused for a while.
    attempts=0
    until create_token; do
      attempts=$((attempts + 1))
      if (( attempts >= 36 )); then
        echo "!! Could not create a DataHub token. Last response: ${LAST_RESPONSE:-none}" >&2
        exit 1
      fi
      sleep 5
    done
    (umask 077 && printf '%s' "$TOKEN" > "$TOKEN_FILE")
  fi
  VEROMESH_DATAHUB_TOKEN="$(cat "$TOKEN_FILE")"
fi
export VEROMESH_DATAHUB_TOKEN

# 5. Start Veromesh services on DataHub's network.
echo ">> Starting Veromesh services"
docker compose -f "$ROOT/deploy/docker-compose.veromesh.yml" up -d --build
wait_for "Veromesh API" "http://localhost:${VEROMESH_SERVICE_PORT:-8700}/health" 120

cat <<EOF

DataHub UI:        http://localhost:9002   (datahub / datahub)
DataHub GMS:       http://localhost:8080
Oxigraph SPARQL:   http://localhost:${VEROMESH_OXIGRAPH_PORT:-7878}
Veromesh API:      http://localhost:${VEROMESH_SERVICE_PORT:-8700}/docs
EOF
