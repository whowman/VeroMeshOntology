"""The decision check on the procure-to-pay example.

The invoice assistant acts for Dana (AP manager) and may approve supplier invoices up
to 5,000 USD. Approving affects the duplicate payment rate (owner: Controller; material
above 1,000 USD per invoice) and the on-time payment rate (owner: AP manager; material
above 25 invoices per day).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veromesh import decisions as D
from veromesh import provenance as prov
from veromesh.api import create_app
from veromesh.config import Settings

API = "/api/v1"
DAY = "2026-11-16"  # inside every example authority's validity


@pytest.fixture
def ctx(service):
    log = D.MemoryLog()
    client = TestClient(
        create_app(service, Settings(rdf_store="memory", auth_disabled=True), decision_log=log)
    )
    items = client.get(f"{API}/examples/procure-to-pay").json()["items"]
    cs = client.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert client.post(f"{API}/changesets/{cs}/commit").status_code == 200
    return client, log, service


def attest_basis(client, service, authority="AssistantApprovalAuthority"):
    """Attest every fact the agent's authority rests on (sign-in off: the request names the attester)."""
    for c in prov.authority_basis(service.store, authority)["checks"]:
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
    assert prov.authority_basis(service.store, authority)["confirmed"]


def ask(client, amount=None, action="ApproveSupplierInvoice", currency="USD", on=DAY, **kw):
    body = {"agent": "InvoiceAssistant", "action": action, "onDate": on, **kw}
    if amount is not None:
        body.update(amount=amount, currency=currency)
    r = client.post(f"{API}/decisions/check", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def roles(entries):
    return sorted(e["role"] for e in entries)


def test_refused_until_the_facts_are_attested(ctx):
    client, log, _ = ctx
    d = ask(client, 800)
    assert d["outcome"] == "refuse"
    assert "not all attested" in d["reasons"][0]
    assert d["basis"]["authorityConfirmed"] is False


def test_small_invoice_is_allowed_and_owners_informed(ctx):
    client, log, service = ctx
    attest_basis(client, service)
    d = ask(client, 800)
    assert d["outcome"] == "allow", d["reasons"]
    assert d["principal"] == "DanaExample" and d["authority"] == "AssistantApprovalAuthority"
    assert roles(d["inform"]) == ["ApManager", "Controller"]
    assert d["consult"] == []
    assert any("Within 'Approve supplier invoices up to 5,000 USD'" in r for r in d["reasons"])
    controller = next(i for i in d["inform"] if i["role"] == "Controller")
    assert controller["people"] == [{"id": "LeeExample", "name": "Lee Example"}]
    assert log.verify().ok and log.get(d["id"])["outcome"] == "allow"


def test_material_invoice_consults_the_kpi_owner(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 3000)
    assert d["outcome"] == "consult"
    assert roles(d["consult"]) == ["Controller"]
    assert "3,000 USD against a threshold of 1,000 USD per invoice" in d["consult"][0]["why"][0]
    assert roles(d["inform"]) == ["ApManager"]


def test_above_the_agents_limit_is_refused_and_escalated(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 7000)
    assert d["outcome"] == "refuse"
    assert "7,000 USD is above its limit of 5,000 USD" in d["reasons"][0]
    assert d["escalateTo"]["person"] == "DanaExample"


def test_an_action_the_agent_is_not_registered_for(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 100, action="HoldSupplierInvoice")
    assert d["outcome"] == "refuse" and "not registered" in d["reasons"][0]


def test_an_expired_authority(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 800, on="2027-01-15")
    assert d["outcome"] == "refuse" and "ended on 2026-12-31" in d["reasons"][0]


def test_another_currency(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 800, currency="EUR")
    assert d["outcome"] == "refuse" and "limit is in USD, the amount in EUR" in d["reasons"][0]


def test_per_day_threshold_counts_todays_allowed_decisions(ctx):
    client, log, service = ctx
    attest_basis(client, service)
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    for _ in range(25):
        log.append(
            {"kind": "decision", "subject": "s", "action": "ApproveSupplierInvoice", "outcome": "allow"}
        )
    d = ask(client, 800, on=today)
    if d["outcome"] == "refuse":  # the example's authorities have lapsed: run on a date inside them
        pytest.skip("example authorities not valid today")
    assert d["outcome"] == "consult"
    assert roles(d["consult"]) == ["ApManager"]
    assert "26 today against a threshold of 25 invoices per day" in d["consult"][0]["why"][0]


def test_missing_currency_and_unknown_action(ctx):
    client, _, _ = ctx
    r = client.post(
        f"{API}/decisions/check",
        json={"agent": "InvoiceAssistant", "action": "ApproveSupplierInvoice", "amount": 5},
    )
    assert r.status_code == 400
    r = client.post(f"{API}/decisions/check", json={"agent": "InvoiceAssistant", "action": "Nope"})
    assert r.status_code == 404


def test_every_check_is_logged_in_one_chain(ctx):
    client, log, service = ctx
    ask(client, 800)  # refused: unattested
    attest_basis(client, service)
    for a in (800, 3000, 7000):
        ask(client, a)
    v = log.verify()
    assert v.ok and v.entries == 4
    assert [e["outcome"] for e in log.find(kind="decision")] == ["refuse", "allow", "consult", "refuse"]
