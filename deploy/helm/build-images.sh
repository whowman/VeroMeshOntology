#!/usr/bin/env bash
# Build every image the Kubernetes install needs, and optionally push them.
#
#   deploy/helm/build-images.sh                      # build, tagged veromesh/<name>:<version>
#   REGISTRY=registry.example.com deploy/helm/build-images.sh --push
#   deploy/helm/build-images.sh --kind veromesh      # load into a kind cluster instead
#
# Images
#   veromesh/service                  the VeroMesh service          (chart: api.image)
#   veromesh/fuseki                   Apache Jena Fuseki, writer and replicas (chart: fuseki.image)
#   veromesh/connectors               connector CLI, for jobs       (run with kubectl)
#   veromesh/datahub-frontend-react   branded DataHub UI            (datahub-values.yaml)
#   veromesh/datahub-model-plugin     custom DataHub aspect         (datahub-values.yaml)
#
# SKIP_DATAHUB=1 builds only the first three (no JDK/Gradle build needed).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=../versions.env
source "$ROOT/deploy/versions.env"

ACTION="" KIND_CLUSTER=""
case "${1:-}" in
  --push) ACTION=push ;;
  --kind) ACTION=kind; KIND_CLUSTER="${2:?usage: --kind <cluster>}" ;;
  "") ;;
  *) echo "usage: $0 [--push | --kind <cluster>]" >&2; exit 2 ;;
esac

SERVICE_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/service/pyproject.toml")"
CONNECTORS_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/connectors/pyproject.toml")"
TAG="${TAG:-}"   # override every tag, e.g. TAG=test

built=()
build() {  # build <name> <version> <docker build args...>
  local name="$1" version="${TAG:-$2}"; shift 2
  local image="veromesh/$name:$version"
  echo ">> Building $image"
  docker build -q -t "$image" "$@" >/dev/null
  built+=("$image")
}

build service "$SERVICE_VERSION" -f "$ROOT/service/Dockerfile" "$ROOT"
build fuseki "$JENA_VERSION" --build-arg JENA_VERSION="$JENA_VERSION" \
  --build-arg FUSEKI_KAFKA_VERSION="$FUSEKI_KAFKA_VERSION" "$ROOT/deploy/fuseki"
build connectors "$CONNECTORS_VERSION" "$ROOT/connectors"

if [[ -z "${SKIP_DATAHUB:-}" ]]; then
  "$ROOT/branding/datahub/build.sh"
  built+=("veromesh/datahub-frontend-react:$DATAHUB_VERSION")
  "$ROOT/datahub-model/build.sh"
  "$ROOT/datahub-model/build_image.sh"
  built+=("veromesh/datahub-model-plugin:$SERVICE_VERSION")
fi

for image in "${built[@]}"; do
  case "$ACTION" in
    push)
      target="${REGISTRY:?set REGISTRY to push}/$image"
      docker tag "$image" "$target" && docker push -q "$target" ;;
    kind)
      kind load docker-image "$image" --name "$KIND_CLUSTER" >/dev/null && echo ">> Loaded $image into kind/$KIND_CLUSTER" ;;
  esac
done
printf '%s\n' "${built[@]}"
