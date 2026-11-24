"""Findings: what needs someone's attention, routed to its owner (issue #67, step 2d)."""

from __future__ import annotations

import test_decide
from test_decide import API, DANA, LEE, load_source

ctx = test_decide.ctx  # the procure-to-pay example, sign-in off


def found(client, **q):
    r = client.get(f"{API}/findings", params=q)
    assert r.status_code == 200, r.text
    return r.json()["findings"]


def test_a_leaver_with_access_goes_to_the_system_owner(ctx):
    client, _, _ = ctx
    load_source(client, "workday", "hr", [LEE])  # Dana has left HR
    load_source(client, "entra", "iam", [DANA, LEE])  # her account is still enabled
    client.put(f"{API}/sources/entra", json={"category": "iam", "ownerRole": "Controller"})
    got = found(client, kind="access-without-employment")
    assert [(f["subject"]["id"], f["source"], f["severity"]) for f in got] == [
        ("DanaExample", "entra", "high")
    ]
    assert got[0]["ownerRole"] == "Controller"
    assert got[0]["routedTo"] == [{"id": "LeeExample", "name": "Lee Example"}]  # Lee fills Controller
    assert found(client, ownerRole="Controller", kind="access-without-employment") == got
    assert not found(client, ownerRole="Nobody", kind="access-without-employment")


def test_contractors_are_not_leavers(ctx):
    client, _, _ = ctx
    load_source(client, "workday", "hr", [DANA, LEE])
    load_source(client, "entra", "iam", [DANA, LEE, {"id": "sam.contractor", "name": "Sam Contractor"}])
    assert not found(client, kind="access-without-employment")


def test_employed_without_an_account(ctx):
    client, _, _ = ctx
    load_source(client, "workday", "hr", [DANA, LEE])
    load_source(client, "entra", "iam", [LEE])
    got = found(client, kind="employed-without-access")
    assert [(f["subject"]["id"], f["severity"]) for f in got] == [("DanaExample", "info")]


def test_no_deciding_source_is_a_finding(ctx):
    client, _, _ = ctx
    assert any(f["about"]["domain"] == "employment" for f in found(client, kind="domain-without-authority"))
    load_source(client, "workday", "hr", [DANA, LEE])
    assert not any(
        f["about"]["domain"] == "employment" for f in found(client, kind="domain-without-authority")
    )


def _authority(client, source, category, limit, **reg):
    assert client.put(f"{API}/sources/{source}", json={"category": category, **reg}).status_code == 200, (
        source
    )
    base = next(
        i["entity"]
        for i in client.get(f"{API}/examples/procure-to-pay").json()["items"]
        if i["entity"]["id"] == "AssistantApprovalAuthority"
    )
    cs = client.post(f"{API}/changesets", json={"source": source, "mode": "merge"}).json()["id"]
    client.post(
        f"{API}/changesets/{cs}/items",
        json={"items": [{"type": "authorities", "entity": {**base, "limitAmount": limit}}]},
    )
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.text


def test_the_workflow_out_of_sync_with_policy(ctx):
    client, _, _ = ctx
    _authority(client, "workbook", "governance", "5000")
    _authority(client, "ap", "erp", "3000", ownerRole="Controller")
    got = found(client, kind="out-of-sync")
    assert len(got) == 1
    f = got[0]
    assert f["subject"]["id"] == "AssistantApprovalAuthority" and f["source"] == "ap"
    assert f["ownerRole"] == "Controller" and f["about"]["field"] == "limitAmount"
    assert float(f["about"]["inForce"]) == 3000 and float(f["about"]["otherValue"]) == 5000
    _authority(client, "ap", "erp", "5000.00", ownerRole="Controller")  # back in sync (same amount)
    assert not found(client, kind="out-of-sync")


def test_a_derived_source_out_of_sync_with_its_origin(ctx):
    client, _, _ = ctx
    _authority(client, "levels", "hr", "5000")  # AP limits are meant to follow these levels
    r = client.put(f"{API}/sources/ap", json={"category": "erp", "derivedFrom": {"nope": "levels"}})
    assert r.status_code == 422
    _authority(
        client, "ap", "erp", "4000", ownerRole="ApManager", derivedFrom={"authority-in-force": "levels"}
    )
    assert client.get(f"{API}/sources").status_code == 200
    got = found(client, kind="out-of-sync")
    assert [(f["source"], f["ownerRole"], f["about"]["derivedFrom"]) for f in got] == [
        ("ap", "ApManager", "levels")
    ]


