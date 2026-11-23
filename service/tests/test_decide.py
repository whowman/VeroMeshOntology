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


def test_above_the_agents_limit_consults_the_next_authority_up(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 7000)  # the agent may approve 5,000; the AP manager 50,000; the controller 250,000
    assert d["outcome"] == "consult"
    ap = next(c for c in d["consult"] if c["role"] == "ApManager")
    assert ap["people"] == [{"id": "DanaExample", "name": "Dana Example"}]
    assert "7,000 USD is above its limit of 5,000 USD" in ap["why"][0]
    assert "within 'Approve supplier invoices up to 50,000 USD'" in ap["why"][0]
    assert d["basis"]["nextAuthority"] == "ApApprovalAuthority" and d["escalateTo"] is None
    assert any("Consult the next authority up: " in r for r in d["reasons"])


def test_the_next_authority_up_is_the_smallest_that_covers_the_amount(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 120_000)  # above the AP manager's 50,000: the controller
    assert d["outcome"] == "consult" and d["basis"]["nextAuthority"] == "ControllerAuthority"
    assert "Controller" in roles(d["consult"]) and "ApManager" not in roles(d["consult"])


def test_above_every_authority_is_refused_and_escalated(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 300_000)  # above the controller's 250,000: nobody may approve it
    assert d["outcome"] == "refuse"
    assert "300,000 USD is above its limit of 5,000 USD" in d["reasons"][0]
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
    for a in (800, 3000, 300_000):
        ask(client, a)
    v = log.verify()
    attested = log.find(kind="attestation")  # the facts attested are on the same chain
    assert v.ok and attested and v.entries == 4 + len(attested)
    assert [e["outcome"] for e in log.find(kind="decision")] == ["refuse", "allow", "consult", "refuse"]


def test_lookups_find_what_a_scan_finds(ctx):
    """The decision check looks entities up by field; each lookup finds exactly what
    scanning every entity of the type finds, in the same order."""
    from veromesh.decide import Model

    _, _, service = ctx
    m = Model(service.store, Settings().instance_base)
    for type_, field in (
        ("authorities", "holder"),
        ("co-approvals", "authority"),
        ("impacts", "action"),
        ("people", "rolesFilled"),
    ):
        values = {
            v
            for e in m.all(type_)
            for v in (e.get(field) if isinstance(e.get(field), list) else [e.get(field)])
            if v
        }
        assert values, (type_, field)
        for v in values | {"NoSuchValue"}:
            scan = [
                e
                for e in m.all(type_)
                if (v in e.get(field, []) if isinstance(e.get(field), list) else e.get(field) == v)
            ]
            assert m.by(type_, field, v) == scan, (type_, field, v)
        both = sorted(values)[:2]
        scan = [
            e
            for e in m.all(type_)
            if any(
                (b in e.get(field, []) if isinstance(e.get(field), list) else e.get(field) == b) for b in both
            )
        ]
        assert m.by(type_, field, *both) == scan


# Employment decided by the authoritative source (issue #67, decided 23 November)

DANA = {"id": "DanaExample", "name": "Dana Example", "employeeId": "E-1001", "rolesFilled": "ApManager"}
LEE = {"id": "LeeExample", "name": "Lee Example", "employeeId": "E-0042", "rolesFilled": ["Controller"]}


def load_source(client, name, category, people):
    assert client.put(f"{API}/sources/{name}", json={"category": category}).status_code == 200
    cs = client.post(f"{API}/changesets", json={"source": name}).json()["id"]
    items = [{"type": "people", "entity": p} for p in people]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.text


def test_a_principal_hr_no_longer_lists_is_not_employed(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    load_source(client, "workday", "hr", [LEE])  # HR lists Lee; Dana has left
    d = ask(client, 800)
    assert d["outcome"] == "refuse" and d["basis"]["principalStatus"] == "ended"
    assert "Dana Example, is no longer employed" in d["reasons"][0]


def test_a_principal_hr_lists_is_employed(ctx):
    client, _, service = ctx
    load_source(client, "workday", "hr", [DANA, LEE])
    attest_basis(client, service)  # HR now supplies Dana's facts: owners confirm them
    d = ask(client, 800)
    assert d["outcome"] == "allow" and d["basis"]["principalStatus"] == "employed"


def test_a_suspended_principal_s_agent_consults_the_next_authority_up(ctx):
    client, _, service = ctx
    load_source(client, "workday", "hr", [DANA, LEE])
    load_source(client, "entra", "iam", [LEE])  # Dana's account is disabled: Entra ID leaves her out
    attest_basis(client, service)
    d = ask(client, 800)
    assert d["basis"]["principalStatus"] == "no-access" and d["outcome"] == "consult"
    # Dana's own authority (AP manager) is skipped: the controller is next.
    assert d["basis"]["nextAuthority"] == "ControllerAuthority"
    first = d["consult"][0]
    assert first["role"] == "Controller" and "account is disabled" in first["why"][0]


def test_with_no_hr_source_there_is_no_employment_rule(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    d = ask(client, 800)
    assert d["outcome"] == "allow" and d["basis"]["principalStatus"] == "unknown"


def test_a_contractor_only_entra_lists_is_employed(ctx):
    from veromesh.decide import Model

    client, _, service = ctx
    load_source(client, "workday", "hr", [LEE])
    load_source(client, "entra", "iam", [LEE, {"id": "kim.vo", "name": "Kim Vo"}])
    m = Model(service.store, Settings().instance_base)
    assert m.employment("kim.vo") == "contractor"  # no employee ID: Entra ID decides
    assert m.employment("LeeExample") == "employed"
    assert m.employment("DanaExample") == "ended"
