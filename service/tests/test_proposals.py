"""The proposal step: accountability changes wait for their owner's attestation (step 6a)."""

from __future__ import annotations

import pytest
import test_decide
from test_decide import API

ctx = test_decide.ctx  # the procure-to-pay example in source p2p, sign-in off


def example(client, rid):
    return next(
        i
        for i in client.get(f"{API}/examples/procure-to-pay").json()["items"]
        if i["entity"].get("id") == rid
    )


def upsert(client, type_, entity, source="p2p"):
    cs = client.post(f"{API}/changesets", json={"source": source, "mode": "merge"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": [{"type": type_, "entity": entity}]})
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.text


def change(client, rid, **fields):
    it = example(client, rid)
    upsert(client, it["type"], {**it["entity"], **fields})


def settle(client, type_, rid, field, verdict="attested", attester="LeeExample", role="Controller"):
    return client.post(
        f"{API}/proposals/{type_}/{rid}/{field}",
        json={"verdict": verdict, "attester": attester, "role": role},
    )


def test_a_new_manager_waits_for_attestation(ctx):
    client, log, _ = ctx
    pat = {"id": "PatExample", "name": "Pat Example", "rolesFilled": ["Controller"]}
    upsert(client, "people", pat)  # a new person is not a change: in force at once
    assert client.get(f"{API}/people/PatExample").json()["name"] == "Pat Example"
    assert client.get(f"{API}/proposals").json() == []

    change(client, "DanaExample", reportsTo="PatExample")
    assert client.get(f"{API}/people/DanaExample").json()["reportsTo"] == "LeeExample"  # still in force
    (p,) = client.get(f"{API}/proposals").json()
    assert (p["type"], p["id"], p["field"], p["source"]) == ("people", "DanaExample", "reportsTo", "p2p")
    assert p["proposed"] == "PatExample" and p["inForce"] == "LeeExample"
    assert p["attestBy"] == ["PatExample", "LeeExample"]  # the new manager, then the one in force

    r = settle(client, "people", "DanaExample", "reportsTo", attester="PatExample")
    assert r.status_code == 201, r.text
    assert r.json()["inForce"] == "PatExample" and r.json()["warnings"] == []
    assert client.get(f"{API}/people/DanaExample").json()["reportsTo"] == "PatExample"
    assert client.get(f"{API}/proposals").json() == []
    assert any(e["kind"] == "attestation" and e["entityId"] == "DanaExample" for e in log.find())

    change(client, "DanaExample", reportsTo="PatExample", name="Dana E.")  # not an accountability change
    assert client.get(f"{API}/people/DanaExample").json()["reportsTo"] == "PatExample"
    assert client.get(f"{API}/proposals").json() == []


def test_a_dispute_keeps_the_value_in_force(ctx):
    client, _, _ = ctx
    upsert(client, "people", {"id": "PatExample", "name": "Pat Example"})
    change(client, "DanaExample", reportsTo="PatExample")
    assert settle(client, "people", "DanaExample", "reportsTo", "disputed").status_code == 201
    assert client.get(f"{API}/people/DanaExample").json()["reportsTo"] == "LeeExample"
    assert client.get(f"{API}/proposals").json() == []  # settled: no longer waiting
    change(client, "DanaExample", reportsTo="LeeExample")  # the source goes back: nothing held
    change(client, "DanaExample", reportsTo="PatExample")  # and proposes it again: still disputed
    assert client.get(f"{API}/people/DanaExample").json()["reportsTo"] == "LeeExample"


def test_a_role_removed_goes_at_once_a_role_added_waits(ctx):
    client, _, _ = ctx
    change(client, "LeeExample", rolesFilled=["Controller", "ApManager"])
    lee = client.get(f"{API}/people/LeeExample").json()
    assert lee["rolesFilled"] == ["Controller"]  # ErpPlatformLead and AiReviewBoard gone; ApManager waits
    (p,) = client.get(f"{API}/proposals").json()
    assert p["field"] == "rolesFilled" and p["inForce"] == ["Controller"]
    assert settle(client, "people", "LeeExample", "rolesFilled").status_code == 201
    assert sorted(client.get(f"{API}/people/LeeExample").json()["rolesFilled"]) == ["ApManager", "Controller"]


def test_a_limit_lowered_at_once_raised_on_attestation(ctx):
    client, _, _ = ctx
    get = lambda: float(client.get(f"{API}/authorities/AssistantApprovalAuthority").json()["limitAmount"])  # noqa: E731
    change(client, "AssistantApprovalAuthority", limitAmount="1000")
    assert get() == 1000 and client.get(f"{API}/proposals").json() == []  # narrowing: no wait
    change(client, "AssistantApprovalAuthority", limitAmount="7500")
    assert get() == 1000
    (p,) = client.get(f"{API}/proposals").json()
    assert p["attestBy"] == ["DanaExample"]  # who holds the parent authority it is bounded by
    r = settle(
        client,
        "authorities",
        "AssistantApprovalAuthority",
        "limitAmount",
        attester="DanaExample",
        role="ApManager",
    )
    assert r.status_code == 201, r.text
    assert get() == 7500


def test_accepting_a_change_that_breaks_the_model_is_refused(ctx):
    client, _, _ = ctx
    change(client, "AssistantApprovalAuthority", limitAmount="900000")  # above its parent's 50,000
    assert float(client.get(f"{API}/authorities/AssistantApprovalAuthority").json()["limitAmount"]) == 5000
    r = settle(
        client,
        "authorities",
        "AssistantApprovalAuthority",
        "limitAmount",
        attester="DanaExample",
        role="ApManager",
    )
    assert r.status_code == 422, r.text
    assert r.json()["report"]["violations"]
    assert [p["field"] for p in client.get(f"{API}/proposals").json()] == ["limitAmount"]  # still waiting


def test_an_agents_new_principal_is_asked(ctx):
    client, _, _ = ctx
    change(client, "InvoiceAssistant", actsFor="LeeExample")
    assert client.get(f"{API}/agents/InvoiceAssistant").json()["actsFor"] == "DanaExample"
    (p,) = client.get(f"{API}/proposals").json()
    assert p["attestBy"] == ["LeeExample"]


def test_the_step_can_be_turned_off(ctx, monkeypatch):
    client, _, _ = ctx
    monkeypatch.setenv("VEROMESH_PROPOSALS", "off")
    upsert(client, "people", {"id": "PatExample", "name": "Pat Example"})
    change(client, "DanaExample", reportsTo="PatExample")
    assert client.get(f"{API}/people/DanaExample").json()["reportsTo"] == "PatExample"


def test_no_proposal_to_settle(ctx):
    client, _, _ = ctx
    assert settle(client, "people", "DanaExample", "reportsTo").status_code == 404


@pytest.mark.parametrize("verdict", ["maybe"])
def test_a_verdict_is_attested_or_disputed(ctx, verdict):
    client, _, _ = ctx
    upsert(client, "people", {"id": "PatExample", "name": "Pat Example"})
    change(client, "DanaExample", reportsTo="PatExample")
    assert settle(client, "people", "DanaExample", "reportsTo", verdict).status_code in (400, 422)


def test_each_publish_has_a_model_version_quoted_in_decisions(ctx):
    client, log, _ = ctx
    upsert(client, "people", {"id": "PatExample", "name": "Pat Example"})
    published = client.get(f"{API}/model/versions").json()
    last = published["publishes"][-1]
    assert last["kind"] == "publish" and last["source"] == "p2p" and last["changeset"]
    assert last["modelVersion"] == published["current"]
    d = test_decide.ask(client, 800)
    assert d["basis"]["modelVersion"] == published["current"]
    change(client, "DanaExample", reportsTo="PatExample")  # a held change still publishes (the claim)
    r = settle(client, "people", "DanaExample", "reportsTo", attester="PatExample")
    assert r.json()["modelVersion"] == client.get(f"{API}/model/versions").json()["current"]
    assert test_decide.ask(client, 800)["basis"]["modelVersion"] == r.json()["modelVersion"]
