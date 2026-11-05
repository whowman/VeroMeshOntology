#!/usr/bin/env bash
# Validate and import a model workbook into a running Veromesh service.
#
#   scripts/import-model.sh model.xlsx               # source name from the file name
#   scripts/import-model.sh model.xlsx my-source     # explicit source name
#   DRY_RUN=1 scripts/import-model.sh model.xlsx     # validate only, write nothing
#
# Get a blank template:   curl -o template.xlsx localhost:8700/model/template
# Or a filled example:    curl -o example.xlsx 'localhost:8700/model/template?example=true'
set -euo pipefail

FILE="${1:?usage: scripts/import-model.sh <workbook.xlsx> [source-name]}"
API="${VEROMESH_API:-http://localhost:8700}"
PARAMS="dry_run=${DRY_RUN:+true}"
[[ "$PARAMS" == "dry_run=" ]] && PARAMS="dry_run=false"
[[ -n "${2:-}" ]] && PARAMS="$PARAMS&source=$2"

OUT="$(mktemp)"
CODE="$(curl -sS -o "$OUT" -w '%{http_code}' -X POST "$API/model/import?$PARAMS" -F "file=@$FILE")"
python3 - "$OUT" "$CODE" <<'PY'
import json, sys
report, code = json.load(open(sys.argv[1])), sys.argv[2]
if "summary" not in report:
    print(f"Import failed ({code}): {report.get('detail', report)}"); sys.exit(1)
s = report["summary"]
state = "written" if report["written"] else ("validated only (dry run)" if report["dry_run"] and code == "200" else "NOT written")
print(f"Source '{report['source']}': {state}. {s['errors']} errors, {s['violations']} violations, {s['warnings']} warnings.")
for kind in ("errors", "violations", "warnings"):
    for item in report[kind]:
        where = f"{item.get('sheet', '?')} row {item.get('row', '?')}"
        col = f", {item['column']}" if "column" in item else ""
        print(f"  {kind[:-1]:9} {where}{col}: {item['message']}")
sys.exit(0 if code == "200" else 1)
PY
