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
        # Optional columns: placement (for an authority's scope) and role-based security
        # groups the worker is assigned (HR Partner, Compensation Partner...), ';'-separated.
        "department": row.get("Supervisory_Organization", ""),
        "costCentre": row.get("Cost_Center", ""),
        "orgRoles": [g.strip() for g in str(row.get("Organization_Roles") or "").split(";") if g.strip()],
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
        "department": (w.get("primarySupervisoryOrganization") or {}).get("descriptor", ""),
        "costCentre": "",
        "orgRoles": [],
    }


class _WorkerMapping:
    """``map`` shared by the batch and the change-event connectors."""

    roles: bool
    role_map: dict[str, str]
    today: date
    manager_role: str | None = None
    managers: set[str] = set()

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
        for key in ("department", "costCentre"):
            if w.get(key):
                person[key] = w[key]
        items = []
        filled = []
        role = self.role_of(w["title"])
        if role:
            filled.append(role)
            items.append(Item(type="roles", entity={"id": role, "name": w["title"]}, ref=record.ref))
        # Role-based groups (an HR Partner), and the manager group for anyone with reports:
        # the groups Workday's business processes name as initiators and approvers.
        groups = list(w.get("orgRoles") or [])
        if self.manager_role and emp in self.managers:
            groups.append(self.manager_role)
        for g in groups:
            gid = self.role_map.get(g) or role_id(g)
            if gid and gid not in filled:
                filled.append(gid)
                items.append(Item(type="roles", entity={"id": gid, "name": g}, ref=record.ref))
        if filled:
            person["rolesFilled"] = filled[0] if len(filled) == 1 else filled
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
        manager_role: str | None = None,
    ) -> None:
        super().__init__()
        if via not in ("raas", "rest"):
            raise ValueError("via is 'raas' or 'rest'")
        self.client, self.via = client, via
        self.report_owner, self.report = report_owner, report
        self.roles, self.role_map = roles, dict(role_map or {})
        self.manager_role, self.managers = manager_role, set()
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
        self.managers = {w["managerEmployeeId"] for w in current if w["managerEmployeeId"] in here}
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


# ---------------------------------------------------------------- business processes


#: Workday business process types to VeroMesh actions (override with ``process_map``).
PROCESS_ACTIONS = {
    "Request Compensation Change": "AdjustPay",
    "Promote Employee Inbound": "PromoteEmployee",
    "Hire": "HireEmployee",
    "Termination": "TerminateEmployee",
}
#: A rule's measure (the condition field) to a VeroMesh limit kind.
MEASURES = {
    "Percent_Change": "percent",
    "Grade_Steps": "grade-steps",
    "Headcount": "headcount",
    "Amount": "amount",
}
#: The organization a business process runs over, to an authority's scope.
SCOPES = {
    "Supervisory Organization": "reporting-line",
    "Supervisory Organization and Subordinates": "reporting-line",
    "Organization": "department",
    "Cost Center": "cost-centre",
    "Company": "any",
    "": "any",
}
#: Approver groups Workday names by relation to the worker, not by assignment.
RELATIONS = {"Manager": "manager", "Manager's Manager": "manager-of-manager"}
APPROVAL_COLUMNS = ("Business_Process_Type", "Rule_Type", "Security_Group")


