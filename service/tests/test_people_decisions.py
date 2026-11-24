"""Approvals beyond money (step 6b): over whom an authority may be used, limits that are not
money (percent, grade steps, headcount), approval chains, and HR business processes as the
workflow in force for decisions about people."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veromesh import decisions as D
from veromesh import provenance as prov
from veromesh.api import create_app
from veromesh.config import Settings

API = "/api/v1"
DAY = "2026-11-16"

PEOPLE = [
    {
        "id": "DanaExample",
        "name": "Dana Example",
        "employeeId": "E-1001",
        "rolesFilled": ["ApManager"],
        "reportsTo": "LeeExample",
        "department": "AP",
        "costCentre": "CC-100",
    },
    {
        "id": "LeeExample",
        "name": "Lee Example",
        "employeeId": "E-0042",
        "rolesFilled": ["Controller", "ErpPlatformLead", "AiReviewBoard"],
        "department": "FIN",
        "costCentre": "CC-100",
    },
    {
        "id": "PatExample",
        "name": "Pat Example",
        "employeeId": "E-2001",
        "reportsTo": "DanaExample",
        "department": "AP",
        "costCentre": "CC-100",
    },
    {
        "id": "KimExample",
        "name": "Kim Example",
        "employeeId": "E-2002",
        "reportsTo": "LeeExample",
        "rolesFilled": ["HrPartner"],
        "department": "FIN",
        "costCentre": "CC-200",
    },
]
ACTIONS = [
    {"id": "AdjustPay", "name": "Adjust pay"},
    {"id": "PromoteEmployee", "name": "Promote an employee"},
    {"id": "HireEmployee", "name": "Hire an employee"},
]
BP = [  # what Workday's business processes enforce (the workflow in force)
    {
        "id": "ManagerPay",
        "name": "Managers raise pay in their team up to 5%",
        "holder": "ApManager",
        "actionsCovered": ["AdjustPay"],
        "limitKind": "percent",
        "limitValue": "5",
        "scope": "reporting-line",
        "delegatedBy": "DelegationPolicy",
    },
    {
        "id": "ControllerPay",
        "name": "Finance raises pay up to 15%",
        "holder": "Controller",
        "actionsCovered": ["AdjustPay"],
        "limitKind": "percent",
        "limitValue": "15",
        "scope": "cost-centre",
        "delegatedBy": "DelegationPolicy",
    },
    {
        "id": "ManagerPromotion",
        "name": "Managers promote one grade in their team",
        "holder": "ApManager",
        "actionsCovered": ["PromoteEmployee"],
        "limitKind": "grade-steps",
        "limitValue": "1",
        "scope": "reporting-line",
        "delegatedBy": "DelegationPolicy",
    },
    {
        "id": "AgentPay",
        "name": "The assistant prepares raises up to 5%",
        "holder": "InvoiceAssistant",
        "actionsCovered": ["AdjustPay"],
        "limitKind": "percent",
        "limitValue": "5",
        "scope": "reporting-line",
        "parentAuthority": "ManagerPay",
        "delegatedBy": "DanaExample",
    },
    {
        "id": "AgentPromotion",
        "name": "The assistant prepares promotions",
        "holder": "InvoiceAssistant",
        "actionsCovered": ["PromoteEmployee"],
        "limitKind": "grade-steps",
        "limitValue": "1",
        "scope": "reporting-line",
        "parentAuthority": "ManagerPromotion",
        "delegatedBy": "DanaExample",
    },
    {
        "id": "ManagerHire",
        "name": "Managers open up to 3 hires in their department",
        "holder": "ApManager",
        "actionsCovered": ["HireEmployee"],
        "limitKind": "headcount",
        "limitValue": "3",
        "scope": "department",
        "delegatedBy": "DelegationPolicy",
    },
    {
        "id": "AgentHire",
        "name": "The assistant opens hires",
        "holder": "InvoiceAssistant",
        "actionsCovered": ["HireEmployee"],
        "limitKind": "headcount",
        "limitValue": "2",
        "scope": "department",
        "parentAuthority": "ManagerHire",
        "delegatedBy": "DanaExample",
    },
]
CHAIN = [  # a promotion: the employee's manager's manager, then the HR partner
    {"authority": "ManagerPromotion", "approverRelation": "manager-of-manager", "step": "1"},
    {"authority": "ManagerPromotion", "approverRole": "HrPartner", "step": "2"},
]


def load(client, source, category, items):
    assert client.put(f"{API}/sources/{source}", json={"category": category}).status_code == 200
    cs = client.post(f"{API}/changesets", json={"source": source, "mode": "merge"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.text


@pytest.fixture
def ctx(service, monkeypatch):
    monkeypatch.setenv("VEROMESH_PROPOSALS", "off")  # set-up loads, not changes to settle
    log = D.MemoryLog()
    settings = Settings(rdf_store="memory", auth_disabled=True, decision_unmapped_impact="allow")
    client = TestClient(create_app(service, settings, decision_log=log))
    items = client.get(f"{API}/examples/procure-to-pay").json()["items"]
    load(client, "p2p", "spreadsheet", items + [{"type": "actions", "entity": a} for a in ACTIONS])
    load(
        client,
        "workday",
        "hr",
        [{"type": "roles", "entity": {"id": "HrPartner", "name": "HR partner"}}]
        + [{"type": "people", "entity": p} for p in PEOPLE],
    )
    load(
        client,
        "workday-bp",
        "hr",
        [{"type": "authorities", "entity": a} for a in BP]
        + [{"type": "co-approvals", "entity": c} for c in CHAIN],
    )
    agent = next(i for i in items if i["entity"].get("id") == "InvoiceAssistant")
    agent["entity"]["performsActions"] = [*agent["entity"]["performsActions"], *(a["id"] for a in ACTIONS)]
    load(client, "p2p", "spreadsheet", [agent])
    for a in ("AgentPay", "AgentPromotion", "AgentHire"):
        for c in prov.authority_basis(service.store, a)["checks"]:
            if not c["ok"] and c["status"] != "missing":
                r = client.post(
                    f"{API}/{c['type']}/{c['id']}/attestations",
                    json={
                        "verdict": "attested",
                        "attester": "LeeExample",
                        "role": "Controller",
                        "fields": [c["field"]],
                    },
                )
                assert r.status_code == 201, r.text
    return client, log


def ask(client, action, **kw):
    r = client.post(
        f"{API}/decisions/check", json={"agent": "InvoiceAssistant", "action": action, "onDate": DAY, **kw}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_a_raise_within_the_limit_for_someone_in_the_team(ctx):
    client, _ = ctx
    d = ask(client, "AdjustPay", about="PatExample", value="4")
    assert d["outcome"] == "allow", d["reasons"]
    assert d["authority"] == "AgentPay" and d["basis"]["about"] == "PatExample" and d["basis"]["value"] == "4"
    assert "limit 5 percent" in d["reasons"][0] and "scope reporting-line" in d["reasons"][0]


def test_a_raise_above_the_limit_goes_to_the_next_authority_up(ctx):
    client, _ = ctx
    d = ask(client, "AdjustPay", about="PatExample", value="8")
    assert d["outcome"] == "consult", d["reasons"]
    assert d["basis"]["nextAuthority"] == "ControllerPay"
    assert [c["role"] for c in d["consult"]] == ["Controller"]
    assert "8 percent is above its limit of 5 percent" in " ".join(d["consult"][0]["why"])


def test_no_authority_covers_a_large_raise(ctx):
    client, _ = ctx
    d = ask(client, "AdjustPay", about="PatExample", value="20")
    assert d["outcome"] == "refuse" and d["escalateTo"]["person"] == "DanaExample"


def test_someone_outside_the_reporting_line_is_out_of_scope(ctx):
    client, _ = ctx
    d = ask(client, "AdjustPay", about="KimExample", value="3")
    assert d["outcome"] == "refuse"
    assert "Kim Example is outside its scope (the reporting line of Dana Example)" in " ".join(d["reasons"])


def test_a_scoped_authority_needs_the_person(ctx):
    client, _ = ctx
    d = ask(client, "AdjustPay", value="3")
    assert d["outcome"] == "refuse" and "give the person the action is about" in " ".join(d["reasons"])
    d = ask(client, "AdjustPay", about="PatExample")
    assert d["outcome"] == "refuse" and "give the value" in " ".join(d["reasons"])


def test_a_promotion_follows_the_approval_chain(ctx):
    client, log = ctx
    d = ask(client, "PromoteEmployee", about="PatExample", value="1")
    assert d["outcome"] == "consult", d["reasons"]
    assert [(c.get("step"), c["role"], [p["id"] for p in c["people"]]) for c in d["consult"]] == [
        (1, "person:LeeExample", ["LeeExample"]),  # Pat's manager's manager
        (2, "HrPartner", ["KimExample"]),
    ]
    assert [s["step"] for s in d["basis"]["approvalChain"]] == [1, 2]
    for who in ("LeeExample", "KimExample"):
        r = client.post(f"{API}/decisions/{d['id']}/overrides", json={"verdict": "approved", "person": who})
        assert r.status_code == 201, r.text
    st = client.get(f"{API}/decisions/{d['id']}/status").json()
    assert st["status"] == "approved" and st["mayAct"]


def test_two_grades_is_above_a_managers_promotion_authority(ctx):
    client, _ = ctx
    d = ask(client, "PromoteEmployee", about="PatExample", value="2")
    assert d["outcome"] == "refuse"
    assert "2 grade-steps is above its limit of 1 grade-steps" in " ".join(d["reasons"])


def test_hires_count_against_a_headcount_limit_in_the_department(ctx):
    client, _ = ctx
    assert ask(client, "HireEmployee", about="PatExample", value="2")["outcome"] == "allow"
    d = ask(client, "HireEmployee", about="PatExample", value="3")  # the manager's own limit is 3
    assert d["outcome"] == "consult" and d["basis"]["nextAuthority"] == "ManagerHire"
    assert ask(client, "HireEmployee", about="PatExample", value="4")["outcome"] == "refuse"
    d = ask(client, "HireEmployee", about="KimExample", value="1")  # a requisition in another department
    assert d["outcome"] == "refuse" and "department FIN) is outside its scope (department AP)" in " ".join(
        d["reasons"]
    )


def test_the_business_process_is_in_force_over_the_workbook(ctx):
    client, _ = ctx
    policy = {**next(a for a in BP if a["id"] == "ManagerPay"), "limitValue": "10"}
    load(client, "workbook", "governance", [{"type": "authorities", "entity": policy}])
    a = client.get(f"{API}/authorities/ManagerPay").json()
    assert float(a["limitValue"]) == 5  # Workday's business process decides
    f = client.get(f"{API}/findings", params={"kind": "out-of-sync"}).json()["findings"]
    assert any(x["subject"]["id"] == "ManagerPay" and x["about"]["field"] == "limitValue" for x in f)


def test_an_unknown_person_or_a_bad_value_is_an_error(ctx):
    client, _ = ctx
    body = {"agent": "InvoiceAssistant", "action": "AdjustPay", "onDate": DAY}
    assert client.post(f"{API}/decisions/check", json={**body, "about": "Nobody"}).status_code == 404
    assert client.post(f"{API}/decisions/check", json={**body, "value": "lots"}).status_code in (400, 422)
