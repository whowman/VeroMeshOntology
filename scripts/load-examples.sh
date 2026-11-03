#!/usr/bin/env bash
# Load the core vocabulary and the example retail ontology into a running
# Veromesh service, then publish the retail classes to the DataHub glossary.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${VEROMESH_API:-http://localhost:8700}"

put() {
  curl -fsS -X PUT "$API/ontologies?graph=$1" -H 'Content-Type: text/turtle' --data-binary "@$2"
  echo "loaded $1"
}

put "https://veromesh.ai/ontology/core" "$ROOT/ontology/veromesh-core.ttl"
put "https://example.com/ontology/retail" "$ROOT/ontology/examples/retail.ttl"

curl -fsS -X POST "$API/glossary/publish" -H 'Content-Type: application/json' \
  -d '{"graph": "https://example.com/ontology/retail", "name": "Retail (example)"}'
echo