class WorkdayApprovalsConnector(BatchConnector):
    """The approval rules of Workday business processes, from a custom report over their
    definitions (RaaS, default ``VeroMesh_Approval_Rules``). One row per rule:

    * ``Rule_Type`` ``Initiator``: ``Security_Group`` may start the process, up to
      ``Condition_Above`` of ``Condition_Measure`` (Percent_Change, Grade_Steps, Headcount,
      Amount), over ``Organization_Scope``. Becomes an authority held by the group's role,
      ID ``{role}-{action}``.
    * ``Rule_Type`` ``Approval``: step ``Step_Order`` asks ``Security_Group`` (Manager,
      Manager's Manager, or an assigned group such as HR Partner), when the measure is above
      ``Condition_Above`` (empty: always). Becomes a co-approval on every initiator's
      authority for the process.

    Register the source with category ``hr``: for decisions about people the business
    process is the workflow in force (the authority-in-force domain)."""

    name = "workday-approvals"
    kind = "connector"

    def __init__(
        self,
        client: WorkdayClient,
        *,
        report_owner: str = "ISU_VeroMesh",
        report: str = "VeroMesh_Approval_Rules",
        process_map: dict[str, str] | None = None,
        role_map: dict[str, str] | None = None,
        policy: str | None = None,
        currency: str = "USD",
    ) -> None:
        super().__init__()
        self.client, self.report_owner, self.report = client, report_owner, report
        self.process_map = {**PROCESS_ACTIONS, **(process_map or {})}
        self.role_map = dict(role_map or {})
        self.policy, self.currency = policy, currency
        self.skipped: list[str] = []

    def describe(self) -> str:
        note = (
            f"; {len(self.skipped)} rules for processes not mapped ({', '.join(sorted(set(self.skipped))[:5])})"
            if self.skipped
            else ""
        )
        return f"Workday business process rules, report {self.report_owner}/{self.report}{note}"

    def fingerprint(self) -> str | None:
        return f"workday-approvals:{self.client.tenant}"

    def role(self, group: str) -> str:
        return self.role_map.get(group) or role_id(group)

    def read(self, schema: list[dict]) -> Iterable[Record]:
        rows = self.client.report(self.report_owner, self.report)
        if rows:
            missing = [c for c in APPROVAL_COLUMNS if c not in rows[0]]
            if missing:
                self.problems.append(f"report {self.report} has no column {', '.join(missing)}")
                return
        initiators: dict[str, list[str]] = {}
        approvals = []
        for i, row in enumerate(rows):
            bp = str(row.get("Business_Process_Type") or "").strip()
            action = self.process_map.get(bp)
            if not action:
                self.skipped.append(bp)
                continue
            ref = f"Workday rule {i + 1} ({bp})"
            if str(row.get("Rule_Type")).strip().lower() == "initiator":
                yield Record(ref=ref, data={"bp": bp, "action": action, **row}, type="authorities")
                initiators.setdefault(action, []).append(self.role(str(row["Security_Group"]).strip()))
            else:
                approvals.append((ref, bp, action, row))
        for ref, bp, action, row in approvals:
            for holder in initiators.get(action, []):
                yield Record(
                    ref=ref, data={"bp": bp, "action": action, "holder": holder, **row}, type="co-approvals"
                )

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        r = record.data
        group = str(r.get("Security_Group") or "").strip()
        if not group:
            raise MappingError("rule has no security group", record.ref)
        above = str(r.get("Condition_Above") or "").strip()
        kind = MEASURES.get(str(r.get("Condition_Measure") or "").strip(), None)
        if record.type == "authorities":
            role = self.role(group)
            auth = {
                "id": f"{role}-{r['action']}",
                "name": f"{group}: {r['bp'].lower()}" + (f" up to {above} {kind}" if above and kind else ""),
                "holder": role,
                "actionsCovered": [r["action"]],
                "scope": SCOPES.get(str(r.get("Organization_Scope") or "").strip(), "any"),
            }
            if above and kind == "amount":
                auth |= {"limitAmount": above, "currency": self.currency}
            elif above and kind:
                auth |= {"limitKind": kind, "limitValue": above}
            if self.policy:
                auth["delegatedBy"] = self.policy
            return [
                Item(type="roles", entity={"id": role, "name": group}, ref=record.ref),
                Item(type="actions", entity={"id": r["action"], "name": r["bp"]}, ref=record.ref),
                Item(type="authorities", entity=auth, ref=record.ref),
            ]
        co = {"authority": f"{r['holder']}-{r['action']}"}
        if group in RELATIONS:
            co["approverRelation"] = RELATIONS[group]
            items = []
        else:
            co["approverRole"] = self.role(group)
            items = [Item(type="roles", entity={"id": co["approverRole"], "name": group}, ref=record.ref)]
        if str(r.get("Step_Order") or "").strip():
            co["step"] = str(r["Step_Order"]).strip()
        if above:
            co["aboveAmount"] = above
        return [*items, Item(type="co-approvals", entity=co, ref=record.ref)]


