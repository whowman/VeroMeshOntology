"""The Workday and Entra ID connectors against the VeroMesh test harness (a simulated company).

Skipped unless the harness is installed:

    pip install "veromesh-test-harness @ git+https://github.com/whowman/VeroMeshTestHarness"

The harness serves mocks of Workday's and Microsoft Graph's public APIs over a
company whose truth it knows, with each system's quirks recorded as defects.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

harness_truth = pytest.importorskip("harness.truth")
harness_app = pytest.importorskip("harness.app")

from fastapi.testclient import TestClient  # noqa: E402

from veromesh_connectors.cli import main  # noqa: E402

API = ["--api", "http://testserver"]
WORKDAY = ["workday", "import", "--url", "http://testserver/workday", "--tenant", "kvh"]
GRAPH = ["--tenant", "kvh", "--graph", "http://testserver/entra", "--login", "http://testserver/entra"]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    for k, v in {
        "WORKDAY_CLIENT_ID": "veromesh-connector",
        "WORKDAY_CLIENT_SECRET": "harness-secret",
        "WORKDAY_REFRESH_TOKEN": "harness-workday-refresh",
        "ENTRA_CLIENT_ID": "veromesh-connector",
        "ENTRA_CLIENT_SECRET": "harness-secret",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def company():
    truth = harness_truth.generate("healthcare", 300, 7)
    return truth, TestClient(harness_app.create_app(truth))


def load(http, h) -> None:
    import tempfile

    groups = Path(tempfile.mkdtemp()) / "groups.yaml"  # Workday security groups to model roles
    groups.write_text("Manager: PeopleManager\nHR Partner: HRPartner\n")
    wd = [*WORKDAY, "--manager-role", "Manager", "--role-map", str(groups)]
    assert main([*API, *wd], http=http, source_http=h) == 0
    # The business process rules (promotions, pay, hires, terminations) the workbook's agents rely on.
    approvals = ["workday", "approvals", *WORKDAY[2:], "--role-map", str(groups)]
    assert main([*API, *approvals], http=http, source_http=h) == 0
    assert (
        main([*API, "entra", "import", *GRAPH, "--role-group-prefix", "SG-"], http=http, source_http=h) == 0
    )


def test_everyone_working_is_loaded_and_agents_act_for_their_principals(http, company):
    truth, h = company
    load(http, h)
    people = {p["id"] for p in http.get("/api/v1/people").json()}
    employees = {p.employee_id for p in truth.active_people() if p.worker_type == "employee"}
    assert employees <= people
    agents = {a["id"]: a for a in http.get("/api/v1/agents").json()}
    for a in truth.agents.values():
        assert agents[a.id]["actsFor"] == truth.people[a.acts_for].employee_id


def test_a_month_of_changes_through_both_feeds(http, company, tmp_path):
    truth, h = company
    load(http, h)
    state = tmp_path / "entra.json"
    entra = [*API, "entra", "changes", *GRAPH, "--role-group-prefix", "SG-", "--state", str(state)]
    assert main(entra, http=http, source_http=h) == 0  # records the position
    start = truth.today
    h.post("/harness/clock", json={"days": 30})
    changes = [*API, "workday", "changes", "--url", "http://testserver/workday", "--tenant", "kvh"]
    assert main([*changes, "--since", start.isoformat()], http=http, source_http=h) == 0
    assert main(entra, http=http, source_http=h) == 0
    people = {p["id"] for p in http.get("/api/v1/people").json()}
    left = {p.employee_id for p in truth.people.values() if p.left and start < p.left <= truth.today}
    hired = {p.employee_id for p in truth.people.values() if start < p.hired <= truth.today}
    # A leaver whose reports' moves wait for attestation (the proposal step) is kept only as
    # an ended record, so the reporting line in force still resolves; no source lists them.
    ended = {p["id"] for p in http.get("/api/v1/people", params={"source": "ended"}).json()}
    assert left and (left & people) <= ended
    waiting = {p["inForce"] for p in http.get("/api/v1/proposals", params={"type": "people"}).json()}
    assert (left & people) <= waiting
    assert hired and hired <= people


def test_misbehaving_systems_give_the_same_result(http, company):
    truth, h = company
    h.put("/harness/chaos", json={"rate": 0.3, "seed": 1, "slow_ms": 20})
    load(http, h)
    seen = h.get("/harness/chaos").json()["seen"]
    assert seen["throttle"] + seen["server-error"] + seen["unavailable"] + seen["token-expired"] > 0
    calm = harness_truth.generate("healthcare", 300, 7)
    assert {p["id"] for p in http.get("/api/v1/people").json()} >= {
        p.employee_id for p in calm.active_people() if p.worker_type == "employee"
    }


ERP = ["--url", "http://testserver/erp"]
ERP_ROLES = {
    "ORA_ACCOUNTS_PAYABLE_SPECIALIST_JOB": "APClerk",
    "ORA_ACCOUNTS_PAYABLE_MANAGER_JOB": "APManager",
    "ORA_FINANCIAL_APPROVER_JOB": "FinanceApprover",
    "ORA_BUYER_JOB": "Buyer",
    "ORA_PROCUREMENT_MANAGER_JOB": "ProcurementApprover",
}


def test_oracle_erp_accounts_and_approval_exceptions(http, company, tmp_path, monkeypatch):
    truth, h = company
    monkeypatch.setenv("ORACLE_USER", "integration.user")
    monkeypatch.setenv("ORACLE_PASSWORD", "harness")
    load(http, h)
    book = tmp_path / "governance.xlsx"
    book.write_bytes(h.get("/governance/workbook.xlsx").content)
    assert main([*API, "spreadsheet", "import", str(book), "--source", "governance"], http=http) == 0
    for name, cat in (("workday", "hr"), ("entra", "iam"), ("governance", "governance"), ("erp", "erp")):
        assert http.put(f"/api/v1/sources/{name}", json={"category": cat}).status_code == 200
    roles = tmp_path / "erp-roles.yaml"
    roles.write_text("".join(f"{k}: {v}\n" for k, v in ERP_ROLES.items()))
    assert main([*API, "erp", "import", *ERP, "--role-map", str(roles)], http=http, source_http=h) == 0

    erp = {p["id"]: p for p in http.get("/api/v1/people", params={"source": "erp"}).json()}
    known = {p["id"] for p in http.get("/api/v1/people", params={"source": "entra"}).json()}
    assert erp and set(erp) <= known  # every ERP account matched to the person Entra ID holds
    assert "employeeId" not in next(iter(erp.values()))  # the ERP is not the record of employment
    by_eid = {p.employee_id: p for p in truth.people.values()}
    ap = [pid for pid, p in erp.items() if "APManager" in (p.get("rolesFilled") or [])]
    assert ap and all("APManager" in by_eid[pid].at(truth.today).roles for pid in ap if pid in by_eid)

    since = (truth.start - timedelta(days=90)).isoformat()
    assert main([*API, "erp", "exceptions", *ERP, "--since", since], http=http, source_http=h) == 0
    key = h.get("/harness/answer-key").json()["defects"]

    def got(kind, field):
        return {
            f["about"][field] for f in http.get("/api/v1/findings", params={"kind": kind}).json()["findings"]
        }

    assert got("self-approved-po", "po") == {d["about"]["po"] for d in key if d["kind"] == "self-approved-po"}
    assert got("approved-over-limit", "invoice") == {
        d["about"]["invoice"] for d in key if d["kind"] == "approved-over-limit"
    }


def test_servicenow_cmdb_systems_and_owner_roles(http, company, monkeypatch):
    truth, h = company
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "harness")
    load(http, h)
    for name, cat in (("workday", "hr"), ("entra", "iam"), ("servicenow", "cmdb")):
        assert http.put(f"/api/v1/sources/{name}", json={"category": cat}).status_code == 200
    assert (
        main(
            [*API, "servicenow", "import", "--url", "http://testserver/servicenow"], http=http, source_http=h
        )
        == 0
    )
    systems = {s["id"]: s for s in http.get("/api/v1/systems", params={"source": "servicenow"}).json()}
    assert len(systems) == len(truth.systems) + 1  # one application is in the CMDB twice
    ehr = next(s for s in systems.values() if s["name"] == truth.systems["EHR"].name)
    assert ehr["businessOwner"] == f"{ehr['id']}.BusinessOwner" and ehr["criticality"] == "critical"
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    owner = truth.people[truth.systems["EHR"].business_owner]
    roles = people[owner.employee_id].get("rolesFilled") or []
    assert ehr["businessOwner"] in (roles if isinstance(roles, list) else [roles])
    dups = http.get("/api/v1/findings", params={"kind": "duplicate-system"}).json()["findings"]
    assert len(dups) == 1 and dups[0]["about"]["names"][1] == "Epic"


def test_halo_applications_and_agent_seats(http, company, monkeypatch):
    truth, h = company
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "harness")
    monkeypatch.setenv("HALO_CLIENT_ID", "veromesh-connector")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "harness-secret")
    load(http, h)
    for name, cat, access in (
        ("workday", "hr", None),
        ("entra", "iam", None),
        ("servicenow", "cmdb", None),
        ("servicenow-users", "cmdb", True),
        ("halo", "cmdb", None),
        ("halo-agents", "cmdb", True),
    ):
        body = {"category": cat, **({"grantsAccess": access} if access is not None else {})}
        assert http.put(f"/api/v1/sources/{name}", json=body).status_code == 200
    assert (
        main(
            [*API, "servicenow", "import", "--url", "http://testserver/servicenow"], http=http, source_http=h
        )
        == 0
    )
    assert http.get("/api/v1/people", params={"source": "servicenow-users"}).json()
    assert main([*API, "halo", "import", "--url", "http://testserver/halo"], http=http, source_http=h) == 0
    sn = {s["name"]: s["id"] for s in http.get("/api/v1/systems", params={"source": "servicenow"}).json()}
    halo = {s["name"]: s for s in http.get("/api/v1/systems", params={"source": "halo"}).json()}
    assert halo and all(halo[n]["id"] == sn[n] for n in halo if n in sn)  # one system, two sources
    agents = http.get("/api/v1/people", params={"source": "halo-agents"}).json()
    it = {
        p.employee_id
        for p in truth.people.values()
        if (p.at(truth.today) or p.history[-1]).unit in ("IT", "INFRA", "APPS", "SEC")
    }
    assert agents and {a["id"] for a in agents} <= it | {
        a["id"] for a in agents if not a["id"].startswith("W")
    }
