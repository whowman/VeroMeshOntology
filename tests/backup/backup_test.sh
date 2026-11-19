#!/bin/sh
# shellcheck shell=busybox
# Tests for deploy/backup/veromesh-backup.sh, run inside the backup image against
# a real Fuseki and a real PostgreSQL (scripts/test.sh backup), or directly with
# age, jq, curl and the PostgreSQL client on PATH.
#
#   FUSEKI_URL         dataset URL of an empty, throwaway Fuseki
#   DECISION_LOG_URL   postgresql:// URL of a throwaway database
#   SCHEMA_SQL         the decision log schema (taken from service/src/veromesh/decisions.py)
#   BACKUP             the script to test (default: veromesh-backup on PATH)
set -eu
set -o pipefail

BACKUP="${BACKUP:-veromesh-backup}"
: "${FUSEKI_URL:?}" "${DECISION_LOG_URL:?}" "${SCHEMA_SQL:?}"
T="$(mktemp -d)"
export WORK_DIR="$T/work" BACKUP_TARGET="$T/backups" BACKUP_KEEP=2
export AGE_RECIPIENTS_FILE="$T/recipients" AGE_IDENTITY_FILE="$T/identity"
export BACKUP_RELEASE=test BACKUP_CHART=veromesh-test BACKUP_APP_VERSION=test
trap 'rm -rf "$T"' EXIT