# ---------------------------------------------------------------------------- goals

#: Columns the goals report must have; Goal_Level, Status and KPI_IDs are optional.
GOAL_COLUMNS = ("Goal_ID", "Goal_Name", "Owner_Employee_ID", "Owner_Business_Title", "Parent_Goal_ID")
CLOSED_GOALS = {"cancelled", "canceled", "archived"}
LEVELS = ("enterprise", "function", "capability", "process")


class WorkdayGoalsConnector(BatchConnector):
    """Organization goals from Workday Talent (Manage Goals), as objectives, from a custom
    report (RaaS, default ``VeroMesh_Goals``). One row per goal:

    * ``Goal_ID``, ``Goal_Name``, ``Description``;
    * ``Owner_Employee_ID`` and ``Owner_Business_Title``: the objective is owned by the
      owner's **role**, the one the workers connector gives that business title (or the
      ``role_map`` entry), because ownership in VeroMesh sits with a role;
    * ``Parent_Goal_ID``: the goal this one is aligned to (cascaded from), one level up;
    * ``Goal_Level`` (optional): enterprise, function, capability or process; without it, a
      goal with no parent is an enterprise objective and the rest function objectives;
    * ``Status`` (optional): cancelled and archived goals are left out;
    * ``KPI_IDs`` (optional, a calculated field): IDs of the KPIs that measure it, ``;``-separated.

    Register the source with category ``hr``: Workday decides the objectives domain by default,
    with the governance workbook as evidence (its priorities and conflicts still come from there).
    A parent goal that is not in the report is left out and noted."""

    name = "workday-goals"
    kind = "connector"

    def __init__(
        self,
        client: WorkdayClient,
        *,
        report_owner: str = "ISU_VeroMesh",
        report: str = "VeroMesh_Goals",
        role_map: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.client, self.report_owner, self.report = client, report_owner, report
        self.role_map = dict(role_map or {})
        self.ids: set[str] = set()
        self.orphans: list[str] = []

    def describe(self) -> str:
        note = f"; {len(self.orphans)} aligned to goals not in the report" if self.orphans else ""
        return f"Workday goals, report {self.report_owner}/{self.report}: {len(self.ids)} goals{note}"

    def fingerprint(self) -> str | None:
        return f"workday-goals:{self.client.tenant}"

    def role(self, title: str) -> str:
        return self.role_map.get(title) or role_id(title)

    def read(self, schema: list[dict]) -> Iterable[Record]:
        rows = self.client.report(self.report_owner, self.report)
        if rows:
            missing = [c for c in GOAL_COLUMNS if c not in rows[0]]
            if missing:
                self.problems.append(f"report {self.report} has no column {', '.join(missing)}")
                return
        live = [r for r in rows if str(r.get("Status") or "").strip().lower() not in CLOSED_GOALS]
        self.ids = {str(r["Goal_ID"]).strip() for r in live}
        for r in live:
            yield Record(ref=f"Workday goal {r['Goal_ID']}", data=r, type="objectives")

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        r = record.data
        gid = str(r["Goal_ID"]).strip()
        e: dict = {"id": gid, "name": str(r.get("Goal_Name") or gid).strip()}
        if str(r.get("Description") or "").strip():
            e["definition"] = str(r["Description"]).strip()
        parent = str(r.get("Parent_Goal_ID") or "").strip()
        if parent in self.ids:
            e["parentObjective"] = parent
        elif parent:
            self.orphans.append(gid)
        level = str(r.get("Goal_Level") or "").strip().lower()
        e["level"] = level if level in LEVELS else ("function" if parent else "enterprise")
        kpis = [k.strip() for k in str(r.get("KPI_IDs") or "").split(";") if k.strip()]
        if kpis:
            e["measuredByKpis"] = kpis if len(kpis) > 1 else kpis[0]
        items = []
        title = str(r.get("Owner_Business_Title") or "").strip()
        if title:
            e["ownerRole"] = self.role(title)
            items.append(Item(type="roles", entity={"id": e["ownerRole"], "name": title}, ref=record.ref))
        return [*items, Item(type="objectives", entity=e, ref=record.ref)]
