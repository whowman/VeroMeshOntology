"""veromesh-connect: run VeroMesh connectors from the command line.

    veromesh-connect spreadsheet template -o model.xlsx [--example]
    veromesh-connect spreadsheet import model.xlsx --source p2p [--mode merge] [--dry-run]
    veromesh-connect spreadsheet export --source p2p -o p2p.xlsx
    veromesh-connect csv import people.csv --source hr --mapping workday.yaml [--dry-run]
    veromesh-connect csv import exports/ --source cmdb          # one file per entity type
    veromesh-connect sources
    veromesh-connect runs [--source p2p]
    veromesh-connect cleanup [--older-than-hours 24]

Connection settings (flags or environment):
    --api / VEROMESH_API                    default http://localhost:8700
    --token / VEROMESH_TOKEN                static bearer token
    VEROMESH_TOKEN_URL, VEROMESH_CLIENT_ID, VEROMESH_CLIENT_SECRET, VEROMESH_SCOPE
                                            OAuth 2.0 client credentials (e.g. Keycloak)
    VEROMESH_CA_BUNDLE                      private CA for TLS
    VEROMESH_RETRIES / VEROMESH_TIMEOUT     defaults 4 and 60 seconds
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

import httpx

from . import __version__
from .core import ApiError, ApiSession, ClientCredentials, Runner, RunOptions, StaticToken, Transport
from .plugins import CsvConnector, SpreadsheetConnector, build_workbook


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64] or "source"


def _load_mapping(path: str) -> dict:
    import yaml

    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a mapping file is a YAML object with 'rules'.")
    return data


def session_from(args, http: httpx.Client | None = None) -> ApiSession:
    transport = Transport(
        args.api,
        timeout=float(os.environ.get("VEROMESH_TIMEOUT", "60")),
        retries=int(os.environ.get("VEROMESH_RETRIES", "4")),
        ca_bundle=os.environ.get("VEROMESH_CA_BUNDLE"),
        http=http,
    )
    token_url = os.environ.get("VEROMESH_TOKEN_URL")
    if token_url:
        auth = ClientCredentials(
            transport,
            token_url,
            os.environ["VEROMESH_CLIENT_ID"],
            os.environ["VEROMESH_CLIENT_SECRET"],
            os.environ.get("VEROMESH_SCOPE"),
        )
    else:
        auth = StaticToken(args.token)
    return ApiSession(transport, auth)


# ---------------------------------------------------------------------------- commands


def spreadsheet_template(api: ApiSession, args) -> int:
    schema = api.schema()
    entities = None
    if args.example:
        entities = defaultdict(list)
        for it in api.example_items():
            entities[it["type"]].append(it["entity"])
    Path(args.output).write_bytes(build_workbook(schema, entities))
    _out(
        f"Wrote {args.output} ({len(schema)} sheets{', with the procure-to-pay example' if args.example else ''})."
    )
    return 0


def spreadsheet_import(api: ApiSession, args) -> int:
    conn = SpreadsheetConnector(args.file)
    opts = RunOptions(source=args.source or _slug(Path(args.file).stem), mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def spreadsheet_export(api: ApiSession, args) -> int:
    known = {s["source"] for s in api.sources()}
    if args.source not in known:
        _out(f"No source named '{args.source}'. Known sources: {', '.join(sorted(known)) or 'none'}.")
        return 1
    schema = api.schema()
    entities = {t["type"]: api.entities(t["type"], args.source) for t in schema}
    output = args.output or f"veromesh-{args.source}.xlsx"
    Path(output).write_bytes(build_workbook(schema, entities))
    _out(f"Wrote {output} ({sum(len(v) for v in entities.values())} entities from source '{args.source}').")
    return 0


def csv_import(api: ApiSession, args) -> int:
    mapping = _load_mapping(args.mapping) if args.mapping else None
    conn = CsvConnector(args.path, type_=args.type, mapping=mapping)
    opts = RunOptions(source=args.source or _slug(Path(args.path).stem), mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def sources(api: ApiSession, args) -> int:
    rows = api.sources()
    if not rows:
        _out("No sources loaded yet.")
    for s in rows:
        _out(
            f"{s['source']:24} {s['kind']:12} {str(s.get('facts') or ''):>6} facts  {s['updatedAt']}  {s.get('file') or ''}"
        )
    return 0


def runs(api: ApiSession, args) -> int:
    rows = api.runs(source=args.source, limit=args.limit)
    if not rows:
        _out("No runs yet.")
    for r in rows:
        items = r["items"]["upsert"] + r["items"]["delete"]
        _out(
            f"{r['createdAt']}  {r['id']}  {r['source']:20} {r.get('connector') or r['kind']:12} "
            f"{r['mode']:8} {r['status']:10} {items:>5} items  {r.get('file') or ''}"
        )
    return 0


def cleanup(api: ApiSession, args) -> int:
    gone = api.cleanup_stale(args.older_than_hours)
    _out(
        f"Discarded {len(gone)} changesets left open by unfinished runs."
        + (f" ({', '.join(gone)})" if gone else "")
    )
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="veromesh-connect", description="Run VeroMesh connectors.")
    p.add_argument("--api", default=os.environ.get("VEROMESH_API", "http://localhost:8700"))
    p.add_argument("--token", default=os.environ.get("VEROMESH_TOKEN"))
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="connector", required=True)

    def load_flags(sp):
        sp.add_argument("--source", help="Source name in VeroMesh (default: from the file name)")
        sp.add_argument(
            "--mode",
            choices=("replace", "merge"),
            default="replace",
            help="replace: this input becomes the whole source (default); merge: upsert into it",
        )
        sp.add_argument("--dry-run", action="store_true", help="Validate only; write nothing")

    ss = sub.add_parser("spreadsheet", help="The VeroMesh model workbook (.xlsx)").add_subparsers(
        dest="action", required=True
    )
    t = ss.add_parser("template", help="Write a blank or example workbook built from the API's schema")
    t.add_argument("-o", "--output", default="veromesh-model.xlsx")
    t.add_argument("--example", action="store_true")
    t.set_defaults(fn=spreadsheet_template)
    i = ss.add_parser("import", help="Validate and commit a workbook")
    i.add_argument("file")
    load_flags(i)
    i.set_defaults(fn=spreadsheet_import)
    e = ss.add_parser("export", help="Write a source back out as an editable workbook")
    e.add_argument("--source", required=True)
    e.add_argument("-o", "--output")
    e.set_defaults(fn=spreadsheet_export)

    cs = sub.add_parser("csv", help="CSV files: a file or a folder").add_subparsers(
        dest="action", required=True
    )
    ci = cs.add_parser("import", help="Validate and commit CSV data")
    ci.add_argument("path", help="A .csv file or a folder of them")
    ci.add_argument("--type", help="Entity type of a single file (default: from the file name)")
    ci.add_argument("--mapping", help="YAML mapping file for columns from another system")
    load_flags(ci)
    ci.set_defaults(fn=csv_import)

    sub.add_parser("sources", help="Sources loaded into the model").set_defaults(fn=sources)
    r = sub.add_parser("runs", help="Recent connector runs (changesets), newest first")
    r.add_argument("--source")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(fn=runs)
    c = sub.add_parser("cleanup", help="Discard changesets left open by crashed runs")
    c.add_argument("--older-than-hours", type=float, default=24)
    c.set_defaults(fn=cleanup)
    return p


def main(argv: list[str] | None = None, http: httpx.Client | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.fn(session_from(args, http), args)
    except ApiError as exc:
        _out(f"Error: {exc.detail}")
        return 2
    except (ValueError, OSError, KeyError) as exc:
        _out(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
