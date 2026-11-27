"""ServiceNow CMDB: business applications, their owners, and the people in ServiceNow, through the Table API.

What it reads (``/api/now/table/{table}``, basic authentication or an OAuth token, paged with
``sysparm_limit`` and ``sysparm_offset``):

* ``cmdb_ci_business_app``: each business application (Application Portfolio Management,
  now ServiceNow Enterprise Architecture): name, number, ``owned_by`` (business owner),
  ``managed_by`` (technical owner), ``business_criticality``, ``install_status``;
* ``sys_user``: the people, with ``employee_number`` (synced from HR), ``user_name``, name and
  email; every active user, and every user named as an application's owner even if inactive.

What it writes, in two sources (``import`` loads both):

* ``servicenow`` (category ``cmdb``): the applications, their owner roles, and the people they
  name as owners (active or not);
* ``servicenow-users`` (category ``cmdb``, registered with ``grantsAccess``): every active user,
  because an active ServiceNow user can still sign in and act there. A leaver HR no longer lists
  shows as access without employment until the weekly sync deactivates them.

In detail:

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

from ..core import BatchConnector, Item, MappingError, Record, source_time
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

    def __init__(
        self, client: ServiceNowClient, *, people: list[dict] | None = None, part: str = "cmdb"
    ) -> None:
        super().__init__()
        if part not in ("cmdb", "users"):
            raise ValueError("part is cmdb or users")
        self.part = part
        self.client = client
        people = people or []
        self.by_employee = {str(p["employeeId"]): p for p in people if p.get("employeeId")}
        self.by_id = {str(p["id"]): p for p in people}
        self.apps: list[dict] = []
        self.unowned: list[str] = []

    def describe(self) -> str:
        return f"ServiceNow CMDB: {len(self.apps)} business applications"

    def fingerprint(self) -> str | None:
        return f"servicenow-{self.part}"

    def _pid(self, u: dict) -> str:
        num, local = str(u.get("employee_number") or "").strip(), local_id(str(u.get("user_name") or ""))
        known = self.by_employee.get(num) or (self.by_id.get(local) if local else None)
        return known["id"] if known else (num or local)

    def read(self, schema: list[dict]) -> Iterable[Record]:
        if self.part == "users":
            seen: dict[str, str] = {}
            for u in self.client.rows("sys_user", "active=true"):
                pid = self._pid(u)
                if pid and pid not in seen:
                    seen[pid] = u.get("user_name") or u["sys_id"]
                    yield Record(
                        ref=f"ServiceNow user {u.get('user_name')}",
                        data={"user": u, "roles": []},
                        type="people",
                    )
            return
        self.apps = list(self.client.rows("cmdb_ci_business_app"))
        owners: dict[str, list[str]] = {}  # sys_user sys_id -> owner role IDs
        for a in self.apps:
            num = str(a.get("number") or a.get("sys_id"))
            for field, kind in (("owned_by", "BusinessOwner"), ("managed_by", "TechnicalOwner")):
                if uid := _ref(a.get(field)):
                    owners.setdefault(uid, []).append(f"{_slug(num)}.{kind}")
            yield Record(ref=f"ServiceNow business application {num}", data=a, type="systems")
        users: dict[str, dict] = {}
        for uid in owners:  # the people the CMDB names as owners, active or not
            if uid in users:
                continue
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
                            observed_at=source_time(a.get("sys_updated_on")),
                        )
                    )
            return [
                *items,
                Item(
                    type="systems", entity=e, ref=record.ref, observed_at=source_time(a.get("sys_updated_on"))
                ),
            ]
        if record.type == "people":
            u, roles = record.data["user"], record.data["roles"]
            pid = self._pid(u)
            known = self.by_id.get(pid) or {}
            e = {"id": pid, "name": u.get("name") or known.get("name") or pid}
            if u.get("email"):
                e["email"] = u["email"]
            if roles:
                e["rolesFilled"] = sorted(set(roles)) if len(set(roles)) > 1 else roles[0]
            return [
                Item(
                    type="people", entity=e, ref=record.ref, observed_at=source_time(u.get("sys_updated_on"))
                )
            ]
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


# ---------------------------------------------------------------------------- Enterprise Architecture

CAPABILITY_TABLE = "cmdb_ci_business_capability"
APP_TABLE = "cmdb_ci_business_app"
REL_TABLE = "cmdb_rel_ci"
PROVIDES = "Provided by::Provides"  # CSDM: a business capability is provided by its applications


class SystemMatcher:
    """Finds the system VeroMesh holds for an application an EA tool names: by its ID or CMDB
    ID (a ServiceNow number or sys_id, or an external ID set to one), else by name."""

    def __init__(self, systems: list[dict] | None) -> None:
        self.known = systems is not None
        self.by_key: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        for s in systems or []:
            for k in (s.get("id"), s.get("cmdbId")):
                if k:
                    self.by_key[str(k)] = s["id"]
            if s.get("name"):
                self.by_name.setdefault(str(s["name"]).strip().lower(), s["id"])

    def find(self, *keys: str | None, name: str | None = None) -> str | None:
        for k in keys:
            if k and str(k) in self.by_key:
                return self.by_key[str(k)]
        return self.by_name.get(str(name or "").strip().lower())


def pascal(name: str) -> str:
    """'Accounts payable' -> 'AccountsPayable': the ID style of the workbook, so one capability
    loaded from both is one entity."""
    words = re.findall(r"[A-Za-z0-9]+", name)
    return "".join(w[:1].upper() + w[1:] for w in words)[:120]


class ServiceNowEaConnector(BatchConnector):
    """ServiceNow Enterprise Architecture: the business capability map and who owns each capability.

    Reads ``cmdb_ci_business_capability`` through the Table API: name, ``short_description``,
    the parent capability (``parent``, or another reference field), ``owned_by`` and
    ``install_status``. Writes, as one source (category ``ea``, which decides the capabilities
    domain by default):

    * **capabilities**: ID from the name in the workbook's style ("Accounts payable" ->
      AccountsPayable), or from a field (``id_field``, e.g. ``number``); name, definition, the
      parent capability, and the owner **role**. Retired capabilities are left out.
    * **roles**: "Owner of <capability>", one per owned capability: ownership sits with a role.
    * **people**: each owner, filling their owner roles, matched to the people VeroMesh holds as
      the CMDB connector matches them.

    * **supported by systems**: the applications each capability is provided by (CSDM's
      ``Provided by::Provides`` relationship in ``cmdb_rel_ci``, the capability as parent and the
      business application as child), as the systems VeroMesh holds: the application's number
      (the CMDB connector's system ID) or its sys_id (their CMDB ID). An application VeroMesh
      does not hold yet is left out and reported: load the CMDB first.

    Two capabilities that would get the same ID are a source problem: nothing is sent until one
    is renamed or IDs come from a unique field. A capability whose parent is retired or missing
    is loaded at the top level.
    """

    name = "servicenow-ea"
    kind = "connector"

    def __init__(
        self,
        client: ServiceNowClient,
        *,
        people: list[dict] | None = None,
        id_field: str | None = None,
        parent_field: str = "parent",
        systems: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.id_field = id_field
        self.parent_field = parent_field
        self.systems = SystemMatcher(systems)
        self.support: dict[str, list[str]] = {}  # capability sys_id -> system IDs
        self.unlinked: list[str] = []
        people = people or []
        self.by_employee = {str(p["employeeId"]): p for p in people if p.get("employeeId")}
        self.by_id = {str(p["id"]): p for p in people}
        self.caps: list[dict] = []
        self.ids: dict[str, str] = {}  # sys_id -> capability ID

    _pid = ServiceNowCmdbConnector._pid

    def describe(self) -> str:
        links = sum(len(v) for v in self.support.values())
        note = (
            f"; {len(self.unlinked)} supporting applications VeroMesh does not hold yet "
            f"({', '.join(sorted(set(self.unlinked))[:5])}): load the CMDB first"
            if self.unlinked
            else ""
        )
        return f"ServiceNow EA: {len(self.caps)} business capabilities, {links} links to supporting systems{note}"

    def fingerprint(self) -> str | None:
        return "servicenow-ea"

    def _assign_ids(self) -> None:
        taken: dict[str, str] = {}
        for c in self.caps:
            raw = str(c.get(self.id_field) or "") if self.id_field else pascal(str(c.get("name") or ""))
            cid = _slug(raw) or c["sys_id"]
            if cid in taken:
                self.problems.append(
                    f"ServiceNow capabilities {taken[cid]} and {c['sys_id']} would both be {cid}: rename one, "
                    "or take IDs from a field that is unique (--id-field number)"
                )
                cid = c["sys_id"]
            taken[cid] = c["sys_id"]
            self.ids[c["sys_id"]] = cid

    def read(self, schema: list[dict]) -> Iterable[Record]:
        self.caps = [
            c for c in self.client.rows(CAPABILITY_TABLE) if str(c.get("install_status")) not in RETIRED
        ]
        self._assign_ids()
        self._read_support()
        owners: dict[str, list[str]] = {}
        for c in self.caps:
            if uid := _ref(c.get("owned_by")):
                owners.setdefault(uid, []).append(f"{self.ids[c['sys_id']]}.Owner")
            yield Record(ref=f"ServiceNow business capability {c.get('name')}", data=c, type="capabilities")
        seen: dict[str, str] = {}
        for uid, roles in owners.items():
            try:
                u = self.client.api.get(
                    f"{TABLE}/sys_user/{uid}", {"sysparm_exclude_reference_link": "true"}
                )["result"]
            except Exception:  # noqa: BLE001 — a dangling reference: reported, not fatal
                self.problems.append(f"ServiceNow user {uid}, named as a capability owner, could not be read")
                continue
            pid = self._pid(u)
            if not pid:
                continue
            if pid in seen:
                self.problems.append(f"ServiceNow users {seen[pid]} and {u.get('user_name')} are both {pid}")
                continue
            seen[pid] = u.get("user_name") or uid
            yield Record(
                ref=f"ServiceNow user {u.get('user_name')}", data={"user": u, "roles": roles}, type="people"
            )

    def _read_support(self) -> None:
        """Which applications provide each capability (CSDM: Provided by::Provides)."""
        apps = {
            a["sys_id"]: a for a in self.client.rows(APP_TABLE, fields="sys_id,number,name,install_status")
        }
        for rel in self.client.rows(REL_TABLE, query=f"type.name={PROVIDES}", fields="parent,child,type"):
            cap, app = _ref(rel.get("parent")), _ref(rel.get("child"))
            if cap not in self.ids or app not in apps:
                continue  # a retired capability, or a relationship between other kinds of CI
            a = apps[app]
            sid = self.systems.find(a.get("number"), app) if self.systems.known else a.get("number") or app
            if sid is None:
                self.unlinked.append(str(a.get("name") or a.get("number") or app))
                continue
            self.support.setdefault(cap, [])
            if sid not in self.support[cap]:
                self.support[cap].append(sid)

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if record.type == "capabilities":
            c = record.data
            cid = self.ids[c["sys_id"]]
            name = c.get("name") or cid
            e: dict = {"id": cid, "name": name}
            if c.get("short_description"):
                e["definition"] = c["short_description"]
            parent = _ref(c.get(self.parent_field))
            if parent in self.ids:  # a retired or missing parent: shown at the top level
                e["parentCapability"] = self.ids[parent]
            if sup := sorted(self.support.get(c["sys_id"], [])):
                e["supportedBySystems"] = sup
            items = []
            if _ref(c.get("owned_by")):
                e["ownerRole"] = f"{cid}.Owner"
                items.append(
                    Item(
                        type="roles",
                        entity={"id": e["ownerRole"], "name": f"Owner of {name}"},
                        ref=record.ref,
                        observed_at=source_time(c.get("sys_updated_on")),
                    )
                )
            return [
                *items,
                Item(
                    type="capabilities",
                    entity=e,
                    ref=record.ref,
                    observed_at=source_time(c.get("sys_updated_on")),
                ),
            ]
        if record.type == "people":
            u, roles = record.data["user"], sorted(set(record.data["roles"]))
            pid = self._pid(u)
            known = self.by_id.get(pid) or {}
            e = {"id": pid, "name": u.get("name") or known.get("name") or pid}
            if u.get("email"):
                e["email"] = u["email"]
            e["rolesFilled"] = roles if len(roles) > 1 else roles[0]
            return [
                Item(
                    type="people", entity=e, ref=record.ref, observed_at=source_time(u.get("sys_updated_on"))
                )
            ]
        raise MappingError(f"unexpected record type {record.type}", record.ref)
