#!/usr/bin/env bash
# Check the branded frontend image: our assets are in the jar the frontend
# serves /assets/* from, and the theme JSON in it is complete.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/deploy/versions.env"
IMAGE="veromesh/datahub-frontend-react:$DATAHUB_VERSION"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"; docker rm -f vm-frontend-check >/dev/null 2>&1 || true' EXIT
docker create --name vm-frontend-check "$IMAGE" >/dev/null
docker cp -q vm-frontend-check:/datahub-frontend/lib "$OUT/lib"
python3 - "$OUT/lib" "$ROOT/branding/logos" <<'PY'
import json, sys, zipfile
from pathlib import Path
lib, logos = Path(sys.argv[1]), Path(sys.argv[2])
jars = [j for j in lib.glob("*.jar") if any(n.startswith("public/assets/veromesh/") for n in zipfile.ZipFile(j).namelist())]
assert len(jars) == 1, f"VeroMesh assets found in {len(jars)} jars"
z = zipfile.ZipFile(jars[0])
theme = json.loads(z.read("public/assets/conf/theme/veromesh.theme.json"))
assert theme["id"] == "veromesh.theme.json" and len(theme["colors"]) > 100, "theme incomplete"
for f in ("veromesh-mark.svg", "veromesh-stacked.svg", "favicon.ico"):
    assert z.read(f"public/assets/veromesh/{f}") == (logos / f).read_bytes(), f
assert any(n.startswith("public/assets/") and "veromesh" not in n for n in z.namelist()), "DataHub's own assets missing"
print(f"{jars[0].name}: theme with {len(theme['colors'])} colour tokens and VeroMesh logos present")
PY
