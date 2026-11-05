#!/usr/bin/env bash
# The model survives a Fuseki restart: same number of facts before and after,
# and the Veromesh API answers again afterwards. Needs the running stack.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FUSEKI="${VEROMESH_FUSEKI:-http://localhost:3030}"
API="${VEROMESH_API:-http://localhost:8700}"
Q='SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }'

count() {
  curl -fsS "$FUSEKI/veromesh/query" -H 'Accept: application/sparql-results+json' --data-urlencode "query=$Q" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["results"]["bindings"][0]["n"]["value"])'
}

BEFORE=$(count)
echo "facts before restart: $BEFORE"
if [[ "$BEFORE" -lt 1 ]]; then
  echo "the store is empty; run scripts/load-examples.sh (or the e2e suite) first" >&2
  exit 1
fi
docker compose -f "$ROOT/deploy/docker-compose.veromesh.yml" restart fuseki
for _ in $(seq 1 60); do curl -fs -o /dev/null "$FUSEKI/\$/ping" && break; sleep 2; done
AFTER=$(count)
echo "facts after restart:  $AFTER"
[[ "$AFTER" == "$BEFORE" ]]
for _ in $(seq 1 30); do curl -fs "$API/api/v1/sources" > /dev/null && break; sleep 2; done
curl -fsS "$API/api/v1/sources" > /dev/null
echo "Veromesh API answers after the restart."
