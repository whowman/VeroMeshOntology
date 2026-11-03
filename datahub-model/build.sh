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
# Requires: git, and either JDK 25 or Docker (DataHub v1.8.0 targets Java 25;
# without a local JDK 25 the build runs in an eclipse-temurin:25-jdk container).
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

GRADLE_ARGS=(--no-daemon "-PprojVersion=$MODEL_VERSION" :metadata-models-custom:build -x test)

java_major() { java -version 2>&1 | sed -n 's/.*version "\([0-9]*\).*/\1/p' | head -1; }

echo ">> Building $REGISTRY_ID $MODEL_VERSION"
if [[ -z "${BUILD_IN_DOCKER:-}" ]] && command -v java >/dev/null && [[ "$(java_major)" -ge 25 ]]; then
  (cd "$WORK" && ./gradlew "${GRADLE_ARGS[@]}")
else
  # No local JDK 25: build inside a JDK 25 container. The Gradle cache lives in a
  # named volume so rebuilds are fast; files are handed back to the host user.
  echo ">> Using Docker (eclipse-temurin:25-jdk) for the build"
  docker run --rm \
    -v "$WORK":/work -w /work \
    -v veromesh-gradle-cache:/root/.gradle \
    eclipse-temurin:25-jdk \
    bash -c "(command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git >/dev/null)) \
      && git config --global --add safe.directory /work \
      && ./gradlew ${GRADLE_ARGS[*]}; rc=\$?; chown -R $(id -u):$(id -g) /work; exit \$rc"
fi

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
