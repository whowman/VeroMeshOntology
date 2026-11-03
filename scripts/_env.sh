# Shared setup for dev-up.sh / dev-down.sh. Source it; don't run it.
# Works with macOS bash 3.2.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../deploy/versions.env
source "$ROOT/deploy/versions.env"
export DATAHUB_VERSION OXIGRAPH_VERSION

PROFILES_DIR="$ROOT/.build/datahub-profiles-$DATAHUB_VERSION"
PROFILES_COMPOSE_DIR="$PROFILES_DIR/docker/profiles"

# DataHub's compose profiles symlink into sibling folders under docker/ (env
# files, init scripts), so check out the whole docker/ directory of the pinned
# release, unmodified.
fetch_datahub_profiles() {
  if [[ ! -d "$PROFILES_DIR/.git" ]]; then
    echo ">> Fetching DataHub $DATAHUB_VERSION docker configuration"
    git clone -q --depth 1 --branch "$DATAHUB_VERSION" --filter=blob:none --sparse \
      https://github.com/datahub-project/datahub.git "$PROFILES_DIR"
  fi
  git -C "$PROFILES_DIR" sparse-checkout set docker
}

# DataHub's own launcher generates these token-service secrets and persists
# them; do the same so tokens stay valid across restarts.
load_datahub_secrets() {
  local secrets="$ROOT/.build/local-secrets.env"
  if [[ ! -f "$secrets" ]]; then
    mkdir -p "$ROOT/.build"
    umask 077
    {
      echo "# Auto-generated local dev secrets - do not commit"
      echo "DATAHUB_TOKEN_SERVICE_SIGNING_KEY=$(openssl rand -base64 32)"
      echo "DATAHUB_TOKEN_SERVICE_SALT=$(openssl rand -base64 32)"
    } > "$secrets"
  fi
  set -a
  # shellcheck disable=SC1090
  source "$secrets"
  set +a
}
