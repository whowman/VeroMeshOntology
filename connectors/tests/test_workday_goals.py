"""Workday goals as objectives: the cascade, owner roles, and the objectives domain.

A small stand-in for Workday serves the OAuth token endpoint and the goals report (RaaS),
so the connector runs through the CLI as it does against a real tenant.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from veromesh_connectors.cli import main

API = ["--api", "http://testserver"]
GOALS = ["workday", "goals", "--url", "http://testserver/workday", "--tenant", "acme"]


def workday(rows: list[dict]) -> TestClient:
    app = FastAPI()

    @app.post("/workday/ccx/oauth2/acme/token")
    def token():
        return {"access_token": "t", "token_type": "Bearer", "expires_in": 3600}

    @app.get("/workday/ccx/service/customreport2/acme/ISU_VeroMesh/VeroMesh_Goals")
    def report():
        return {"Report_Entry": rows}

    return TestClient(app)


def goal(gid, name, title, parent="", **more):
    return {
        "Goal_ID": gid,
        "Goal_Name": name,
        "Owner_Employee_ID": "E1",
        "Owner_Business_Title": title,
        "Parent_Goal_ID": parent,
        **more,
    }


ROWS = [
    goal(
        "G-1", "Pay suppliers on time", "Chief Financial Officer", Description="Every supplier paid by terms."
    ),
    goal("G-2", "Clear the invoice backlog", "Accounts Payable Manager", "G-1", KPI_IDs="OnTimePaymentRate"),
    goal("G-3", "Automate invoice matching", "Accounts Payable Manager", "G-2", Goal_Level="Process"),
    goal("G-4", "Old goal", "Accounts Payable Manager", "G-1", Status="Cancelled"),
    goal("G-5", "Aligned to a goal not in the report", "Controller", "G-99"),
]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("WORKDAY_CLIENT_ID", "c")
    monkeypatch.setenv("WORKDAY_CLIENT_SECRET", "s")
    monkeypatch.delenv("WORKDAY_REFRESH_TOKEN", raising=False)


def objectives(http, source=None) -> dict[str, dict]:
    params = {"source": source} if source else {}
    return {o["id"]: o for o in http.get("/api/v1/objectives", params=params).json()}


def test_the_goal_cascade_as_objectives(http):
    kpi = {
        "type": "kpis",
        "op": "upsert",
        "entity": {"id": "OnTimePaymentRate", "name": "On-time payment rate"},
    }
    cs = http.post("/api/v1/changesets", json={"source": "workbook"}).json()["id"]
    http.post(f"/api/v1/changesets/{cs}/items", json={"items": [kpi]})
    http.post(f"/api/v1/changesets/{cs}/commit")
    assert main([*API, *GOALS], http=http, source_http=workday(ROWS)) == 0
    got = objectives(http, "workday-goals")
    assert set(got) == {"G-1", "G-2", "G-3", "G-5"}  # the cancelled goal is left out
    assert got["G-1"]["level"] == "enterprise" and got["G-1"]["ownerRole"] == "chief-financial-officer"
    assert got["G-1"]["definition"] == "Every supplier paid by terms."
    assert got["G-2"]["parentObjective"] == "G-1" and got["G-2"]["level"] == "function"
    assert got["G-2"]["measuredByKpis"] in ("OnTimePaymentRate", ["OnTimePaymentRate"])
    assert got["G-3"]["level"] == "process" and got["G-3"]["parentObjective"] == "G-2"
    assert "parentObjective" not in got["G-5"]  # its parent is not in the report
    assert http.get("/api/v1/roles/accounts-payable-manager").json()["name"] == "Accounts Payable Manager"


def test_a_role_map_names_the_owner_roles(http, tmp_path):
    titles = tmp_path / "titles.yaml"
    titles.write_text("Accounts Payable Manager: ApManager\n")
    rows = [{k: v for k, v in r.items() if k != "KPI_IDs"} for r in ROWS[:3]]  # no KPIs loaded here
    assert main([*API, *GOALS, "--role-map", str(titles)], http=http, source_http=workday(rows)) == 0
    assert objectives(http, "workday-goals")["G-2"]["ownerRole"] == "ApManager"


def test_a_report_without_the_columns_sends_nothing(http, capsys):
    assert main([*API, *GOALS], http=http, source_http=workday([{"Goal_ID": "G-1"}])) == 1
    assert "has no column" in capsys.readouterr().out
    assert objectives(http) == {}


def test_workday_decides_the_owner_until_the_organization_says_otherwise(http):
    assert http.put("/api/v1/sources/workday-goals", json={"category": "hr"}).status_code == 200
    items = [
        {"type": "roles", "op": "upsert", "entity": {"id": "Controller", "name": "Controller"}},
        {
            "type": "objectives",
            "op": "upsert",
            "entity": {
                "id": "G-1",
                "name": "Pay suppliers on time",
                "ownerRole": "Controller",
                "priority": 1,
            },
        },
    ]
    cs = http.post("/api/v1/changesets", json={"source": "workbook"}).json()["id"]
    http.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
    http.post(f"/api/v1/changesets/{cs}/commit")
    assert main([*API, *GOALS], http=http, source_http=workday(ROWS[:1])) == 0
    g1 = objectives(http)["G-1"]
    assert g1["ownerRole"] == "chief-financial-officer"  # Workday decides the owner
    assert str(g1["priority"]) in ("1", "1.0")  # the workbook keeps the priority
    r = http.put("/api/v1/domains/objectives", json={"authoritative": ["spreadsheet"], "evidence": ["hr"]})
    assert r.status_code == 200, r.text
    assert objectives(http)["G-1"]["ownerRole"] == "Controller"
