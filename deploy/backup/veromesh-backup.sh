#!/bin/sh
# shellcheck shell=busybox
# VeroMesh backups: the knowledge graph and the decision log, encrypted with the
# customer's own key, written to a volume or to object storage.
#
#   veromesh-backup backup                   take a backup now
#   veromesh-backup list                     the backups held, newest last
#   veromesh-backup restore <id> [--decision-log auto|skip|replace]
#
# A backup is a folder named after its UTC time (20261119T021700Z) holding
#   graph.nq.gz.age            every quad in the knowledge graph (N-Quads, gzip)
#   decision-log.pgdump.age    the decision log database (pg_dump custom format)
#   manifest.json              what is in it: counts, checksums, versions, key recipients
# The manifest is written last, so a folder without one is an unfinished backup.
# Without encryption (development only) the files have no .age suffix.
#
# Restoring the graph replaces the whole dataset through the writer in one
# transaction (a Graph Store Protocol PUT), so the read replicas follow through
# Kafka like any other write. Restoring the decision log, which is append-only,
# is only done into an empty database unless --decision-log replace is given:
# a backup must never quietly erase decisions recorded after it was taken.
#
# Environment
#   FUSEKI_URL            dataset URL, e.g. http://veromesh-fuseki:3030/veromesh (empty: no graph)
#   FUSEKI_WRITER_URL     where a restore writes, when FUSEKI_URL is a read replica (default FUSEKI_URL)
#   DECISION_LOG_URL      postgresql:// URL of the decision log database (empty: no decision log)
#   BACKUP_TARGET         a local folder (/backups) or an rclone remote (azure:backups/veromesh)
#   RCLONE_CONFIG         rclone configuration, for a remote target
#   BACKUP_KEEP           backups to keep (default 30); older ones are deleted after a backup
#   BACKUP_ENCRYPT        true (default) or false
#   AGE_RECIPIENTS_FILE   public keys to encrypt to (age or SSH), one per line
#   AGE_IDENTITY_FILE     private key, for restore only
#   WORK_DIR              scratch space (default /work)
#   BACKUP_RELEASE, BACKUP_CHART, BACKUP_APP_VERSION   recorded in the manifest
set -eu
set -o pipefail

TARGET="${BACKUP_TARGET:-/backups}"
KEEP="${BACKUP_KEEP:-30}"
ENCRYPT="${BACKUP_ENCRYPT:-true}"
WORK="${WORK_DIR:-/work}"
FUSEKI_URL="${FUSEKI_URL:-}"
FUSEKI_WRITER_URL="${FUSEKI_WRITER_URL:-$FUSEKI_URL}"
DECISION_LOG_URL="${DECISION_LOG_URL:-}"
TABLE=veromesh_decision_log

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { printf 'veromesh-backup: %s\n' "$*" >&2; exit 1; }

case "$KEEP" in '' | *[!0-9]*) die "BACKUP_KEEP must be a whole number, not '$KEEP'" ;; esac
[ "$KEEP" -ge 1 ] || die "BACKUP_KEEP must be at least 1"
case "$ENCRYPT" in true | false) ;; *) die "BACKUP_ENCRYPT must be true or false, not '$ENCRYPT'" ;; esac

suffix() { if [ "$ENCRYPT" = true ]; then printf '.age'; fi; }

# ---------------------------------------------------------------- storage
# A target starting with / (or .) is a folder; anything else is an rclone remote.
is_local() { case "$TARGET" in /* | ./* | ../*) return 0 ;; *) return 1 ;; esac; }

# The ids of complete backups (a folder with a manifest), oldest first.
complete_ids() {
  if is_local; then
    [ -d "$TARGET" ] || return 0
    for m in "$TARGET"/*/manifest.json; do
      [ -f "$m" ] && basename "$(dirname "$m")"
    done | sort
  else
    rclone lsf -R --files-only --include '/*/manifest.json' "$TARGET" 2>/dev/null \
      | sed 's|/manifest.json$||' | sort
  fi
}

