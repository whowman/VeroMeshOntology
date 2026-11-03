#!/usr/bin/env bash
# Build the Veromesh custom DataHub metadata model plugin.
#
# DataHub custom models must be compiled inside DataHub's Gradle build, so this
# script checks out DataHub at the pinned version (read-only, unmodified),
# drops our PDL + registry into its metadata-models-custom module, and builds.
# Nothing from DataHub is committed to this repo.
#
# Usage:
#   datahub-model/build.sh            # build zip into datahub-model/dist/
#   datahub-model/build.sh --deploy   # also unpack into ~/.datahub/plugins/models/
#
# Requires: git, JDK 17.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=../deploy/versions.env
source "$ROOT/deploy/versions.env"

MODEL_VERSION="${MODEL_VERSION:-$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/service/pyproject.toml")}"
WORK="${DATAHUB_BUILD_DIR:-$ROOT/.build/datahub-$DATAHUB_VERSION}"
REGISTRY_ID="$(sed -n 's/^id: *//p' "$HERE/registry/entity-registry.yaml")"

if [[ ! -d "$WORK/.git" ]]; then
  echo ">> Checking out DataHub $DATAHUB_VERSION into $WORK"
  git clone --depth 1 --branch "$DATAHUB_VERSION" https://github.com/datahub-project/datahub.git "$WORK"
fi

MODULE="$WORK/metadata-models-custom"
echo ">> Installing Veromesh model into $MODULE"
rm -rf "$MODULE/src" "$MODULE/registry"
mkdir -p "$MODULE/src/main"
cp -R "$HERE/src/main/pegasus" "$MODULE/src/main/pegasus"
cp -R "$HERE/registry" "$MODULE/registry"

echo ">> Building $REGISTRY_ID $MODEL_VERSION"
(cd "$WORK" && ./gradlew --no-daemon -PprojVersion="$MODEL_VERSION" :metadata-models-custom:build -x test)

mkdir -p "$HERE/dist"
ZIP="$MODULE/build/dist/metadata-models-custom-$MODEL_VERSION.zip"
cp "$ZIP" "$HERE/dist/$REGISTRY_ID-$MODEL_VERSION.zip"
echo ">> Built $HERE/dist/$REGISTRY_ID-$MODEL_VERSION.zip"

if [[ "${1:-}" == "--deploy" ]]; then
  TARGET="$HOME/.datahub/plugins/models/$REGISTRY_ID/$MODEL_VERSION"
  rm -rf "$TARGET" && mkdir -p "$TARGET"
  unzip -q "$HERE/dist/$REGISTRY_ID-$MODEL_VERSION.zip" -d "$TARGET"
  echo ">> Deployed to $TARGET (restart datahub-gms to load it)"
fi
