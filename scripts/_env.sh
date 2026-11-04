# Shared setup for dev-up.sh / dev-down.sh. Source it; don't run it.
# Works with macOS bash 3.2.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../deploy/versions.env
source "$ROOT/deploy/versions.env"
export DATAHUB_VERSION OXIGRAPH_VERSION

# One shallow checkout of the pinned DataHub release, shared with
# datahub-model/build.sh (which needs the full source tree anyway).
PROFILES_DIR="$ROOT/.build/datahub-$DATAHUB_VERSION"
PROFILES_COMPOSE_DIR="$PROFILES_DIR/docker/profiles"

# DataHub's compose profiles symlink into sibling folders under docker/, so the
# whole docker/ directory is needed. Plain --depth/--branch only: partial-clone
# and sparse options are missing from older git versions.
fetch_datahub_profiles() {
  if [[ ! -d "$PROFILES_COMPOSE_DIR" ]]; then
    echo ">> Fetching DataHub $DATAHUB_VERSION (shallow clone, one-time)"
    rm -rf "$PROFILES_DIR"
    git clone --depth 1 --branch "$DATAHUB_VERSION" \
      https://github.com/datahub-project/datahub.git "$PROFILES_DIR"
  fi
}

# DataHub's own launcher generates these token-service secrets and persists
# them; do the same so tokens stay valid across restarts.
load_datahub_secrets() {
  local secrets="$ROOT/.build/local-secrets.env"
  if [[ ! -f "$secrets" ]]; then
    mkdir -p "$ROOT/.build"
    # Subshell so the restrictive umask applies only to this file, not to
    # anything the calling script creates later (e.g. the plugin folders).
    (
      umask 077
      {
        echo "# Auto-generated local dev secrets - do not commit"
        echo "DATAHUB_TOKEN_SERVICE_SIGNING_KEY=$(openssl rand -base64 32)"
        echo "DATAHUB_TOKEN_SERVICE_SALT=$(openssl rand -base64 32)"
      } > "$secrets"
    )
  fi
  set -a
  # shellcheck disable=SC1090
  source "$secrets"
  set +a
}