# Every backup folder, complete or not.
all_ids() {
  if is_local; then
    [ -d "$TARGET" ] || return 0
    for d in "$TARGET"/*/; do [ -d "$d" ] && basename "$d"; done | sort
  else
    rclone lsf --dirs-only "$TARGET" 2>/dev/null | sed 's|/$||' | sort
  fi
}

remove_id() {
  if is_local; then rm -rf "${TARGET:?}/$1"; else rclone purge "$TARGET/$1"; fi
}

# Upload the files of $1 as backup $2; the manifest goes last.
upload() {
  src="$1" id="$2"
  if is_local; then
    mkdir -p "$TARGET/$id"
    for f in "$src"/*; do
      [ "$(basename "$f")" = manifest.json ] || cp "$f" "$TARGET/$id/"
    done
    cp "$src/manifest.json" "$TARGET/$id/manifest.json.tmp"
    mv "$TARGET/$id/manifest.json.tmp" "$TARGET/$id/manifest.json"
  else
    rclone copy --exclude manifest.json "$src" "$TARGET/$id"
    rclone copyto "$src/manifest.json" "$TARGET/$id/manifest.json"
  fi
}

download() {
  id="$1" dest="$2"
  if is_local; then
    [ -f "$TARGET/$id/manifest.json" ] || die "no complete backup $id in $TARGET"
    cp "$TARGET/$id"/* "$dest/"
  else
    rclone copy "$TARGET/$id" "$dest"
    [ -f "$dest/manifest.json" ] || die "no complete backup $id in $TARGET"
  fi
}

# ---------------------------------------------------------------- helpers
encrypt() {
  if [ "$ENCRYPT" = true ]; then age -R "$AGE_RECIPIENTS_FILE"; else cat; fi
}

decrypt() {  # decrypt <file>
  case "$1" in
    *.age)
      [ -n "${AGE_IDENTITY_FILE:-}" ] && [ -s "$AGE_IDENTITY_FILE" ] \
        || die "this backup is encrypted: give the private key (AGE_IDENTITY_FILE)"
      age -d -i "$AGE_IDENTITY_FILE" "$1" ;;
    *) cat "$1" ;;
  esac
}

sha() { sha256sum "$1" | cut -d' ' -f1; }
size() { wc -c <"$1" | tr -d ' '; }

# Count the lines passing through, into file $1 (N-Quads: one quad per line).
count_lines() { awk -v out="$1" '{ print } END { print NR > out }'; }

psql_value() { psql "$DECISION_LOG_URL" -X -q -t -A -v ON_ERROR_STOP=1 -c "$1"; }

# Entries in the decision log and its last sequence number ("0 0" if the table is missing).
log_state() {
  if [ "$(psql_value "SELECT to_regclass('$TABLE') IS NOT NULL")" = t ]; then
    psql_value "SELECT count(*) || ' ' || coalesce(max(seq), 0) FROM $TABLE"
  else
    echo "0 0"
  fi
}

fuseki_quads() {  # quads now in the writer's dataset, counted the same way as the backup
  curl -fsS --max-time 3600 -H 'Accept: application/n-quads' "$FUSEKI_WRITER_URL/data" | wc -l | tr -d ' '
}

new_workdir() {
  dir="$WORK/$1"
  rm -rf "$dir" && mkdir -p "$dir"
  printf '%s' "$dir"
}

# ---------------------------------------------------------------- backup
backup() {
  [ -n "$FUSEKI_URL" ] || [ -n "$DECISION_LOG_URL" ] || die "nothing to back up: set FUSEKI_URL or DECISION_LOG_URL"
  if [ "$ENCRYPT" = true ]; then
    [ -s "${AGE_RECIPIENTS_FILE:-}" ] || die "encryption is on but there are no recipients (AGE_RECIPIENTS_FILE)"
  fi
  id="$(date -u +%Y%m%dT%H%M%SZ)"
  dir="$(new_workdir "$id")"
  sfx="$(suffix)"
  graph=null
  dlog=null

  if [ -n "$FUSEKI_URL" ]; then
    log "Knowledge graph: reading every quad from $FUSEKI_URL"
    file="graph.nq.gz$sfx"
    curl -fsS --max-time 3600 -H 'Accept: application/n-quads' "$FUSEKI_URL/data" \
      | count_lines "$dir/.quads" | gzip -c | encrypt >"$dir/$file"
    quads="$(cat "$dir/.quads")"
    rm -f "$dir/.quads"
    [ "$quads" -gt 0 ] || log "warning: the knowledge graph is empty"
    graph="$(jq -n --arg f "$file" --argjson q "$quads" --arg s "$(sha "$dir/$file")" --argjson b "$(size "$dir/$file")" \
      '{file: $f, quads: $q, sha256: $s, bytes: $b}')"
    log "Knowledge graph: $quads quads"
  fi

  if [ -n "$DECISION_LOG_URL" ]; then
    log "Decision log: dumping the database"
    state="$(log_state)"
    file="decision-log.pgdump$sfx"
    pg_dump --format=custom --no-owner --no-privileges "$DECISION_LOG_URL" | encrypt >"$dir/$file"
    # Counted after the dump: the log only grows, so the dump holds at least this many.
    entries="${state% *}"
    last="${state#* }"
    dlog="$(jq -n --arg f "$file" --argjson e "$entries" --argjson l "$last" --arg s "$(sha "$dir/$file")" \
      --argjson b "$(size "$dir/$file")" '{file: $f, entries: $e, lastSeq: $l, sha256: $s, bytes: $b}')"
    log "Decision log: $entries entries (last sequence number $last)"
  fi

  recipients='[]'
  if [ "$ENCRYPT" = true ]; then
    recipients="$(grep -v '^[[:space:]]*#' "$AGE_RECIPIENTS_FILE" | grep -v '^[[:space:]]*$' | jq -R . | jq -s .)"
  fi
  jq -n --arg id "$id" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg release "${BACKUP_RELEASE:-}" --arg chart "${BACKUP_CHART:-}" --arg app "${BACKUP_APP_VERSION:-}" \
    --argjson enc "$ENCRYPT" --argjson rcp "$recipients" --argjson graph "$graph" --argjson dlog "$dlog" \
    '{format: 1, id: $id, createdAt: $at, release: $release, chart: $chart, appVersion: $app,
      encrypted: $enc, recipients: $rcp, graph: $graph, decisionLog: $dlog}' >"$dir/manifest.json"

  log "Writing backup $id to $TARGET"
  upload "$dir" "$id"
  rm -rf "$dir"

  # Retention: keep the newest $KEEP complete backups; drop unfinished ones.
  complete="$(complete_ids)"
  n="$(printf '%s\n' "$complete" | grep -c . || true)"
  if [ "$n" -gt "$KEEP" ]; then
    printf '%s\n' "$complete" | head -n "$((n - KEEP))" | while read -r old; do
      log "Retention: deleting backup $old"
      remove_id "$old"
    done
  fi
  for d in $(all_ids); do
    if [ "$d" != "$id" ] && ! printf '%s\n' "$complete" | grep -qx "$d"; then
      log "Deleting unfinished backup $d"
      remove_id "$d"
    fi
  done
  log "Backup $id complete"
  echo "BACKUP_ID=$id"
}

# ---------------------------------------------------------------- list
list() {
  ids="$(complete_ids)"
  [ -n "$ids" ] || { echo "No backups in $TARGET"; return 0; }
  dir="$(new_workdir list)"
  printf '%-18s %12s %16s %s\n' ID QUADS DECISIONS ENCRYPTED
  for id in $ids; do
    if is_local; then
      cp "$TARGET/$id/manifest.json" "$dir/m.json"
    else
      rclone copyto "$TARGET/$id/manifest.json" "$dir/m.json"
    fi
    jq -r '[.id, (.graph.quads // "-"), (.decisionLog.entries // "-"), .encrypted] | @tsv' "$dir/m.json" \
      | awk -F'\t' '{ printf "%-18s %12s %16s %s\n", $1, $2, $3, $4 }'
  done
  rm -rf "$dir"
}

# ---------------------------------------------------------------- restore
restore() {
  id="${1:-}"
  [ -n "$id" ] || die "usage: veromesh-backup restore <id> [--decision-log auto|skip|replace]"
  shift
  mode=auto
  while [ $# -gt 0 ]; do
    case "$1" in
      --decision-log) mode="${2:-}"; shift 2 ;;
      --decision-log=*) mode="${1#*=}"; shift ;;
      *) die "unknown option $1" ;;
    esac
  done
  case "$mode" in auto | skip | replace) ;; *) die "--decision-log must be auto, skip or replace" ;; esac

  dir="$(new_workdir "restore-$id")"
  log "Fetching backup $id from $TARGET"
  download "$id" "$dir"
  m="$dir/manifest.json"
  [ "$(jq -r .format "$m")" = 1 ] || die "unknown backup format in $id"

  # Every file must match its checksum before anything is changed.
  for part in graph decisionLog; do
    file="$(jq -r ".$part.file // empty" "$m")"
    [ -n "$file" ] || continue
    [ "$(sha "$dir/$file")" = "$(jq -r ".$part.sha256" "$m")" ] || die "$file does not match its checksum: the backup is damaged"
  done
  log "Checksums match"

  # A trial run with the key before anything is changed: a wrong key, or a file
  # that does not decrypt or decompress to what the manifest says, stops here.
  # (Otherwise the graph would be replaced with whatever arrived before the failure.)
  file="$(jq -r '.graph.file // empty' "$m")"
  if [ -n "$file" ]; then
    n="$(decrypt "$dir/$file" | gzip -dc | wc -l | tr -d ' ')" || die "cannot decrypt $file (wrong key?)"
    [ "$n" = "$(jq -r .graph.quads "$m")" ] || die "$file holds $n quads, the manifest says $(jq -r .graph.quads "$m")"
  fi
  file="$(jq -r '.decisionLog.file // empty' "$m")"
  if [ -n "$file" ] && [ "$mode" != skip ]; then
    decrypt "$dir/$file" | pg_restore --list >/dev/null || die "cannot decrypt or read $file (wrong key?)"
  fi
  log "The key opens the backup"

  file="$(jq -r '.graph.file // empty' "$m")"
  if [ -n "$file" ]; then
    [ -n "$FUSEKI_WRITER_URL" ] || die "the backup holds a knowledge graph but FUSEKI_URL is not set"
    want="$(jq -r .graph.quads "$m")"
    log "Knowledge graph: replacing the dataset with $want quads (one transaction, through the writer)"
    decrypt "$dir/$file" | gzip -dc \
      | curl -fsS --max-time 7200 -X PUT -T - -H 'Content-Type: application/n-quads' "$FUSEKI_WRITER_URL/data" >/dev/null
    have="$(fuseki_quads)"
    [ "$have" = "$want" ] || die "after the restore the graph holds $have quads, the backup $want"
    log "Knowledge graph: restored, $have quads"
  fi

  file="$(jq -r '.decisionLog.file // empty' "$m")"
  if [ -n "$file" ] && [ "$mode" != skip ]; then
    [ -n "$DECISION_LOG_URL" ] || die "the backup holds a decision log but DECISION_LOG_URL is not set"
    state="$(log_state)"
    entries="${state% *}"
    if [ "$entries" -gt 0 ] && [ "$mode" != replace ]; then
      log "Decision log: NOT restored. The database already holds $entries entries, and the log is"
      log "append-only. Restore it only into an empty database, or pass --decision-log replace to"
      log "discard what is there (decisions recorded after this backup would be lost)."
    else
      want="$(jq -r .decisionLog.entries "$m")"
      log "Decision log: restoring $want entries"
      # Streamed, so the decrypted log never lands on disk.
      decrypt "$dir/$file" | pg_restore --clean --if-exists --no-owner --no-privileges \
        --single-transaction --exit-on-error -d "$DECISION_LOG_URL"
      have="$(log_state)"
      have="${have% *}"
      [ "$have" -ge "$want" ] || die "after the restore the decision log holds $have entries, the backup at least $want"
      log "Decision log: restored, $have entries"
    fi
  elif [ -n "$file" ]; then
    log "Decision log: skipped (--decision-log skip)"
  fi
  rm -rf "$dir"
  log "Restore of $id complete"
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  backup) backup ;;
  list) list ;;
  restore) restore "$@" ;;
  *) die "usage: veromesh-backup backup | list | restore <id> [--decision-log auto|skip|replace]" ;;
esac
