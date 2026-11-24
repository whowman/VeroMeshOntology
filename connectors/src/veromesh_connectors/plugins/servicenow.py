"""ServiceNow CMDB: business applications, their owners, and the people in ServiceNow, through the Table API.

What it reads (``/api/now/table/{table}``, basic authentication or an OAuth token, paged with
``sysparm_limit`` and ``sysparm_offset``):

* ``cmdb_ci_business_app``: each business application (Application Portfolio Management,
  now ServiceNow Enterprise Architecture): name, number, ``owned_by`` (business owner),
  ``managed_by`` (technical owner), ``business_criticality``, ``install_status``;
* ``sys_user``: the people, with ``employee_number`` (synced from HR), ``user_name``, name and
  email; every active user, and every user named as an application's owner even if inactive.

What it writes, as source ``servicenow`` (category ``cmdb``):

* **systems**: ID = the application's number (e.g. APM0001000), the CMDB ID (its sys_id),
  name, criticality (1 critical, 2 high, 3 medium, 4 low), lifecycle (installed: active;
  retired: retired), and its business and technical owner **roles**;
* **roles**: one per owner of each application ("Business owner of Epic EHR"), because
  ownership in VeroMesh sits with a role, and the CMDB names a person;
* **people**: matched to the people VeroMesh holds: the employee number when it is an
  employee ID VeroMesh knows, else the user name (a contractor, as Entra ID names them), else
  the employee number or user name. The roles they fill are the owner roles above. No employee
  ID: ServiceNow copies it from HR, it is not the record of employment.

It also reports, as findings for source ``servicenow``:

* ``duplicate-system`` (medium): two applications with the same owners, vendor, kind and
  support group, under different names, the same system entered twice.

An owner who has left shows as ``owner-has-left`` in VeroMesh's own findings: their HR record
is gone while the CMDB still names them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from ..core import BatchConnector, Item, MappingError, Record
from ..core.source import SourceApi
from .oracle_erp import local_id

TABLE = "/api/now/table"
PAGE = 1000
CRITICALITY = {"1": "critical", "2": "high", "3": "medium", "4": "low"}
RETIRED = {"7", "8"}  # retired, stolen/absent


class BearerToken:
    """A ServiceNow OAuth access token obtained elsewhere."""

    refreshable = False

    def __init__(self, token: str) -> None:
        self._t = token

    def token(self, force: bool = False) -> str:
        return self._t


class ServiceNowClient:
    def __init__(self, api: SourceApi) -> None:
        self.api = api

    def rows(self, table: str, query: str = "", fields: str = "") -> Iterator[dict]:
        offset = 0
        while True:
            params = {
                "sysparm_limit": str(PAGE),
                "sysparm_offset": str(offset),
                "sysparm_exclude_reference_link": "true",
            }
            if query:
                params["sysparm_query"] = query
            if fields:
                params["sysparm_fields"] = fields
            got = self.api.get(f"{TABLE}/{table}", params).get("result", [])
            yield from got
            if len(got) < PAGE:
                return
            offset += len(got)


def _ref(v) -> str:
    """A reference field: {"value": sys_id, "link": ...}, or the sys_id itself."""
    if isinstance(v, dict):
        return str(v.get("value") or "")
    return str(v or "")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:120]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ServiceNowCmdbConnector(BatchConnector):
    name = "servicenow"
    kind = "connector"

    def __init__(self, client: ServiceNowClient, *, people: list[dict] | None = None) -> None:
        super().__init__()
        self.client = client
        people = people or []
        self.by_employee = {str(p["employeeId"]): p for p in people if p.get("employeeId")}
        self.by_id = {str(p["id"]): p for p in people}
        self.apps: list[dict] = []
        self.unowned: list[str] = []

    def describe(self) -> str:
        return f"ServiceNow CMDB: {len(self.apps)} business applications"

    def fingerprint(self) -> str | None:
        return "servicenow-cmdb"

    def _pid(self, u: dict) -> str:
        num, local = str(u.get("employee_number") or "").strip(), local_id(str(u.get("user_name") or ""))
        known = self.by_employee.get(num) or (self.by_id.get(local) if local else None)
        return known["id"] if known else (num or local)

    def read(self, schema: list[dict]) -> Iterable[Record]:
        self.apps = list(self.client.rows("cmdb_ci_business_app"))
        owners: dict[str, list[str]] = {}  # sys_user sys_id -> owner role IDs
        for a in self.apps:
            num = str(a.get("number") or a.get("sys_id"))
            for field, kind in (("owned_by", "BusinessOwner"), ("managed_by", "TechnicalOwner")):
                if uid := _ref(a.get(field)):
                    owners.setdefault(uid, []).append(f"{_slug(num)}.{kind}")
            yield Record(ref=f"ServiceNow business application {num}", data=a, type="systems")
        users = {u["sys_id"]: u for u in self.client.rows("sys_user", "active=true")}
        missing = [uid for uid in owners if uid not in users]
        for uid in missing:  # an owner who is no longer active: the CMDB still names them
            try:
                users[uid] = self.client.api.get(
                    f"{TABLE}/sys_user/{uid}", {"sysparm_exclude_reference_link": "true"}
                )["result"]
            except Exception:  # noqa: BLE001 — a dangling reference: reported, not fatal
                self.problems.append(f"ServiceNow user {uid}, named as an owner, could not be read")
        seen: dict[str, str] = {}
        for uid, u in users.items():
            pid = self._pid(u)
            if not pid:
                continue
            if pid in seen:
                self.problems.append(f"ServiceNow users {seen[pid]} and {u.get('user_name')} are both {pid}")
                continue
            seen[pid] = u.get("user_name") or uid
            yield Record(
                ref=f"ServiceNow user {u.get('user_name')}",
                data={"user": u, "roles": owners.get(uid, [])},
                type="people",
            )

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if record.type == "systems":
            a = record.data
            num = _slug(str(a.get("number") or a.get("sys_id")))
            name = a.get("name") or num
            e: dict = {"id": num, "name": name, "cmdbId": a.get("sys_id")}
            crit = str(a.get("business_criticality") or "")[:1]
            if crit in CRITICALITY:
                e["criticality"] = CRITICALITY[crit]
            e["lifecycle"] = "retired" if str(a.get("install_status")) in RETIRED else "active"
            items = []
            for field, kind, key, label in (
                ("owned_by", "BusinessOwner", "businessOwner", "Business owner"),
                ("managed_by", "TechnicalOwner", "technicalOwner", "Technical owner"),
            ):
                if _ref(a.get(field)):
                    rid = f"{num}.{kind}"
                    e[key] = rid
                    items.append(
                        Item(
                            type="roles",
                            entity={"id": rid, "name": f"{label} of {name}"},
                            ref=record.ref,
                            observed_at=_now(),
                        )
                    )
            return [*items, Item(type="systems", entity=e, ref=record.ref, observed_at=_now())]
        if record.type == "people":
            u, roles = record.data["user"], record.data["roles"]
            pid = self._pid(u)
            known = self.by_id.get(pid) or {}
            e = {"id": pid, "name": u.get("name") or known.get("name") or pid}
            if u.get("email"):
                e["email"] = u["email"]
            if roles:
                e["rolesFilled"] = sorted(set(roles)) if len(set(roles)) > 1 else roles[0]
            return [Item(type="people", entity=e, ref=record.ref, observed_at=_now())]
        raise MappingError(f"unexpected record type {record.type}", record.ref)


def duplicates(apps: list[dict]) -> list[dict]:
    """Applications that look like one system entered twice: same owners, vendor, kind and
    support group under different names. One finding per group, on its first record."""
    groups: dict[tuple, list[dict]] = {}
    for a in apps:
        key = tuple(_ref(a.get(f)) for f in ("owned_by", "managed_by", "support_group")) + (
            str(a.get("vendor") or ""),
            str(a.get("u_kind") or a.get("sys_class_name") or ""),
        )
        if any(key[:2]):
            groups.setdefault(key, []).append(a)
    out = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: str(r.get("number")))
        nums = [_slug(str(r.get("number"))) for r in rows]
        names = [r.get("name") for r in rows]
        out.append(
            {
                "kind": "duplicate-system",
                "severity": "medium",
                "detail": f"{' and '.join(f'{n} ({x})' for n, x in zip(names, nums, strict=True))} have the same owners, "
                "vendor, kind and support group: probably one system entered twice in the CMDB.",
                "subject": {"type": "systems", "id": nums[0]},
                "about": {"records": nums, "names": names},
            }
        )
    return out
