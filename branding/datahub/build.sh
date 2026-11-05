#!/usr/bin/env bash
# Build the VeroMesh-branded DataHub frontend image:
#   veromesh/datahub-frontend-react:<DATAHUB_VERSION>
#
# 1. Generate the theme from DataHub's own light theme at the pinned version.
# 2. Layer the theme and logos onto the official frontend image.
#
# Needs git and Docker. DataHub's source is the shared shallow checkout in
# .build/ (also used by dev-up.sh and datahub-model/build.sh).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=../../scripts/_env.sh
source "$ROOT/scripts/_env.sh"
fetch_datahub_profiles

IMAGE="veromesh/datahub-frontend-react:$DATAHUB_VERSION"
CTX="$ROOT/.build/branding/frontend-context"
rm -rf "$CTX"
mkdir -p "$CTX/assets/conf/theme" "$CTX/assets/veromesh"

python3 "$HERE/make_theme.py" "$PROFILES_DIR" "$CTX/assets/conf/theme/veromesh.theme.json"
cp "$ROOT"/branding/logos/*.svg "$ROOT"/branding/logos/*.png "$ROOT/branding/logos/favicon.ico" "$CTX/assets/veromesh/"
cp "$HERE/add_assets.py" "$HERE/Dockerfile" "$CTX/"
chmod -R a+rX "$CTX"

echo ">> Building $IMAGE"
docker build --progress=plain --build-arg DATAHUB_VERSION="$DATAHUB_VERSION" -t "$IMAGE" "$CTX" \
  > "$ROOT/.build/branding/frontend-build.log" 2>&1 || { tail -40 "$ROOT/.build/branding/frontend-build.log"; exit 1; }
grep -E "assets jars:|\+ public/assets" "$ROOT/.build/branding/frontend-build.log" | sed 's/^#[0-9]* [0-9.]* /   /' || true
echo ">> Built $IMAGE"
