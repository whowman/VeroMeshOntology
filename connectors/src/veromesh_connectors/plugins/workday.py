"""Workday HCM: workers, their positions and their managers, from Workday's public APIs.

Two ways to read the full workforce (choose with ``via``):

* ``raas`` (default): a custom report exposed as Reports as a Service,
  ``/ccx/service/customreport2/{tenant}/{owner}/{report}?format=json``, the usual
  route for bulk HR feeds. The report's columns are listed in ``RAAS_COLUMNS``.
* ``rest``: the Workday REST API, ``/ccx/api/v1/{tenant}/workers`` paged with
  ``limit`` and ``offset``.

What changed since a date comes from a second report (default
``VeroMesh_Worker_Changes`` with ``Updated_From``): ``WorkdayChangesConnector``
turns each business process event into one VeroMesh event.

Sign-in: an API client registered in Workday, with a refresh token issued to the
integration system user (or client credentials), at ``/ccx/oauth2/{tenant}/token``.

What it writes:

* **people**: ID = Workday employee ID; name (the preferred name); email; the role
  they fill; whom they report to. Workers whose termination has taken effect
  are left out, so a ``replace`` run removes them.
* **roles**: one per business title (or per ``role_map`` entry), unless
  ``roles=False``.

A manager reference to someone not in the run (a contractor, a leaver) is left
out and noted, rather than failing the run on a dangling reference.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime

from ..core import BatchConnector, Event, EventConnector, Item, MappingError, Record
from ..core.source import SourceApi

#: Columns the workers report must have (Workday report field aliases).
RAAS_COLUMNS = (
    "Employee_ID",
    "Preferred_Name",
    "Email_Primary_Work",
    "Business_Title",
    "Manager_Employee_ID",
    "Hire_Date",
    "Termination_Date",
    "Active_Status",
)
PAGE = 100  # the REST API's largest page


def role_id(title: str) -> str:
    """'Accounts Payable Manager' -> 'accounts-payable-manager' (a valid model ID)."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:128]


class WorkdayClient:
    """Workday's REST API and Reports as a Service, for one tenant."""

    def __init__(self, api: SourceApi, tenant: str) -> None:
        self.api = api
        self.tenant = tenant

    def report(self, owner: str, report: str, **params: str) -> list[dict]:
        body = self.api.get(
            f"/ccx/service/customreport2/{self.tenant}/{owner}/{report}", {"format": "json", **params}
        )
        return list(body.get("Report_Entry", []))

    def workers(self) -> Iterator[dict]:
        offset = 0
        while True:
            body = self.api.get(f"/ccx/api/v1/{self.tenant}/workers", {"limit": PAGE, "offset": offset})
            data = body.get("data", [])
            yield from data
            offset += len(data)
            if not data or offset >= int(body.get("total", 0)):
                return

    def worker(self, ident: str) -> dict:
        return self.api.get(f"/ccx/api/v1/{self.tenant}/workers/{ident}")


# ---------------------------------------------------------------- one shape for both APIs


def from_report(row: dict) -> dict:
    return {
        "employeeId": row.get("Employee_ID", ""),
        "wid": row.get("Worker_WID", ""),
        "name": row.get("Preferred_Name") or row.get("Legal_Name", ""),
        "email": row.get("Email_Primary_Work", ""),
        "title": row.get("Business_Title", ""),
        "managerEmployeeId": row.get("Manager_Employee_ID", ""),
        "managerWid": "",
        "hireDate": row.get("Hire_Date", ""),
        "terminationDate": row.get("Termination_Date", ""),
        "active": str(row.get("Active_Status", "1")) in ("1", "true", "True"),
    }


def from_rest(w: dict) -> dict:
    preferred = w.get("preferredName") or {}
    manager = w.get("manager") or {}
    name = " ".join(x for x in (preferred.get("firstName"), preferred.get("lastName")) if x) or w.get(
        "descriptor", ""
    )
    return {
        "employeeId": w.get("workerId", ""),
        "wid": w.get("id", ""),
        "name": name,
        "email": w.get("primaryWorkEmail") or "",
        "title": w.get("businessTitle") or "",
        "managerEmployeeId": manager.get("workerId", ""),
        "managerWid": manager.get("id", ""),
        "hireDate": w.get("hireDate") or "",
        "terminationDate": w.get("terminationDate") or "",
        "active": bool(w.get("active", True)),
    }


class _WorkerMapping:
    """``map`` shared by the batch and the change-event connectors."""

    roles: bool
    role_map: dict[str, str]
    today: date

    def gone(self, w: dict) -> bool:
        end = w.get("terminationDate")
        return not w["active"] or bool(end and date.fromisoformat(end[:10]) <= self.today)

    def role_of(self, title: str) -> str | None:
        if not self.roles or not title:
            return None
        return self.role_map.get(title) or role_id(title)

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        w = record.data
        emp = w["employeeId"]
        if not emp:
            raise MappingError("worker has no employee ID", record.ref)
        person = {"id": emp, "name": w["name"] or emp, "employeeId": emp}
        if w["email"]:
            person["email"] = w["email"]
        if w["managerEmployeeId"]:
            person["reportsTo"] = w["managerEmployeeId"]
        items = []
        role = self.role_of(w["title"])
        if role:
            person["rolesFilled"] = role
            items.append(Item(type="roles", entity={"id": role, "name": w["title"]}, ref=record.ref))
        end = w.get("terminationDate")
        items.insert(
            0,
            Item(
                type="people",
                entity=person,
                ref=record.ref,
                observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                valid_from=(w.get("hireDate") or "")[:10] or None,
                valid_until=end[:10] if end else None,
            ),
        )
        return items


