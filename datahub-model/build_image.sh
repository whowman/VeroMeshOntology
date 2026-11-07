#!/usr/bin/env bash
# Package the built model plugin as an image for Kubernetes:
#   veromesh/datahub-model-plugin:<version>
# Run datahub-model/build.sh first (it writes datahub-model/dist/*.zip).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

MODEL_VERSION="${MODEL_VERSION:-$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/service/pyproject.toml")}"
REGISTRY_ID="$(sed -n 's/^id: *//p' "$HERE/registry/entity-registry.yaml")"
ZIP="$HERE/dist/$REGISTRY_ID-$MODEL_VERSION.zip"
IMAGE="${IMAGE:-veromesh/datahub-model-plugin:$MODEL_VERSION}"
[[ -f "$ZIP" ]] || { echo "!! $ZIP not found: run datahub-model/build.sh first" >&2; exit 1; }

CTX="$ROOT/.build/plugin-image"
rm -rf "$CTX" && mkdir -p "$CTX/plugin" "$CTX/licenses"
unzip -q "$ZIP" -d "$CTX/plugin"
cp "$ROOT/LICENSE" "$ROOT/NOTICE" "$CTX/licenses/"
cp "$HERE/plugin.Dockerfile" "$CTX/Dockerfile"

docker build -q --build-arg REGISTRY_ID="$REGISTRY_ID" --build-arg MODEL_VERSION="$MODEL_VERSION" \
  -t "$IMAGE" "$CTX" >/dev/null
docker run --rm --entrypoint ls "$IMAGE" "/plugins/models/$REGISTRY_ID/$MODEL_VERSION" >/dev/null
echo ">> Built $IMAGE"
