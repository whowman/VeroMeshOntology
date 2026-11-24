"""The white paper's controls (design checkpoint, 24 November): unknown impact means consult,
approved AI use cases gate agents, and every read by an agent is logged with its purpose."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_auth import KEY, h, settings, token
from test_decide import API, ask, attest_basis
from test_mcp import agent_app, call, load_example

from veromesh import auth
from veromesh import decisions as D
from veromesh.api import create_app
from veromesh.config import Settings


def make(service, process="InvoiceApproval", **kw):
    log = D.MemoryLog()
    client = TestClient(
        create_app(service, Settings(rdf_store="memory", auth_disabled=True, **kw), decision_log=log)
    )
    items = client.get(f"{API}/examples/procure-to-pay").json()["items"]
    archive = {
        "type": "actions",
        "entity": {
            "id": "ArchiveSupplierInvoice",
            "name": "Archive supplier invoice",
            **({"partOfProcess": process} if process else {}),
        },
    }
    for i in items:  # the agent may also archive invoices; no impact is mapped for that
        e = i["entity"]
        if e.get("id") == "InvoiceAssistant":
            e["performsActions"] = [*e["performsActions"], "ArchiveSupplierInvoice"]
        if i["type"] == "authorities" and "ApproveSupplierInvoice" in e.get("actionsCovered", []):
            e["actionsCovered"] = [*e["actionsCovered"], "ArchiveSupplierInvoice"]
    cs = client.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": [*items, archive]})
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.text
    attest_basis(client, service)
    return client, log


def test_unknown_impact_means_consult_the_process_owner(service):
    client, _ = make(service)
    d = ask(client, action="ArchiveSupplierInvoice")
    assert d["outcome"] == "consult"
    assert [c["role"] for c in d["consult"]] == ["ApManager"]
    assert "impact is unknown" in d["consult"][0]["why"][0]
    assert ask(client, 800)["outcome"] == "allow"  # a mapped action below its thresholds


def test_with_no_process_the_capability_owner_is_consulted(service):
    client, _ = make(service, process=None)
    d = ask(client, action="ArchiveSupplierInvoice")
    cap = client.get(f"{API}/capabilities/AccountsPayable").json()
    owners = sorted({cap[k] for k in ("ownedBy", "ownerRole", "owner") if cap.get(k)})
    assert d["outcome"] == "consult" and [c["role"] for c in d["consult"]] == owners


def test_unknown_impact_may_be_allowed_by_setting(service):
    client, _ = make(service, decision_unmapped_impact="allow")
    assert ask(client, action="ArchiveSupplierInvoice")["outcome"] == "allow"


def test_approved_use_cases_gate_agents(service):
    client, _ = make(service, decision_requires_use_case=True)
    d = ask(client, 800)
    assert d["outcome"] == "allow" and d["basis"]["useCase"] == "InvoiceAssistantUse"
    assert any("weekly sample" in r for r in d["reasons"])  # the board's conditions travel with it
    d = ask(client, action="ArchiveSupplierInvoice")
    assert d["outcome"] == "refuse" and "does not allow ArchiveSupplierInvoice" in d["reasons"][0]
    uc = next(
        x["entity"]
        for x in client.get(f"{API}/examples/procure-to-pay").json()["items"]
        if x["entity"].get("id") == "InvoiceAssistantUse"
    )
    cs = client.post(f"{API}/changesets", json={"source": "p2p", "mode": "merge"}).json()["id"]
    client.post(
        f"{API}/changesets/{cs}/items",
        json={"items": [{"type": "ai-use-cases", "entity": {**uc, "status": "suspended"}}]},
    )
    assert client.post(f"{API}/changesets/{cs}/commit").status_code == 200
    d = ask(client, 800)
    assert d["outcome"] == "refuse" and "is suspended" in d["reasons"][0]


def test_reads_are_logged_with_their_purpose(service):
    client, log = make(service, query_log="all")
    r = client.get(f"{API}/people/DanaExample", headers={"X-VeroMesh-Purpose": "who approves INV-1"})
    assert r.status_code == 200
    q = log.find(kind="query")[-1]
    assert q["query"] == f"{API}/people/DanaExample" and q["purpose"] == "who approves INV-1"
    client.get(f"{API}/kpis")
    assert "purpose" not in log.find(kind="query")[-1]  # answered, and logged without one
    assert log.verify().ok


def test_people_are_not_logged_by_default(service):
    client, log = make(service)
    client.get(f"{API}/people/DanaExample")
    assert not log.find(kind="query")  # sign-in off acts as an administrator, not an agent


@pytest.fixture
def mcp(service):
    log = D.MemoryLog()
    app = create_app(service, settings(), decision_log=log)
    app.state.verifier = auth.Verifier(settings(), key_for=lambda _t: KEY.public_key())
    with TestClient(app) as client:
        load_example(client)
        yield client, log


def test_an_agents_mcp_reads_are_logged(mcp):
    client, log = mcp
    _, err = call(
        client,
        agent_app(),
        "who_owns",
        type="kpis",
        id="OnTimePaymentRate",
        purpose="before holding an invoice",
    )
    assert not err
    q = log.find(kind="query")[-1]
    assert q["agent"] == "InvoiceAssistant" and q["principal"] == "DanaExample"
    assert (
        q["query"] == "mcp:who_owns type_=kpis rid=OnTimePaymentRate"
        and q["purpose"] == "before holding an invoice"
    )
    r = client.get(f"{API}/kpis", headers={**agent_app(), "X-VeroMesh-Purpose": "listing measures"})
    assert r.status_code == 200 and log.find(kind="query")[-1]["purpose"] == "listing measures"
    person = h(token(roles=("VeroMesh.Reader",), email="lee@example.com"))
    n = len(log.find(kind="query"))
    client.get(f"{API}/kpis", headers=person)
    assert len(log.find(kind="query")) == n  # a person reading is not logged by default