class WorkdayConnector(_WorkerMapping, BatchConnector):
    name = "workday"
    kind = "connector"

    def __init__(
        self,
        client: WorkdayClient,
        *,
        via: str = "raas",
        report_owner: str = "ISU_VeroMesh",
        report: str = "VeroMesh_Workers",
        roles: bool = True,
        role_map: dict[str, str] | None = None,
        today: date | None = None,
    ) -> None:
        super().__init__()
        if via not in ("raas", "rest"):
            raise ValueError("via is 'raas' or 'rest'")
        self.client, self.via = client, via
        self.report_owner, self.report = report_owner, report
        self.roles, self.role_map = roles, dict(role_map or {})
        self.today = today or date.today()
        self.left_out = 0
        self.dropped_managers: list[str] = []

    def describe(self) -> str:
        where = (
            f"Workday report {self.report_owner}/{self.report}"
            if self.via == "raas"
            else f"the Workday REST API ({self.client.tenant})"
        )
        notes = []
        if self.left_out:
            notes.append(f"{self.left_out} terminated workers left out")
        if self.dropped_managers:
            shown = ", ".join(self.dropped_managers[:5]) + (" …" if len(self.dropped_managers) > 5 else "")
            notes.append(
                f"{len(self.dropped_managers)} manager references to people not in this run dropped ({shown})"
            )
        return where + (f"; {'; '.join(notes)}" if notes else "")

    def fingerprint(self) -> str | None:
        return f"workday:{self.client.tenant}:{self.via}"

    def _workers(self) -> list[dict]:
        if self.via == "raas":
            rows = self.client.report(self.report_owner, self.report)
            if rows:
                missing = [c for c in RAAS_COLUMNS if c not in rows[0]]
                if missing:
                    self.problems.append(f"report {self.report} has no column {', '.join(missing)}")
                    return []
            return [from_report(r) for r in rows]
        return [from_rest(w) for w in self.client.workers()]

    def read(self, schema: list[dict]) -> Iterable[Record]:
        workers = self._workers()
        by_wid = {w["wid"]: w["employeeId"] for w in workers if w["wid"]}
        current = []
        for w in workers:
            if self.gone(w):
                self.left_out += 1
                continue
            if not w["managerEmployeeId"] and w["managerWid"]:
                w["managerEmployeeId"] = by_wid.get(w["managerWid"], "")
            current.append(w)
        here = {w["employeeId"] for w in current}
        for w in current:
            m = w["managerEmployeeId"]
            if m and m not in here:
                self.dropped_managers.append(f"{w['employeeId']}→{m}")
                w["managerEmployeeId"] = ""
            yield Record(ref=f"Workday worker {w['employeeId']}", data=w, type="people")


class WorkdayChangesConnector(_WorkerMapping, EventConnector):
    """One VeroMesh event per Workday business process event since ``since``."""

    name = "workday"
    kind = "connector"

    def __init__(
        self,
        client: WorkdayClient,
        since: date,
        *,
        report_owner: str = "ISU_VeroMesh",
        report: str = "VeroMesh_Worker_Changes",
        through: date | None = None,
        roles: bool = True,
        role_map: dict[str, str] | None = None,
        today: date | None = None,
    ) -> None:
        super().__init__()
        self.client, self.since, self.through = client, since, through
        self.report_owner, self.report = report_owner, report
        self.roles, self.role_map = roles, dict(role_map or {})
        self.today = today or date.today()
        self.last_entered: str | None = None

    def describe(self) -> str:
        return f"Workday changes since {self.since.isoformat()}"

    def events(self, schema: list[dict]) -> Iterable[Event]:
        params = {"Updated_From": self.since.isoformat()}
        if self.through:
            params["Updated_Through"] = self.through.isoformat()
        rows = self.client.report(self.report_owner, self.report, **params)
        for row in rows:
            emp = row.get("Employee_ID", "")
            process = row.get("Business_Process", "")
            eid = re.sub(
                r"[^A-Za-z0-9._-]+",
                "-",
                f"{emp}-{process}-{row.get('Effective_Date', '')}-{row.get('Entered_On', '')}",
            )
            self.last_entered = max(self.last_entered or "", row.get("Entered_On", ""))
            effective = row.get("Effective_Date", "")
            if process == "Termination" and effective and date.fromisoformat(effective[:10]) <= self.today:
                yield Event(id=eid, deletes=[("people", emp)])
                continue
            # Anything else, including a termination dated ahead (it becomes the person's valid-until date).
            w = from_rest(self.client.worker(emp))
            if not w["managerEmployeeId"] and w["managerWid"]:  # the REST API names the manager by WID
                w["managerEmployeeId"] = from_rest(self.client.worker(w["managerWid"]))["employeeId"]
            if self.gone(w):
                yield Event(id=eid, deletes=[("people", emp)])
                continue
            yield Event(
                id=eid, records=[Record(ref=f"Workday worker {emp} ({process})", data=w, type="people")]
            )