pass=0
ok() { pass=$((pass + 1)); echo "ok $pass - $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
sql() { psql "$DECISION_LOG_URL" -X -q -t -A -v ON_ERROR_STOP=1 -c "$1"; }
quads() { curl -fsS -H 'Accept: application/n-quads' "$FUSEKI_URL/data" | wc -l | tr -d ' '; }
ask() {  # ask <SPARQL ASK body>: prints true or false
  curl -fsS -H 'Accept: application/sparql-results+json' --data-urlencode "query=ASK { $1 }" "$FUSEKI_URL/query" | jq -r .boolean
}
put_graph() { curl -fsS -X PUT -H 'Content-Type: application/n-quads' --data-binary @"$1" "$FUSEKI_URL/data" >/dev/null; }
# shellcheck disable=SC2012  # backup ids are plain timestamps
latest() { ls "$BACKUP_TARGET" | sort | tail -1; }
entries() { sql "SELECT count(*) FROM veromesh_decision_log"; }
add_decision() {  # add_decision <seq>
  sql "INSERT INTO veromesh_decision_log (seq, id, recorded_at, kind, entry, hash)
       VALUES ($1, gen_random_uuid(), now(), 'decision', '{\"n\": $1}', md5('h$1'))"
}

age-keygen -o "$AGE_IDENTITY_FILE" 2>/dev/null
age-keygen -y "$AGE_IDENTITY_FILE" >"$AGE_RECIPIENTS_FILE"

# A model with named graphs, the default graph, blank nodes, a language tag, a
# line break and non-ASCII text: everything N-Quads has to carry exactly.
cat >"$T/model.nq" <<'EOF'
<urn:x:alice> <urn:x:owns> <urn:x:invoices> <urn:g:hr> .
<urn:x:alice> <http://www.w3.org/2000/01/rdf-schema#label> "Alice Ångström"@sv <urn:g:hr> .
<urn:x:invoices> <urn:x:note> "first line\nsecond line" <urn:g:erp> .
_:b1 <urn:x:approves> <urn:x:invoices> <urn:g:erp> .
_:b1 <urn:x:limit> "50000"^^<http://www.w3.org/2001/XMLSchema#integer> <urn:g:erp> .
<urn:x:meta> <urn:x:version> "1" .
EOF
put_graph "$T/model.nq"
[ "$(quads)" = 6 ] || fail "the test model did not load ($(quads) quads)"

sql "DROP TABLE IF EXISTS veromesh_decision_log CASCADE" 2>/dev/null
PGOPTIONS=--client-min-messages=warning psql "$DECISION_LOG_URL" -X -q -v ON_ERROR_STOP=1 -f "$SCHEMA_SQL" >/dev/null
for i in 1 2 3; do add_decision "$i"; done

# ---------------------------------------------------------------- backup
"$BACKUP" backup >"$T/out" 2>&1 || { cat "$T/out"; fail "backup failed"; }
first="$(latest)"
m="$BACKUP_TARGET/$first/manifest.json"
[ -f "$m" ] || fail "no manifest in $first"
[ "$(jq -r .graph.quads "$m")" = 6 ] || fail "manifest quads: $(jq -c .graph "$m")"
[ "$(jq -r .decisionLog.entries "$m")" = 3 ] || fail "manifest entries: $(jq -c .decisionLog "$m")"
[ "$(jq -r .encrypted "$m")" = true ] || fail "not marked encrypted"
[ "$(jq -r '.recipients[0]' "$m")" = "$(cat "$AGE_RECIPIENTS_FILE")" ] || fail "recipient not recorded"
ok "a backup holds the graph and the decision log, with a manifest"

head -c 21 "$BACKUP_TARGET/$first/graph.nq.gz.age" | grep -q 'age-encryption.org/v1' || fail "graph file is not age-encrypted"
head -c 21 "$BACKUP_TARGET/$first/decision-log.pgdump.age" | grep -q 'age-encryption.org/v1' || fail "log file is not age-encrypted"
if gzip -dc "$BACKUP_TARGET/$first/graph.nq.gz.age" 2>/dev/null | grep -q Alice; then fail "graph readable without the key"; fi
ok "both files are encrypted to the customer's key"

# ---------------------------------------------------------------- retention
sleep 2
"$BACKUP" backup >/dev/null 2>&1 || fail "second backup failed"
sleep 2
mkdir -p "$BACKUP_TARGET/19990101T000000Z"   # an unfinished backup (no manifest)
"$BACKUP" backup >/dev/null 2>&1 || fail "third backup failed"
# shellcheck disable=SC2012
n="$(ls "$BACKUP_TARGET" | wc -l | tr -d ' ')"
[ "$n" = 2 ] || fail "BACKUP_KEEP=2 but $n backups are held: $(ls "$BACKUP_TARGET")"
[ ! -d "$BACKUP_TARGET/$first" ] || fail "the oldest backup was kept"
[ ! -d "$BACKUP_TARGET/19990101T000000Z" ] || fail "the unfinished backup was kept"
ok "retention keeps the newest BACKUP_KEEP backups and drops unfinished ones"

"$BACKUP" list >"$T/list" 2>&1 || fail "list failed"
[ "$(grep -c '^20' "$T/list")" = 2 ] || { cat "$T/list"; fail "list does not show two backups"; }
ok "list shows the backups"

id="$(latest)"

# ---------------------------------------------------------------- restore
# The model changes after the backup, and one more decision is recorded.
printf '<urn:x:bob> <urn:x:owns> <urn:x:payments> <urn:g:hr> .\n' >"$T/other.nq"
put_graph "$T/other.nq"
add_decision 4

"$BACKUP" restore "$id" >"$T/out" 2>&1 || { cat "$T/out"; fail "restore failed"; }
[ "$(quads)" = 6 ] || fail "after restore: $(quads) quads"
[ "$(ask 'GRAPH <urn:g:erp> { <urn:x:invoices> <urn:x:note> "first line\nsecond line" }')" = true ] || fail "the line break did not survive"
[ "$(ask 'GRAPH <urn:g:hr> { <urn:x:alice> ?p "Alice Ångström"@sv }')" = true ] || fail "the non-ASCII literal did not survive"
[ "$(ask 'GRAPH <urn:g:erp> { ?b <urn:x:approves> <urn:x:invoices> ; <urn:x:limit> 50000 }')" = true ] || fail "the blank node lost its statements"
[ "$(ask 'GRAPH ?g { <urn:x:bob> ?p ?o }')" = false ] || fail "the later change is still there"
[ "$(ask '<urn:x:meta> <urn:x:version> "1"')" = true ] || fail "the default graph was not restored"
ok "restore replaces the graph exactly: named and default graphs, blank nodes, literals"

[ "$(entries)" = 4 ] || fail "the decision log was changed although it held entries ($(entries))"
grep -q 'Decision log: NOT restored' "$T/out" || fail "no explanation for skipping the decision log"
ok "a decision log that holds entries is left alone"

# Disaster: the decision log database is lost.
sql "DROP TABLE veromesh_decision_log CASCADE"
"$BACKUP" restore "$id" >"$T/out" 2>&1 || { cat "$T/out"; fail "restore into an empty database failed"; }
[ "$(entries)" = 3 ] || fail "decision log after restore: $(entries) entries"
if sql "DELETE FROM veromesh_decision_log" 2>/dev/null; then fail "the restored log is not append-only"; fi
ok "the decision log restores into an empty database, still append-only"

add_decision 4
"$BACKUP" restore "$id" --decision-log replace >/dev/null 2>&1 || fail "restore --decision-log replace failed"
[ "$(entries)" = 3 ] || fail "after replace: $(entries) entries"
ok "--decision-log replace discards later entries when asked"

# ---------------------------------------------------------------- refusals
age-keygen -o "$T/wrong" 2>/dev/null
if AGE_IDENTITY_FILE="$T/wrong" "$BACKUP" restore "$id" >"$T/out" 2>&1; then fail "restored with the wrong key"; fi
[ "$(quads)" = 6 ] || fail "a failed restore changed the graph"
ok "the wrong key is refused and nothing changes"

if AGE_IDENTITY_FILE='' "$BACKUP" restore "$id" >"$T/out" 2>&1; then fail "restored with no key"; fi
grep -q 'give the private key' "$T/out" || { cat "$T/out"; fail "no clear message without a key"; }
ok "no key: a clear message"

printf 'x' >>"$BACKUP_TARGET/$id/graph.nq.gz.age"
if "$BACKUP" restore "$id" >"$T/out" 2>&1; then fail "restored a damaged backup"; fi
grep -q 'does not match its checksum' "$T/out" || { cat "$T/out"; fail "damage not reported"; }
ok "a damaged backup is refused before anything changes"

if BACKUP_ENCRYPT=true AGE_RECIPIENTS_FILE=/nonexistent "$BACKUP" backup >"$T/out" 2>&1; then fail "backed up with no recipients"; fi
ok "encryption without recipients is refused"

# ---------------------------------------------------------------- unencrypted (development)
BACKUP_ENCRYPT=false "$BACKUP" backup >/dev/null 2>&1 || fail "unencrypted backup failed"
plain="$(latest)"
[ -f "$BACKUP_TARGET/$plain/graph.nq.gz" ] || fail "no plain graph file"
gzip -dc "$BACKUP_TARGET/$plain/graph.nq.gz" | grep -q Alice || fail "plain graph unreadable"
put_graph "$T/other.nq"
BACKUP_ENCRYPT=false AGE_IDENTITY_FILE='' "$BACKUP" restore "$plain" --decision-log skip >/dev/null 2>&1 || fail "plain restore failed"
[ "$(quads)" = 6 ] || fail "plain restore: $(quads) quads"
ok "unencrypted backups (development) back up and restore"

echo "all $pass backup tests passed"