def test_a_dropped_person_still_named(ctx):
    client, _, _ = ctx
    pat = {"id": "PatExample", "name": "Pat Example", "employeeId": "E-0077"}  # not in the example model
    load_source(client, "workday", "hr", [DANA, LEE, pat])
    load_source(client, "entra", "iam", [{**DANA, "reportsTo": "PatExample"}, LEE])
    load_source(
        client, "workday", "hr", [DANA, LEE]
    )  # Pat leaves; Entra ID still names Pat as Dana's manager
    got = found(client, kind="ended-reference")
    assert [(f["subject"]["id"], f["about"]["referrer"]["field"]) for f in got] == [
        ("PatExample", "reportsTo")
    ]
    assert got[0]["source"] == "entra"


def test_connectors_report_what_they_find(ctx):
    client, _, _ = ctx
    client.put(f"{API}/sources/erp", json={"category": "erp", "ownerRole": "Controller"})
    body = [
        {
            "kind": "self-approved-po",
            "severity": "high",
            "detail": "PO-1 raised and approved by Dana",
            "subject": {"type": "people", "id": "DanaExample"},
            "about": {"po": "PO-1"},
        }
    ]
    r = client.put(f"{API}/findings/reported/erp", json=body)
    assert r.status_code == 200 and r.json()["findings"] == 1
    got = found(client, kind="self-approved-po")
    assert got[0]["about"] == {"po": "PO-1"} and got[0]["ownerRole"] == "Controller" and got[0]["reportedAt"]
    assert (
        client.put(f"{API}/findings/reported/erp", json=[]).status_code == 200
    )  # each run replaces the last
    assert not found(client, kind="self-approved-po")
    assert (
        client.put(f"{API}/findings/reported/erp", json=[{"kind": "Bad Kind", "detail": "x"}]).status_code
        == 422
    )
    assert (
        client.put(
            f"{API}/findings/reported/erp", json=[{"kind": "x-y", "detail": "x", "severity": "dire"}]
        ).status_code
        == 422
    )
    assert client.get(f"{API}/findings", params={"severity": "dire"}).status_code == 422


def test_an_owner_who_has_left(ctx):
    client, _, _ = ctx
    pat = {"id": "PatExample", "name": "Pat Example", "rolesFilled": "Erp.BusinessOwner"}
    assert client.put(f"{API}/sources/cmdb", json={"category": "cmdb"}).status_code == 200
    cs = client.post(f"{API}/changesets", json={"source": "cmdb"}).json()["id"]
    items = [
        {"type": "roles", "entity": {"id": "Erp.BusinessOwner", "name": "Business owner of the ERP"}},
        {"type": "systems", "entity": {"id": "APM1", "name": "ERP", "businessOwner": "Erp.BusinessOwner"}},
        {"type": "people", "entity": pat},
    ]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert client.post(f"{API}/changesets/{cs}/commit").status_code == 200
    load_source(client, "workday", "hr", [DANA, LEE])  # Pat is not in HR
    got = found(client, kind="owner-has-left")
    assert [(f["subject"]["id"], f["about"]["field"]) for f in got] == [("APM1", "businessOwner")]
    load_source(client, "workday", "hr", [DANA, LEE, {**pat, "employeeId": "E-9"}])  # Pat is employed
    assert not found(client, kind="owner-has-left")


def test_a_source_that_grants_access(ctx):
    client, _, _ = ctx
    load_source(client, "workday", "hr", [LEE])  # Dana has left
    load_source(client, "halo-agents", "cmdb", [DANA])  # her ITSM agent seat
    assert not found(client, kind="access-without-employment")  # a CMDB does not grant access
    r = client.put(f"{API}/sources/halo-agents", json={"category": "cmdb", "grantsAccess": True})
    assert r.status_code == 200 and r.json()["grantsAccess"] is True
    got = found(client, kind="access-without-employment")
    assert [(f["subject"]["id"], f["source"]) for f in got] == [("DanaExample", "halo-agents")]
    assert client.get(f"{API}/sources").json()  # the registry still reads
