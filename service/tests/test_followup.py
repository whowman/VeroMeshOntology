"""After a decision: answers and overrides by people, outcomes by the agent, and the measures."""

# The `client` and `ctx` fixtures are imported from the sign-in and decision suites.
# ruff: noqa: F811

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_auth import agent_token, client, h, load_example, reader, steward, token  # noqa: F401
from test_decide import ask, attest_basis, ctx  # noqa: F401

from veromesh import decisions as D
from veromesh import followup as F

API = "/api/v1"


def answer(client, decision_id, verdict, hdr=None, **body):
    return client.post(
        f"{API}/decisions/{decision_id}/overrides", json={"verdict": verdict, **body}, headers=hdr or {}
    )


def outcome(client, decision_id, value, hdr=None):
    return client.post(f"{API}/decisions/{decision_id}/outcome", json={"outcome": value}, headers=hdr or {})


def status(client, decision_id, hdr=None):
    return client.get(f"{API}/decisions/{decision_id}/status", headers=hdr or {})


# ---------------------------------------------------------------- the flows (sign-in off)


def test_a_consultation_answered_then_carried_out(ctx):
    client, log, service = ctx
    attest_basis(client, service)
    d = ask(client, 3000)
    st = status(client, d["id"]).json()
    assert st["status"] == "pending" and st["waitingFor"] == ["Controller"] and not st["mayAct"]
    assert outcome(client, d["id"], "done").status_code == 403  # not before the Controller answers
    r = answer(client, d["id"], "approved", person="LeeExample", note="checked the PO")
    assert r.status_code == 201 and r.json()["status"]["mayAct"]
    r = outcome(client, d["id"], "done")
    assert r.status_code == 201 and r.json()["status"]["done"] == "done"
    assert outcome(client, d["id"], "done").status_code == 422  # once
    assert log.verify().ok


def test_a_rejected_consultation_stops_the_agent(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 3000)
    assert answer(client, d["id"], "rejected", person="LeeExample").json()["status"]["status"] == "rejected"
    assert outcome(client, d["id"], "done").status_code == 403
    assert outcome(client, d["id"], "not_done").status_code == 201


def test_the_principal_approves_above_the_limit(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 300_000)  # above every authority: refused, escalated
    assert (
        answer(client, d["id"], "rejected", person="DanaExample").status_code == 422
    )  # a refusal only gets approved
    st = answer(client, d["id"], "approved", person="DanaExample").json()["status"]
    assert st["status"] == "approved" and st["mayAct"]


def test_an_allowed_action_can_be_stopped(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 800)
    assert answer(client, d["id"], "approved", person="DanaExample").status_code == 422
    assert answer(client, d["id"], "rejected", person="DanaExample").json()["status"]["status"] == "rejected"


def test_measures(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    allowed = ask(client, 800)
    ask(client, 900)
    c1 = ask(client, 3000)
    c2 = ask(client, 4000)
    ask(client, 2500)  # left pending
    refused = ask(client, 300_000)  # above every authority: refused, escalated
    answer(client, allowed["id"], "rejected", person="DanaExample")
    answer(client, c1["id"], "approved", person="LeeExample")
    answer(client, c2["id"], "rejected", person="LeeExample")
    answer(client, refused["id"], "approved", person="DanaExample")
    m = client.get(f"{API}/decisions/measures").json()
    assert m["decisions"] == 6 and m["byOutcome"] == {"allow": 2, "consult": 3, "refuse": 1}
    assert m["overrideRate"] == {"value": round(2 / 6, 4), "overruled": 2, "of": 6}
    assert m["consultationPrecision"] == {"value": 0.5, "rejected": 1, "answered": 2, "pending": 1}
    assert m["delegationExtended"]["withinDelegation"] == round(2 / 6, 4)
    assert m["delegationExtended"]["escalationsApproved"] == 1


def test_consulted_actions_count_towards_the_day_once_done():
    log = D.MemoryLog()
    today = datetime.now(UTC).date()
    base = {"kind": "decision", "subject": "s", "action": "Pay"}
    a = log.append({**base, "outcome": "allow"})  # allowed, no outcome yet: counts
    c = log.append({**base, "outcome": "consult", "consult": [{"role": "R"}]})
    assert F.done_today(log, "Pay", today) == 1
    log.append(
        {
            "kind": "override",
            "refersTo": c["id"],
            "subject": "p",
            "outcome": "approved",
            "basis": {"role": "R"},
        }
    )
    assert F.done_today(log, "Pay", today) == 1  # approved but not done yet
    log.append({"kind": "outcome", "refersTo": c["id"], "subject": "s", "outcome": "done"})
    assert F.done_today(log, "Pay", today) == 2
    log.append({"kind": "outcome", "refersTo": a["id"], "subject": "s", "outcome": "not_done"})
    assert F.done_today(log, "Pay", today) == 1


# ---------------------------------------------------------------- who may (signed in)


@pytest.fixture
def signed(client):
    load_example(client)
    log = D.MemoryLog()
    client.app.state.decision_log = log
    consult = log.append(
        {
            "kind": "decision",
            "subject": "11111111-2222-3333-4444-555555555555",
            "agent": "InvoiceAssistant",
            "principal": "DanaExample",
            "action": "ApproveSupplierInvoice",
            "outcome": "consult",
            "consult": [
                {"role": "Controller", "roleName": "Financial controller", "people": [], "why": ["x"]}
            ],
            "basis": {"inform": [{"role": "ApManager"}]},
        }
    )
    return client, log, consult


def lee() -> dict:
    return h(token(roles=("VeroMesh.Reader",), email="lee@example.com"))


def test_only_a_consulted_owner_answers(signed):
    client, log, d = signed
    # Lee has no email in the example; give him one for this test
    from veromesh import changesets

    svc = client.app.state.service
    changesets.apply(
        svc.store,
        source="hr",
        items=[
            {
                "type": "people",
                "op": "upsert",
                "entity": {
                    "id": "LeeExample",
                    "name": "Lee Example",
                    "email": "lee@example.com",
                    "rolesFilled": ["Controller", "ErpPlatformLead"],
                },
            }
        ],
        base=client.app.state.settings.instance_base,
        ontology_dir=client.app.state.settings.ontology_dir,
    )
    r = answer(client, d["id"], "approved", reader(email="dana@example.com"))
    assert r.status_code == 403 and "consulted roles" in r.json()["detail"]
    r = answer(client, d["id"], "approved", lee())
    assert r.status_code == 201 and r.json()["status"]["mayAct"]
    assert r.json()["entry"]["basis"] == {"by": "LeeExample", "role": "Controller"}


def test_services_cannot_answer_and_only_the_agent_reports(signed):
    client, _, d = signed
    assert answer(client, d["id"], "approved", agent_token()).status_code == 403
    r = outcome(client, d["id"], "done", agent_token(client_id="not-registered"))
    assert r.status_code == 403


def test_who_sees_the_status(signed):
    client, _, d = signed
    assert status(client, d["id"], agent_token()).status_code == 200  # the agent
    assert status(client, d["id"], reader(email="dana@example.com")).status_code == 200  # the principal
    assert status(client, d["id"], steward()).status_code == 200
    assert (
        status(client, d["id"], h(token(roles=("VeroMesh.Reader",), email="nobody@example.com"))).status_code
        == 403
    )


def test_measures_are_for_stewards(signed):
    client, _, _ = signed
    assert (
        client.get(f"{API}/decisions/measures", headers=reader(email="dana@example.com")).status_code == 403
    )
    assert client.get(f"{API}/decisions/measures", headers=steward()).json()["decisions"] == 1
