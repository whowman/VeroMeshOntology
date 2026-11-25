"""Domains: which source decides each question, changeable per organization (issue #67)."""

from __future__ import annotations

import test_decide
from test_decide import API, DANA, LEE, ask, attest_basis, load_source

ctx = test_decide.ctx  # the same fixture: the procure-to-pay example, sign-in off


def test_the_shipped_domains(ctx):
    client, _, _ = ctx
    d = client.get(f"{API}/domains").json()
    assert list(d) == [
        "employment",
        "organization",
        "access",
        "authority-granted",
        "authority-in-force",
        "ai-approval",
        "systems",
        "capabilities",
        "data-meaning",
    ]
    assert d["capabilities"]["authoritative"] == ["ea"]  # the EA tool keeps the capability map
    assert d["employment"]["authoritative"] == ["hr"] and d["employment"]["contractors"] == "iam"
    assert d["authority-in-force"]["authoritative"] == [
        "erp",
        "hr",
        "governance",
    ]  # the workflows in force first
    assert not any(x.get("changed") for x in d.values())


def test_job_roles_and_approval_roles_both_count(ctx):
    client, _, _ = ctx
    load_source(client, "workday", "hr", [{**DANA, "rolesFilled": "Controller"}, LEE])  # a job role
    load_source(client, "entra", "iam", [DANA, LEE])  # Dana's approval group: ApManager
    dana = client.get(f"{API}/people/DanaExample").json()
    assert sorted(dana["rolesFilled"]) == ["ApManager", "Controller"]
    assert not any(
        c["field"] == "rolesFilled" and c["id"] == "DanaExample"
        for c in client.get(f"{API}/conflicts").json()
    )


def test_the_workflow_in_force_decides_an_authority_s_limit(ctx):
    client, _, _ = ctx
    granted = {"id": "AssistantApprovalAuthority", "limitAmount": "5000"}
    in_force = {"id": "AssistantApprovalAuthority", "limitAmount": "3000"}
    for name, cat, lim in (("workbook", "governance", granted), ("ap", "erp", in_force)):
        assert client.put(f"{API}/sources/{name}", json={"category": cat}).status_code == 200
        r = client.post(f"{API}/changesets", json={"source": name, "mode": "merge"}).json()["id"]
        base = next(
            i["entity"]
            for i in client.get(f"{API}/examples/procure-to-pay").json()["items"]
            if i["entity"]["id"] == "AssistantApprovalAuthority"
        )
        client.post(
            f"{API}/changesets/{r}/items",
            json={"items": [{"type": "authorities", "entity": {**base, **lim}}]},
        )
        assert client.post(f"{API}/changesets/{r}/commit").status_code == 200
    auth = client.get(f"{API}/authorities/AssistantApprovalAuthority").json()
    assert float(auth["limitAmount"]) == 3000  # the AP workflow, closest to the decision
    out_of_sync = [
        c for c in client.get(f"{API}/conflicts").json() if c["id"] == "AssistantApprovalAuthority"
    ]
    assert out_of_sync and out_of_sync[0]["field"] == "limitAmount"


def test_an_organization_changes_a_domain_and_the_change_is_on_the_chain(ctx):
    client, log, _ = ctx
    load_source(client, "workday", "hr", [{**DANA, "rolesFilled": "Controller"}, LEE])
    load_source(client, "entra", "iam", [DANA, LEE])
    r = client.put(f"{API}/domains/organization", json={"union": {}})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True and r.json()["rebuilt"]["entities"] > 0
    assert client.get(f"{API}/people/DanaExample").json()["rolesFilled"] == ["Controller"]  # HR alone now
    entry = log.find(kind="configuration")[-1]
    assert entry["setting"] == "domain:organization" and entry["value"] == {"union": {}}
    assert log.verify().ok
    assert client.delete(f"{API}/domains/organization").status_code == 200
    assert sorted(client.get(f"{API}/people/DanaExample").json()["rolesFilled"]) == [
        "ApManager",
        "Controller",
    ]


def test_employment_follows_the_domain(ctx):
    client, _, service = ctx
    load_source(client, "entra", "iam", [DANA, LEE])
    attest_basis(client, service)
    assert ask(client, 800)["basis"]["principalStatus"] == "unknown"  # no hr source: no rule
    client.put(f"{API}/domains/employment", json={"authoritative": ["iam"]})
    attest_basis(client, service)
    assert ask(client, 800)["basis"]["principalStatus"] == "employed"  # Entra ID decides here


def test_bad_changes_are_refused(ctx):
    client, _, _ = ctx
    assert client.put(f"{API}/domains/nope", json={}).status_code == 404
    assert client.put(f"{API}/domains/employment", json={"authoritative": ["payroll"]}).status_code == 422
    assert client.put(f"{API}/domains/employment", json={"colour": "blue"}).status_code == 422
