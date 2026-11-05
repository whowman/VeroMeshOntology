"""veromesh-loader: Excel workbook <-> VeroMesh model, through the REST API.

    veromesh-loader template -o model.xlsx [--example]
    veromesh-loader import model.xlsx --source hr [--mode replace|merge] [--dry-run]
    veromesh-loader export --source hr -o hr.xlsx
    veromesh-loader sources

The API address comes from --api or VEROMESH_API (default http://localhost:8700);
a bearer token, when the API requires one, from --token or VEROMESH_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

import httpx

from . import __version__
from .client import ApiClient, ApiError
from .workbook import ReadResult, build, read

BATCH = 500


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _where(rr: ReadResult, item: dict) -> str:
    sheet, row = rr.rows.get((item.get("type") or "", item.get("id") or ""), (None, None))
    if sheet is None:
        label = item.get("type") or "model"
        return f"{label} '{item.get('id')}'"
    col = rr.columns.get((item["type"], item.get("field") or ""))
    return f"{sheet} row {row}" + (f", {col}" if col else "")


def _print_report(report: dict, rr: ReadResult) -> None:
    s = report["summary"]
    _out(f"Validation: {s['errors']} errors, {s['violations']} violations, {s['warnings']} warnings.")
    for kind in ("errors", "violations", "warnings"):
        for item in report.get(kind, []):
            _out(f"  {kind[:-1]:9} {_where(rr, item)}: {item['message']}")
    if report.get("preexistingViolations"):
        _out(
            f"  note: the model already had {report['preexistingViolations']} violations unrelated to this load."
        )


def cmd_template(api: ApiClient, args) -> int:
    schema = api.schema()
    entities = None
    if args.example:
        entities = defaultdict(list)
        for it in api.example_items():
            entities[it["type"]].append(it["entity"])
    Path(args.output).write_bytes(build(schema, entities))
    _out(
        f"Wrote {args.output} ({len(schema)} sheets{', with the procure-to-pay example' if args.example else ''})."
    )
    return 0


def cmd_import(api: ApiClient, args) -> int:
    schema = api.schema()
    path = Path(args.file)
    rr = read(path.read_bytes(), schema)
    rows = sum(rr.counts.values())
    _out(f"Read {rows} rows from {path.name} ({', '.join(f'{k}: {v}' for k, v in rr.counts.items() if v)}).")
    if rr.errors:
        _out(f"Workbook problems ({len(rr.errors)}), nothing sent:")
        for e in rr.errors:
            _out(f"  error     {e}")
        return 1
    source = args.source or _slug(path.stem)
    cs = api.create_changeset(source, args.mode, kind="spreadsheet", file=path.name)["id"]
    for i in range(0, len(rr.items), BATCH):
        api.add_items(cs, rr.items[i : i + BATCH])
    report = api.validate(cs)
    _print_report(report, rr)
    if args.dry_run or not report["valid"]:
        api.discard(cs)
        state = (
            "Dry run: nothing written."
            if args.dry_run
            else "Not written: fix the errors and violations above."
        )
        _out(state)
        return 0 if report["valid"] else 1
    committed = api.commit(cs)
    _out(f"Committed changeset {cs}: source '{source}' now has {committed.get('facts')} facts ({args.mode}).")
    return 0


def cmd_export(api: ApiClient, args) -> int:
    schema = api.schema()
    known = {s["source"] for s in api.sources()}
    if args.source not in known:
        _out(f"No source named '{args.source}'. Known sources: {', '.join(sorted(known)) or 'none'}.")
        return 1
    entities = {t["type"]: api.entities(t["type"], args.source) for t in schema}
    Path(args.output).write_bytes(build(schema, entities))
    _out(
        f"Wrote {args.output} ({sum(len(v) for v in entities.values())} entities from source '{args.source}')."
    )
    return 0


def cmd_sources(api: ApiClient, args) -> int:
    rows = api.sources()
    if not rows:
        _out("No sources loaded yet.")
        return 0
    for s in rows:
        _out(
            f"{s['source']:24} {s['kind']:12} {str(s.get('facts') or ''):>6} facts  {s['updatedAt']}  {s.get('file') or ''}"
        )
    return 0


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64] or "workbook"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veromesh-loader", description="Load the VeroMesh model from Excel workbooks."
    )
    p.add_argument("--api", default=os.environ.get("VEROMESH_API", "http://localhost:8700"))
    p.add_argument("--token", default=os.environ.get("VEROMESH_TOKEN"))
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("template", help="Write a blank (or example) workbook generated from the API's schema")
    t.add_argument("-o", "--output", default="veromesh-model.xlsx")
    t.add_argument("--example", action="store_true", help="Fill it with the procure-to-pay example")

    i = sub.add_parser("import", help="Validate a workbook and commit it as one changeset")
    i.add_argument("file")
    i.add_argument("--source", help="Source name (default: from the file name)")
    i.add_argument(
        "--mode",
        choices=("replace", "merge"),
        default="replace",
        help="replace: the workbook becomes the whole source (default); merge: upsert into it",
    )
    i.add_argument("--dry-run", action="store_true", help="Validate only; write nothing")

    e = sub.add_parser("export", help="Write a source back out as an editable workbook")
    e.add_argument("--source", required=True)
    e.add_argument("-o", "--output")

    sub.add_parser("sources", help="List sources loaded into the model")
    return p


def main(argv: list[str] | None = None, http: httpx.Client | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "export" and not args.output:
        args.output = f"veromesh-{args.source}.xlsx"
    api = ApiClient(args.api, args.token, http=http)
    commands = {"template": cmd_template, "import": cmd_import, "export": cmd_export, "sources": cmd_sources}
    try:
        return commands[args.command](api, args)
    except ApiError as exc:
        _out(f"Error: {exc.detail}")
        if exc.body.get("report"):
            _print_report(exc.body["report"], ReadResult())
        return 2
    except (ValueError, OSError) as exc:
        _out(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
