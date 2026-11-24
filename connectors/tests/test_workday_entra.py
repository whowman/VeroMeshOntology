"""The Workday and Entra ID connectors, against small fakes of the public APIs and the real VeroMesh API.

The fakes answer the documented calls each connector makes (Workday RaaS and REST
with OAuth; Microsoft Graph users with $expand=manager, groups, service principals
and delta), with paging, a token that expires, and transient failures. The fuller
simulation lives in the VeroMesh test harness (test_against_harness.py).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from veromesh_connectors.cli import main
from veromesh_connectors.plugins.entra import agent_id, local_id
from veromesh_connectors.plugins.workday import role_id

API = ["--api", "http://testserver"]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    for k, v in {
        "WORKDAY_CLIENT_ID": "c",
        "WORKDAY_CLIENT_SECRET": "s",
        "WORKDAY_REFRESH_TOKEN": "r",
        "ENTRA_CLIENT_ID": "c",
        "ENTRA_CLIENT_SECRET": "s",
        "VEROMESH_RETRIES": "3",
    }.items():
        monkeypatch.setenv(k, v)


def respond(body, status=200, headers=None):
    return httpx.Response(status, json=body, headers=headers or {})


class Fake:
    """A source system: routes by path, counts calls, can fail the next N calls."""

    def __init__(self):
        self.tokens = 0
        self.valid = set()
        self.fail_next: list[int] = []
        self.calls: list[str] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def issue(self) -> httpx.Response:
        self.tokens += 1
        tok = f"t{self.tokens}"
        self.valid = {tok}
        return respond({"access_token": tok, "token_type": "Bearer", "expires_in": 3600})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/token"):
            return self.issue()
        if self.fail_next:
            return respond({"error": "busy"}, self.fail_next.pop(0), {"Retry-After": "0"})
        if request.headers.get("Authorization", "")[7:] not in self.valid:
            return respond({"error": {"code": "InvalidAuthenticationToken", "message": "expired"}}, 401)
        return self.route(
            request, path, {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        )


# ---------------------------------------------------------------------------- Workday

WORKERS = [
    # employee ID, name, title, manager, terminated
    ("E1", "Ana Ruiz", "Chief Financial Officer", "", ""),
    ("E2", "Ben Ode", "Accounts Payable Manager", "E1", ""),
    ("E3", "Cy Park", "Accounts Payable Clerk", "E2", ""),
    ("E4", "Di Low", "Accounts Payable Clerk", "E2", "2026-01-31"),  # left
    ("E5", "Ed Fox", "Buyer", "C9", ""),  # manager is a contractor, not in Workday
]


# Business process rules, as a custom report over their definitions lays them out.
RULES = [
    {
        "Business_Process_Type": "Request Compensation Change",
        "Rule_Type": "Initiator",
        "Security_Group": "Manager",
        "Condition_Measure": "Percent_Change",
        "Condition_Above": "5",
        "Organization_Scope": "Supervisory Organization",
    },
    {
        "Business_Process_Type": "Request Compensation Change",
        "Rule_Type": "Initiator",
        "Security_Group": "HR Partner",
        "Condition_Measure": "Percent_Change",
        "Condition_Above": "12",
        "Organization_Scope": "Company",
    },
    {
        "Business_Process_Type": "Request Compensation Change",
        "Rule_Type": "Approval",
        "Security_Group": "Manager's Manager",
        "Step_Order": "1",
        "Condition_Measure": "Percent_Change",
        "Condition_Above": "3",
    },
    {
        "Business_Process_Type": "Promote Employee Inbound",
        "Rule_Type": "Initiator",
        "Security_Group": "Manager",
        "Condition_Measure": "Grade_Steps",
        "Condition_Above": "1",
        "Organization_Scope": "Supervisory Organization",
    },
    {
        "Business_Process_Type": "Promote Employee Inbound",
        "Rule_Type": "Approval",
        "Security_Group": "HR Partner",
        "Step_Order": "2",
    },
    {
        "Business_Process_Type": "Promote Employee Inbound",
        "Rule_Type": "Approval",
        "Security_Group": "Manager's Manager",
        "Step_Order": "1",
    },
    {
        "Business_Process_Type": "Move Worker",
        "Rule_Type": "Initiator",
        "Security_Group": "Manager",
    },  # not mapped
]


class FakeWorkday(Fake):
    def __init__(self):
        super().__init__()
        self.changes: list[dict] = []
        self.workers = {w[0]: w for w in WORKERS}
        self.rules = list(RULES)

    def rest(self, w):
        emp, name, title, mgr, end = w
        first, last = name.split()
        return {
            "id": f"wid-{emp}",
            "workerId": emp,
            "descriptor": name,
            "preferredName": {"firstName": first, "lastName": last},
            "primaryWorkEmail": f"{first.lower()}@example.com",
            "businessTitle": title,
            "manager": {"id": f"wid-{mgr}"} if mgr else None,  # REST gives the manager's WID
            "hireDate": "2020-01-01",
            "terminationDate": end or None,
            "active": not end,
        }

    def route(self, request, path, q):
        if path.endswith("/VeroMesh_Workers"):
            assert q["format"] == "json"
            rows = []
            for emp, name, title, mgr, end in self.workers.values():
                rows.append(
                    {
                        "Employee_ID": emp,
                        "Preferred_Name": name,
                        "Email_Primary_Work": f"{name.split()[0].lower()}@example.com",
                        "Business_Title": title,
                        "Manager_Employee_ID": mgr,
                        "Hire_Date": "2020-01-01",
                        "Termination_Date": end,
                        "Active_Status": "0" if end else "1",
                    }
                )
            return respond({"Report_Entry": rows})
        if path.endswith("/VeroMesh_Approval_Rules"):
            return respond({"Report_Entry": self.rules})
        if path.endswith("/VeroMesh_Worker_Changes"):
            return respond(
                {"Report_Entry": [c for c in self.changes if c["Entered_On"] >= q["Updated_From"]]}
            )
        if path.endswith("/workers"):
            items = [self.rest(w) for w in self.workers.values()]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 20))
            return respond({"total": len(items), "data": items[off : off + lim]})
        if "/workers/" in path:  # by employee ID or by WID
            return respond(self.rest(self.workers[path.rsplit("/", 1)[1].removeprefix("wid-")]))
        return respond({"error": "not found"}, 404)


def workday(*args):
    return [*API, "workday", *args, "--url", "http://wd.example", "--tenant", "acme"]


def test_workday_report_loads_current_workers_roles_and_managers(http):
    wd = FakeWorkday()
    assert main(workday("import"), http=http, source_http=wd.client()) == 0
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    assert set(people) == {"E1", "E2", "E3", "E5"}  # E4 has left
    assert people["E3"]["reportsTo"] == "E2" and people["E3"]["rolesFilled"] == ["accounts-payable-clerk"]
    assert "reportsTo" not in people["E5"]  # a contractor manager is not in this source
    roles = {r["id"] for r in http.get("/api/v1/roles").json()}
    assert {"chief-financial-officer", "accounts-payable-manager", "accounts-payable-clerk", "buyer"} <= roles
    assert wd.tokens == 1


def test_workday_rest_pages_and_resolves_managers_by_wid(http, monkeypatch):
    monkeypatch.setattr("veromesh_connectors.plugins.workday.PAGE", 2)
    wd = FakeWorkday()
    assert main(workday("import", "--via", "rest", "--no-roles"), http=http, source_http=wd.client()) == 0
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    assert people["E2"]["reportsTo"] == "E1" and not people["E2"].get("rolesFilled")
    assert sum(c.endswith("/workers") for c in wd.calls) == 3  # 5 workers, 2 a page


def test_workday_retries_and_renews_its_token(http):
    wd = FakeWorkday()
    wd.fail_next = [503, 401, 500]  # unavailable, then the token expires, then a server error
    assert main(workday("import"), http=http, source_http=wd.client()) == 0
    assert wd.tokens == 2  # signed in, and renewed once after the 401
    assert len(http.get("/api/v1/people").json()) == 4


def test_workday_changes_apply_hires_moves_and_terminations(http):
    wd = FakeWorkday()
    main(workday("import"), http=http, source_http=wd.client())
    wd.workers["E6"] = ("E6", "Flo Ng", "Buyer", "E2", "")
    wd.workers["E3"] = ("E3", "Cy Park", "Accounts Payable Manager", "E1", "")
    wd.changes = [
        {
            "Employee_ID": "E6",
            "Business_Process": "Hire",
            "Effective_Date": "2026-02-01",
            "Entered_On": "2026-02-01",
        },
        {
            "Employee_ID": "E3",
            "Business_Process": "Change Job",
            "Effective_Date": "2026-02-02",
            "Entered_On": "2026-02-02",
        },
        {
            "Employee_ID": "E5",
            "Business_Process": "Termination",
            "Effective_Date": "2026-02-03",
            "Entered_On": "2026-02-05",
        },
    ]
    rc = main(workday("changes", "--since", "2026-02-01"), http=http, source_http=wd.client())
    assert rc == 0
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    assert "E6" in people and "E5" not in people
    # The move is in Workday's record and waits for attestation before it is in force.
    wd_people = {p["id"]: p for p in http.get("/api/v1/people", params={"source": "workday"}).json()}
    assert wd_people["E3"]["reportsTo"] == "E1" and wd_people["E3"]["rolesFilled"] == [
        "accounts-payable-manager"
    ]
    assert {p["field"] for p in http.get("/api/v1/proposals", params={"id": "E3"}).json()} == {
        "reportsTo",
        "rolesFilled",
    }
    # Delivered again (an overlapping --since): each event is applied once.
    runs = len(http.get("/api/v1/changesets", params={"limit": 100}).json())
    main(workday("changes", "--since", "2026-02-01"), http=http, source_http=wd.client())
    assert len(http.get("/api/v1/changesets", params={"limit": 100}).json()) == runs


def test_workday_report_without_the_needed_columns_sends_nothing(http, capsys):
    wd = FakeWorkday()
    wd.route = lambda request, path, q: respond({"Report_Entry": [{"Employee_ID": "E1"}]})
    assert main(workday("import"), http=http, source_http=wd.client()) == 1
    assert "has no column" in capsys.readouterr().out
    assert http.get("/api/v1/people").json() == []


# ---------------------------------------------------------------------------- Entra ID

USERS = [
    # object ID, UPN, employee ID, manager, enabled, groups
    ("u1", "ana@example.com", "E1", None, True, ["SG-CFO"]),
    ("u2", "ben@example.com", "E2", "u1", True, ["SG-APManager"]),
    ("u3", "cy@example.com", "E3", "u2", True, ["SG-APClerk"]),
    ("u4", "di@example.com", "E4", "u2", False, ["SG-APClerk"]),  # left; account disabled
    ("u5", "Kim.Vo@example.com", None, "u4", True, []),  # contractor; manager disabled
]


class FakeGraph(Fake):
    def __init__(self):
        super().__init__()
        self.users = {u[0]: u for u in USERS}
        self.removed: list[str] = []
        self.changed: list[str] = []

    def user(self, u, select=None):
        oid, upn, emp, _mgr, enabled, _g = u
        out = {
            "id": oid,
            "userPrincipalName": upn,
            "displayName": upn.split("@")[0].title(),
            "mail": upn if oid != "u3" else None,
            "employeeId": emp,
            "accountEnabled": enabled,
        }
        if select:
            out = {k: v for k, v in out.items() if k in select.split(",") or k == "id"}
        return out

    def page(self, request, items, q):
        start, top = int(q.get("$skiptoken", 0)), 2  # small pages, whatever $top asks
        out = {"value": items[start : start + top]}
        if start + top < len(items):
            out["@odata.nextLink"] = str(request.url.copy_merge_params({"$skiptoken": str(start + top)}))
        return out

    def route(self, request, path, q):
        p = path.removeprefix("/v1.0")
        if p == "/users":
            items = []
            for u in self.users.values():
                item = self.user(u)
                if q.get("$expand", "").startswith("manager") and u[3]:
                    item["manager"] = {"id": u[3]}
                items.append(item)
            return respond(self.page(request, items, q))
        if p == "/users/delta":
            if "$deltatoken" in q:
                items = [self.user(self.users[o]) for o in self.changed] + [
                    {"id": o, "@removed": {"reason": "changed"}} for o in self.removed
                ]
                return respond({"value": items, "@odata.deltaLink": str(request.url)})
            body = self.page(request, [self.user(u) for u in self.users.values()], q)
            if "@odata.nextLink" not in body:
                body["@odata.deltaLink"] = "http://graph.example/v1.0/users/delta?$deltatoken=1"
            return respond(body)
        if p.endswith("/manager"):
            mgr = self.users[p.split("/")[2]][3]
            if not mgr or mgr not in self.users:
                return respond({"error": {"code": "Request_ResourceNotFound", "message": "no manager"}}, 404)
            return respond(self.user(self.users[mgr]))
        if p.endswith("/memberOf"):
            groups = self.users[p.split("/")[2]][5]
            return respond({"value": [{"id": g, "displayName": g} for g in groups]})
        if p == "/groups":
            assert q["$filter"] == "startswith(displayName,'SG-')"
            names = sorted({g for u in self.users.values() for g in u[5]})
            return respond(
                {"value": [{"id": g, "displayName": g, "description": f"{g[3:]}; a role"} for g in names]}
            )
        if p.startswith("/groups/") and p.endswith("/members"):
            g = p.split("/")[2]
            return respond({"value": [{"id": u[0]} for u in self.users.values() if g in u[5]]})
        if p == "/servicePrincipals":
            return respond(
                {
                    "value": [
                        {
                            "id": "sp1",
                            "appId": "app-1",
                            "displayName": "Invoice Assistant",
                            "tags": ["AIAgent"],
                        },
                        {"id": "sp2", "appId": "app-2", "displayName": "Some App", "tags": []},
                    ]
                }
            )
        if p == "/servicePrincipals/sp1/owners":
            return respond({"value": [{"id": "u2"}]})
        return respond({"error": {"code": "NotFound", "message": p}}, 404)


def entra(*args):
    return [
        *API,
        "entra",
        *args,
        "--tenant",
        "t1",
        "--graph",
        "http://graph.example",
        "--login",
        "http://login.example",
    ]


def test_entra_loads_people_roles_and_agents(http, capsys):
    g = FakeGraph()
    assert main(entra("import", "--role-group-prefix", "SG-"), http=http, source_http=g.client()) == 0
    out = capsys.readouterr().out
    assert "1 disabled accounts left out" in out and "1 accounts with no mail address" in out
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    assert set(people) == {"E1", "E2", "E3", "kim.vo"}  # employee ID, else the UPN's local part
    assert people["E3"]["reportsTo"] == "E2" and people["E3"]["rolesFilled"] == ["APClerk"]
    assert "email" not in people["E3"] and "reportsTo" not in people["kim.vo"]  # manager disabled
    role = next(r for r in http.get("/api/v1/roles").json() if r["id"] == "APClerk")
    assert role["definition"] == "APClerk; a role"  # free text keeps its ';'
    agents = http.get("/api/v1/agents").json()
    assert [(a["id"], a["signInClientId"], a["actsFor"]) for a in agents] == [
        ("InvoiceAssistant", "app-1", "E2")
    ]
    assert g.calls.count("/v1.0/users") == 3  # paged, two a page


def test_entra_changes_record_the_position_then_apply_deltas(http, tmp_path):
    g = FakeGraph()
    client = g.client()
    state = tmp_path / "delta.json"
    flags = ["--state", str(state), "--role-group-prefix", "SG-"]
    main(entra("import", "--role-group-prefix", "SG-"), http=http, source_http=client)
    assert main(entra("changes", *flags), http=http, source_http=client) == 0
    saved = json.loads(state.read_text())
    assert saved["people"]["u5"] == "kim.vo" and "deltatoken" in saved["deltaLink"]

    g.users["u3"] = ("u3", "cy@example.com", "E3", "u1", True, ["SG-APManager"])  # promoted
    g.changed, g.removed = ["u3"], ["u5"]
    assert main(entra("changes", *flags), http=http, source_http=client) == 0
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    assert "kim.vo" not in people
    mine = {p["id"]: p for p in http.get("/api/v1/people", params={"source": "entra"}).json()}
    assert mine["E3"]["reportsTo"] == "E1" and mine["E3"]["rolesFilled"] == ["APManager"]
    # The promotion waits for attestation (the proposal step); the old role goes at once.
    assert people["E3"]["reportsTo"] == "E2" and "APManager" not in (people["E3"].get("rolesFilled") or [])
    assert {p["field"] for p in http.get("/api/v1/proposals", params={"id": "E3"}).json()} == {
        "reportsTo",
        "rolesFilled",
    }


def test_a_leaver_an_agent_acts_for_is_held_until_the_agent_is_reassigned(http, tmp_path, capsys):
    g = FakeGraph()
    client = g.client()
    state = tmp_path / "delta.json"
    main(entra("import"), http=http, source_http=client)
    main(entra("changes", "--state", str(state)), http=http, source_http=client)
    g.users["u2"] = ("u2", "ben@example.com", "E2", "u1", False, [])  # the Invoice Assistant's owner leaves
    g.users["u3"] = ("u3", "cy@example.com", "E3", "u1", True, [])
    g.changed = ["u3", "u2"]
    assert main(entra("changes", "--state", str(state)), http=http, source_http=client) == 1
    assert "Still referenced by agents 'InvoiceAssistant'" in capsys.readouterr().out
    assert "E2" in {p["id"] for p in http.get("/api/v1/people").json()}
    # The next full import leaves out the agent, whose only owner has left; then the change applies.
    assert main(entra("import"), http=http, source_http=client) == 0
    assert "agents with no enabled owner left out: Invoice Assistant" in capsys.readouterr().out
    assert http.get("/api/v1/agents").json() == []
    assert main(entra("changes", "--state", str(state)), http=http, source_http=client) == 0


def test_entra_changes_keep_their_position_when_an_event_fails(http, tmp_path):
    g = FakeGraph()
    client = g.client()
    state = tmp_path / "delta.json"
    main(entra("import"), http=http, source_http=client)
    main(entra("changes", "--state", str(state)), http=http, source_http=client)
    before = state.read_text()
    g.users["u3"] = ("u3", "cy@example.com", "E3", "u9", True, [])
    g.users["u9"] = ("u9", "zed@example.com", "E9", None, True, [])  # a manager VeroMesh has not seen
    g.changed = ["u3"]
    assert main(entra("changes", "--state", str(state)), http=http, source_http=client) == 1
    assert state.read_text() == before  # retried next run


def test_entra_sign_in_failure_is_reported(http, capsys):
    g = FakeGraph()
    g.issue = lambda: respond(
        {"error": "invalid_client", "error_description": "AADSTS7000215: bad secret"}, 401
    )
    assert main(entra("import"), http=http, source_http=g.client()) == 2
    assert "token request failed" in capsys.readouterr().out


def test_ids():
    assert role_id("Accounts Payable Manager") == "accounts-payable-manager"
    assert local_id("Jane.Doe@contoso.com") == "jane.doe"
    assert agent_id("Invoice Assistant") == "InvoiceAssistant" and agent_id("AP bot") == "APBot"


def test_business_process_rules_become_authorities_and_approval_chains(http):
    wd = FakeWorkday()
    main(workday("import", "--manager-role", "Manager"), http=http, source_http=wd.client())
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    assert "manager" in people["E2"]["rolesFilled"]  # E2 has reports: fills the Manager group
    assert "manager" not in (people["E3"].get("rolesFilled") or [])
    http.put("/api/v1/sources/workday-approvals", json={"category": "hr"})
    assert main(workday("approvals"), http=http, source_http=wd.client()) == 0
    auths = {a["id"]: a for a in http.get("/api/v1/authorities").json()}
    pay = auths["manager-AdjustPay"]
    assert pay["holder"] == "manager" and pay["limitKind"] == "percent" and float(pay["limitValue"]) == 5
    assert pay["scope"] == "reporting-line"
    assert auths["hr-partner-AdjustPay"]["scope"] == "any"
    assert auths["manager-PromoteEmployee"]["limitKind"] == "grade-steps"
    co = http.get("/api/v1/co-approvals").json()
    chain = sorted(
        (c["authority"], str(c.get("step")), c.get("approverRelation") or c.get("approverRole")) for c in co
    )
    assert ("manager-PromoteEmployee", "1", "manager-of-manager") in {
        (a, s.split(".")[0], r) for a, s, r in chain
    }
    assert ("manager-PromoteEmployee", "2", "hr-partner") in {(a, s.split(".")[0], r) for a, s, r in chain}
    above = [c for c in co if c["authority"] == "manager-AdjustPay"]
    assert above and float(above[0]["aboveAmount"]) == 3
