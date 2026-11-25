"""veromesh-connect: run VeroMesh connectors from the command line.

    veromesh-connect spreadsheet template -o model.xlsx [--example]
    veromesh-connect spreadsheet import model.xlsx --source p2p [--mode merge] [--dry-run]
    veromesh-connect spreadsheet export --source p2p -o p2p.xlsx
    veromesh-connect csv import people.csv --source hr --mapping workday.yaml [--dry-run]
    veromesh-connect csv import exports/ --source cmdb          # one file per entity type
    veromesh-connect workday import --url https://wd2-impl-services1.workday.com --tenant acme [--via rest]
    veromesh-connect workday changes --since 2026-11-01
    veromesh-connect workday approvals --tenant acme --role-map groups.yaml
    veromesh-connect entra import --tenant <tenant ID> [--role-group-prefix SG-] [--agent-tag AIAgent]
    veromesh-connect entra changes --state entra-delta.json
    veromesh-connect erp import --url https://acme.fa.us2.oraclecloud.com [--role-map erp-roles.yaml]
    veromesh-connect erp exceptions --since 2026-11-01
    veromesh-connect servicenow import --url https://acme.service-now.com
    veromesh-connect servicenow ea --url https://acme.service-now.com [--id-field number]
    veromesh-connect leanix import --url https://acme.leanix.net [--id-from externalId]
    veromesh-connect halo import --url https://acme.haloitsm.com
    veromesh-connect sources
    veromesh-connect runs [--source p2p]
    veromesh-connect cleanup [--older-than-hours 24]

Connection settings (flags or environment):
    --api / VEROMESH_API                    default http://localhost:8700
    --token / VEROMESH_TOKEN                static bearer token
    VEROMESH_TOKEN_URL, VEROMESH_CLIENT_ID, VEROMESH_CLIENT_SECRET, VEROMESH_SCOPE
                                            OAuth 2.0 client credentials (e.g. Keycloak)
    VEROMESH_CA_BUNDLE                      private CA for TLS
    VEROMESH_RETRIES / VEROMESH_TIMEOUT     defaults 4 and 60 seconds (also used for source systems)

Source systems:
    WORKDAY_URL, WORKDAY_TENANT, WORKDAY_CLIENT_ID, WORKDAY_CLIENT_SECRET,
    WORKDAY_REFRESH_TOKEN (without it, client credentials)
    ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET,
    ENTRA_GRAPH_URL / ENTRA_LOGIN_URL      defaults https://graph.microsoft.com and
                                            https://login.microsoftonline.com
    ORACLE_URL, ORACLE_USER, ORACLE_PASSWORD
                                            Oracle Fusion Cloud (basic authentication)
    HALO_URL, HALO_CLIENT_ID, HALO_CLIENT_SECRET (HALO_TENANT for hosted instances)
                                            HaloITSM (OAuth client credentials)
    LEANIX_URL, LEANIX_API_TOKEN               SAP LeanIX (a technical user's API token)
    SERVICENOW_URL, SERVICENOW_USER, SERVICENOW_PASSWORD (or SERVICENOW_TOKEN)
                                            ServiceNow (basic authentication or an OAuth token)
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
from .core.source import SOURCE_RETRY, RefreshToken, SourceApi, SourceError
from .plugins import (
    BasicAuth,
    CsvConnector,
    EntraChangesConnector,
    EntraConnector,
    GraphClient,
    OracleClient,
    OracleErpConnector,
    ServiceNowClient,
    ServiceNowCmdbConnector,
    SpreadsheetConnector,
    WorkdayChangesConnector,
    WorkdayClient,
    WorkdayConnector,
    build_workbook,
)


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


def _source_transport(url: str, label: str, http: httpx.Client | None) -> Transport:
    return Transport(
        url,
        timeout=float(os.environ.get("VEROMESH_TIMEOUT", "60")),
        retries=int(os.environ.get("VEROMESH_RETRIES", "4")),
        http=http,
        label=label,
        retry_on=SOURCE_RETRY,
    )


def _need(value: str | None, flag: str, env: str) -> str:
    if not value:
        raise ValueError(f"{flag} (or {env}) is required")
    return value


def workday_client(args, http: httpx.Client | None = None) -> WorkdayClient:
    url = _need(args.url, "--url", "WORKDAY_URL")
    tenant = _need(args.tenant, "--tenant", "WORKDAY_TENANT")
    t = _source_transport(url, "Workday", http)
    token_url = f"{t.base_url}/ccx/oauth2/{tenant}/token"
    cid, secret = os.environ.get("WORKDAY_CLIENT_ID", ""), os.environ.get("WORKDAY_CLIENT_SECRET", "")
    if os.environ.get("WORKDAY_REFRESH_TOKEN"):
        auth = RefreshToken(t, token_url, cid, secret, os.environ["WORKDAY_REFRESH_TOKEN"])
    else:
        auth = ClientCredentials(t, token_url, cid, secret)
    return WorkdayClient(SourceApi("Workday", t, auth), tenant)


def graph_client(args, http: httpx.Client | None = None) -> GraphClient:
    tenant = _need(args.tenant, "--tenant", "ENTRA_TENANT_ID")
    graph = _source_transport(args.graph, "Microsoft Graph", http)
    login = _source_transport(args.login, "the Microsoft Entra sign-in", http)
    auth = ClientCredentials(
        login,
        f"{login.base_url}/{tenant}/oauth2/v2.0/token",
        os.environ.get("ENTRA_CLIENT_ID", ""),
        os.environ.get("ENTRA_CLIENT_SECRET", ""),
        "https://graph.microsoft.com/.default",
    )
    return GraphClient(SourceApi("Microsoft Graph", graph, auth))


def oracle_client(args, http: httpx.Client | None = None) -> OracleClient:
    url = _need(args.url, "--url", "ORACLE_URL")
    t = _source_transport(url, "Oracle Fusion", http)
    user = _need(os.environ.get("ORACLE_USER"), "ORACLE_USER", "the environment")
    auth = BasicAuth(user, os.environ.get("ORACLE_PASSWORD", ""))
    return OracleClient(SourceApi("Oracle Fusion", t, auth))


def servicenow_client(args, http: httpx.Client | None = None) -> ServiceNowClient:
    from .plugins import BearerToken

    url = _need(args.url, "--url", "SERVICENOW_URL")
    t = _source_transport(url, "ServiceNow", http)
    if os.environ.get("SERVICENOW_TOKEN"):
        auth = BearerToken(os.environ["SERVICENOW_TOKEN"])
    else:
        user = _need(os.environ.get("SERVICENOW_USER"), "SERVICENOW_USER", "the environment")
        auth = BasicAuth(user, os.environ.get("SERVICENOW_PASSWORD", ""))
    return ServiceNowClient(SourceApi("ServiceNow", t, auth))


def halo_import(api: ApiSession, args) -> int:
    from .plugins import HaloClient, HaloConnector

    url = _need(args.url, "--url", "HALO_URL").rstrip("/")
    http = getattr(args, "source_http", None)
    t = _source_transport(url, "HaloITSM", http)
    tenant = os.environ.get("HALO_TENANT")
    auth = ClientCredentials(
        t,
        f"{url}/auth/token" + (f"?tenant={tenant}" if tenant else ""),
        os.environ.get("HALO_CLIENT_ID", ""),
        os.environ.get("HALO_CLIENT_SECRET", ""),
        "all",
    )
    client = HaloClient(SourceApi("HaloITSM", t, auth))
    people, systems = api.entities("people"), api.entities("systems")
    source = args.source or "halo"
    ok = (
        Runner(api, _out)
        .run(
            HaloConnector(client, people=people, systems=systems),
            RunOptions(source=source, mode=args.mode, dry_run=args.dry_run),
        )
        .ok
    )
    agents_source = args.agents_source or f"{source}-agents"
    ok = (
        Runner(api, _out)
        .run(
            HaloConnector(client, people=people, part="agents"),
            RunOptions(source=agents_source, mode=args.mode, dry_run=args.dry_run),
        )
        .ok
        and ok
    )
    return 0 if ok else 1


def servicenow_import(api: ApiSession, args) -> int:
    from .plugins.servicenow import duplicates

    client = servicenow_client(args, getattr(args, "source_http", None))
    known = api.entities("people")
    conn = ServiceNowCmdbConnector(client, people=known)
    source = args.source or "servicenow"
    ok = Runner(api, _out).run(conn, RunOptions(source=source, mode=args.mode, dry_run=args.dry_run)).ok
    users = ServiceNowCmdbConnector(client, people=known, part="users")
    users_source = args.users_source or f"{source}-users"
    ok = (
        Runner(api, _out).run(users, RunOptions(source=users_source, mode=args.mode, dry_run=args.dry_run)).ok
        and ok
    )
    if ok and not args.dry_run:
        found = duplicates(conn.apps)
        api.call("PUT", f"/findings/reported/{source}", json=found)
        _out(f"Reported {len(found)} likely duplicate systems for source '{source}'.")
    return 0 if ok else 1


def servicenow_ea(api: ApiSession, args) -> int:
    from .plugins import ServiceNowEaConnector

    conn = ServiceNowEaConnector(
        servicenow_client(args, getattr(args, "source_http", None)),
        people=api.entities("people"),
        id_field=args.id_field,
        parent_field=args.parent_field,
    )
    opts = RunOptions(source=args.source or "servicenow-ea", mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def leanix_import(api: ApiSession, args) -> int:
    from .plugins import LeanixClient, LeanixConnector
    from .plugins.leanix import ApiToken

    url = _need(args.url, "--url", "LEANIX_URL").rstrip("/")
    t = _source_transport(url, "SAP LeanIX", getattr(args, "source_http", None))
    token = _need(os.environ.get("LEANIX_API_TOKEN"), "LEANIX_API_TOKEN", "the environment")
    conn = LeanixConnector(
        LeanixClient(SourceApi("SAP LeanIX", t, ApiToken(t, token))),
        people=api.entities("people"),
        id_from=args.id_from,
        owner_role=args.owner_role,
    )
    opts = RunOptions(source=args.source or "leanix", mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def erp_import(api: ApiSession, args) -> int:
    conn = OracleErpConnector(
        oracle_client(args, getattr(args, "source_http", None)),
        role_map=_load_mapping(args.role_map) if args.role_map else None,
        people=api.entities("people"),
    )
    opts = RunOptions(source=args.source or "erp", mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def erp_exceptions(api: ApiSession, args) -> int:
    from datetime import date

    from .plugins.oracle_erp import exceptions

    found = exceptions(
        oracle_client(args, getattr(args, "source_http", None)),
        date.fromisoformat(args.since),
        people=api.entities("people"),
        authorities=api.entities("authorities"),
        co_approvals=api.entities("co-approvals"),
    )
    kinds: dict[str, int] = defaultdict(int)
    for f in found:
        kinds[f["kind"]] += 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "none"
    if args.dry_run:
        _out(f"Found {len(found)} approval exceptions ({summary}); dry run, nothing sent.")
        return 0
    source = args.source or "erp"
    api.call("PUT", f"/findings/reported/{source}", json=found)
    _out(f"Reported {len(found)} approval exceptions for source '{source}' ({summary}).")
    return 0


def workday_import(api: ApiSession, args) -> int:
    role_map = _load_mapping(args.role_map) if args.role_map else None
    conn = WorkdayConnector(
        workday_client(args, getattr(args, "source_http", None)),
        via=args.via,
        report_owner=args.report_owner,
        report=args.report,
        roles=not args.no_roles,
        role_map=role_map,
        manager_role=args.manager_role,
    )
    opts = RunOptions(source=args.source or "workday", mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def workday_approvals(api: ApiSession, args) -> int:
    from .plugins.workday import WorkdayApprovalsConnector

    conn = WorkdayApprovalsConnector(
        workday_client(args, getattr(args, "source_http", None)),
        report_owner=args.report_owner,
        report=args.report,
        process_map=_load_mapping(args.process_map) if args.process_map else None,
        role_map=_load_mapping(args.role_map) if args.role_map else None,
        policy=args.policy,
        currency=args.currency,
    )
    opts = RunOptions(source=args.source or "workday-approvals", mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def workday_changes(api: ApiSession, args) -> int:
    from datetime import date

    conn = WorkdayChangesConnector(
        workday_client(args, getattr(args, "source_http", None)),
        date.fromisoformat(args.since),
        report_owner=args.report_owner,
        report=args.report,
        through=date.fromisoformat(args.through) if args.through else None,
        roles=not args.no_roles,
        role_map=_load_mapping(args.role_map) if args.role_map else None,
    )
    results = Runner(api, _out).run_events(
        conn, RunOptions(source=args.source or "workday", dry_run=args.dry_run)
    )
    _out(
        f"{len(results)} events, {sum(not r.ok for r in results)} failed."
        + (f" Next run: --since {conn.last_entered}" if conn.last_entered else " No changes.")
    )
    return 0 if all(r.ok for r in results) else 1


def entra_import(api: ApiSession, args) -> int:
    conn = EntraConnector(
        graph_client(args, getattr(args, "source_http", None)),
        tenant=args.tenant,
        role_group_prefix=args.role_group_prefix,
        agent_tag=args.agent_tag,
    )
    opts = RunOptions(source=args.source or "entra", mode=args.mode, dry_run=args.dry_run)
    return 0 if Runner(api, _out).run(conn, opts).ok else 1


def entra_changes(api: ApiSession, args) -> int:
    conn = EntraChangesConnector(
        graph_client(args, getattr(args, "source_http", None)),
        args.state,
        role_group_prefix=args.role_group_prefix,
    )
    results = Runner(api, _out).run_events(
        conn, RunOptions(source=args.source or "entra", dry_run=args.dry_run)
    )
    if conn.initialized:
        _out(f"Recorded the current position in {args.state}; later runs apply the changes since.")
        return 0
    failed = sum(not r.ok for r in results)
    if failed or args.dry_run:
        _out(
            f"{len(results)} events, {failed} failed. {args.state} not advanced, so they are retried next run."
        )
        return 1 if failed else 0
    conn.save()
    _out(f"{len(results)} events applied; position saved in {args.state}.")
    return 0


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

    def workday_flags(sp, report: str):
        sp.add_argument("--url", default=os.environ.get("WORKDAY_URL"), help="Workday services host")
        sp.add_argument("--tenant", default=os.environ.get("WORKDAY_TENANT"))
        sp.add_argument("--report-owner", default="ISU_VeroMesh", help="Owner of the custom report (RaaS)")
        sp.add_argument("--report", default=report, help="Custom report name (RaaS)")
        sp.add_argument("--role-map", help="YAML object: business title or security group -> role ID")
        sp.add_argument("--no-roles", action="store_true", help="Do not write roles from business titles")

    wd = sub.add_parser("workday", help="Workday HCM: workers, positions, managers").add_subparsers(
        dest="action", required=True
    )
    wi = wd.add_parser("import", help="Load every current worker")
    workday_flags(wi, "VeroMesh_Workers")
    wi.add_argument(
        "--via", choices=("raas", "rest"), default="raas", help="Custom report (default) or REST API"
    )
    wi.add_argument(
        "--manager-role",
        help="Role every worker with direct reports fills (the group business processes call Manager), e.g. Manager",
    )
    load_flags(wi)
    wi.set_defaults(fn=workday_import)
    wa = wd.add_parser(
        "approvals", help="Load business process approval rules (promotions, pay, hires, terminations)"
    )
    workday_flags(wa, "VeroMesh_Approval_Rules")
    wa.add_argument("--process-map", help="YAML object: business process type -> action ID")
    wa.add_argument(
        "--policy", help="Policy ID the rules are delegated by (e.g. the delegation of authority policy)"
    )
    wa.add_argument("--currency", default="USD", help="Currency of amount conditions")
    load_flags(wa)
    wa.set_defaults(fn=workday_approvals)
    wc = wd.add_parser("changes", help="Apply worker changes entered since a date, one event each")
    workday_flags(wc, "VeroMesh_Worker_Changes")
    wc.add_argument("--since", required=True, help="YYYY-MM-DD: changes entered on or after")
    wc.add_argument("--through", help="YYYY-MM-DD (default: today in Workday)")
    wc.add_argument("--source")
    wc.add_argument("--dry-run", action="store_true")
    wc.set_defaults(fn=workday_changes)

    def entra_flags(sp):
        sp.add_argument("--tenant", default=os.environ.get("ENTRA_TENANT_ID"), help="Directory (tenant) ID")
        sp.add_argument("--graph", default=os.environ.get("ENTRA_GRAPH_URL", "https://graph.microsoft.com"))
        sp.add_argument(
            "--login", default=os.environ.get("ENTRA_LOGIN_URL", "https://login.microsoftonline.com")
        )
        sp.add_argument(
            "--role-group-prefix", help="Groups named <prefix><role ID> give the roles people fill"
        )

    en = sub.add_parser(
        "entra", help="Microsoft Entra ID: users, managers, role groups, agents"
    ).add_subparsers(dest="action", required=True)
    ei = en.add_parser("import", help="Load every enabled user, role group and agent")
    entra_flags(ei)
    ei.add_argument(
        "--agent-tag", default="AIAgent", help="Service principals with this tag are agents ('' for none)"
    )
    load_flags(ei)
    ei.set_defaults(fn=entra_import)
    ec = en.add_parser("changes", help="Apply user changes since the last run (delta query)")
    entra_flags(ec)
    ec.add_argument("--state", default="entra-delta.json", help="Where the delta position is kept")
    ec.add_argument("--source")
    ec.add_argument("--dry-run", action="store_true")
    ec.set_defaults(fn=entra_changes)

    def erp_flags(sp):
        sp.add_argument("--url", default=os.environ.get("ORACLE_URL"), help="Oracle Fusion Cloud host")

    er = sub.add_parser(
        "erp", help="Oracle Fusion Cloud ERP: who can act, approval exceptions"
    ).add_subparsers(dest="action", required=True)
    eri = er.add_parser("import", help="Load every active ERP account linked to a person")
    erp_flags(eri)
    eri.add_argument("--role-map", help="YAML object: Oracle role code -> role ID (roles filled in the ERP)")
    load_flags(eri)
    eri.set_defaults(fn=erp_import)
    erx = er.add_parser("exceptions", help="Report approval exceptions since a date as findings")
    erp_flags(erx)
    erx.add_argument("--since", required=True, help="YYYY-MM-DD: purchase orders and approvals on or after")
    erx.add_argument("--source")
    erx.add_argument("--dry-run", action="store_true")
    erx.set_defaults(fn=erp_exceptions)

    sn = sub.add_parser(
        "servicenow",
        help="ServiceNow CMDB and Enterprise Architecture: business applications, capabilities and their owners",
    ).add_subparsers(dest="action", required=True)
    sni = sn.add_parser(
        "import", help="Load business applications, their owner roles, and ServiceNow's people"
    )
    sni.add_argument(
        "--url", default=os.environ.get("SERVICENOW_URL"), help="https://<instance>.service-now.com"
    )
    sni.add_argument(
        "--users-source",
        help="source for the active users (default <source>-users; register it with grantsAccess)",
    )
    load_flags(sni)
    sni.set_defaults(fn=servicenow_import)
    sne = sn.add_parser(
        "ea",
        help="Load the business capability map and its owner roles from ServiceNow Enterprise Architecture "
        "(register the source with category ea)",
    )
    sne.add_argument(
        "--url", default=os.environ.get("SERVICENOW_URL"), help="https://<instance>.service-now.com"
    )
    sne.add_argument(
        "--id-field",
        help="capability field to use as its ID (e.g. number); default: the name, as AccountsPayable",
    )
    sne.add_argument(
        "--parent-field", default="parent", help="reference field to the parent capability (default parent)"
    )
    load_flags(sne)
    sne.set_defaults(fn=servicenow_ea)

    lx = sub.add_parser(
        "leanix", help="SAP LeanIX: the business capability map and its owners"
    ).add_subparsers(dest="action", required=True)
    lxi = lx.add_parser(
        "import",
        help="Load business capabilities and their owner roles (register the source with category ea)",
    )
    lxi.add_argument("--url", default=os.environ.get("LEANIX_URL"), help="https://<workspace>.leanix.net")
    lxi.add_argument(
        "--id-from",
        choices=("name", "externalId"),
        default="name",
        help="capability IDs from the name (AccountsPayable, default) or the fact sheet's external ID",
    )
    lxi.add_argument(
        "--owner-role",
        default="Business Owner",
        help="subscription role that names the owner (default Business Owner)",
    )
    load_flags(lxi)
    lxi.set_defaults(fn=leanix_import)

    ha = sub.add_parser("halo", help="HaloITSM: business applications and agents").add_subparsers(
        dest="action", required=True
    )
    hai = ha.add_parser("import", help="Load business applications and their owners, and the enabled agents")
    hai.add_argument("--url", default=os.environ.get("HALO_URL"), help="https://<instance>.haloitsm.com")
    hai.add_argument(
        "--agents-source",
        help="source for the agents (default <source>-agents; register it with grantsAccess)",
    )
    load_flags(hai)
    hai.set_defaults(fn=halo_import)

    sub.add_parser("sources", help="Sources loaded into the model").set_defaults(fn=sources)
    r = sub.add_parser("runs", help="Recent connector runs (changesets), newest first")
    r.add_argument("--source")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(fn=runs)
    c = sub.add_parser("cleanup", help="Discard changesets left open by crashed runs")
    c.add_argument("--older-than-hours", type=float, default=24)
    c.set_defaults(fn=cleanup)
    return p


def main(
    argv: list[str] | None = None, http: httpx.Client | None = None, source_http: httpx.Client | None = None
) -> int:
    args = parser().parse_args(argv)
    args.source_http = source_http  # tests: a source system served in-process
    try:
        return args.fn(session_from(args, http), args)
    except ApiError as exc:
        _out(f"Error: {exc.detail}")
        return 2
    except SourceError as exc:
        _out(f"Error: {exc}")
        return 2
    except (ValueError, OSError, KeyError) as exc:
        _out(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
