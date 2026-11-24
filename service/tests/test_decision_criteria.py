"""Decision criteria: which objective wins when two conflict, and capability health
(design checkpoint gap 1; the white paper's third kind of grounding)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_decide import API, ask, attest_basis

from veromesh import decisions as D
from veromesh.api import create_app
from veromesh.config import Settings


def make(service, *, wc_priority=2, parent=True, health=None):
    """The procure-to-pay example, plus a working-capital objective that pulls against paying
    suppliers on time, both under one enterprise objective the Controller owns."""
    client = TestClient(
        create_app(service, Settings(rdf_store="memory", auth_disabled=True), decision_log=D.MemoryLog())
    )
    items = client.get(f"{API}/examples/procure-to-pay").json()["items"]
    for i in items:
        e = i["entity"]
        if e.get("id") == "PaySuppliersCorrectly":
            e.update(
                ownerRole="ApManager",
                level="function",
                priority="1",
                conflictsWithObjectives=["PreserveWorkingCapital"],
            )
            if parent:
                e["parentObjective"] = "SoundFinances"
        if e.get("id") == "ApproveSupplierInvoice":
            e["affects"] = [*e["affects"], "DaysPayableOutstanding"]
        if e.get("id") == "OnTimePaymentRate" and health is not None:
            e.update(
                healthOfCapabilities=["AccountsPayable"],
                healthThreshold="95",
                betterWhen="higher",
                currentValue=str(health),
                measuredOn="2026-11-15",
            )
    extra = [
        {"type": "roles", "entity": {"id": "Treasurer", "name": "Treasurer"}},
        {
            "type": "kpis",
            "entity": {
                "id": "DaysPayableOutstanding",
                "name": "Days payable outstanding",
                "definition": "Average days to pay suppliers.",
                "unit": "days",
                "ownerRole": "Treasurer",
                "computedFromDataProducts": ["SupplierPayments"],
            },
        },
        {
            "type": "objectives",
            "entity": {
                "id": "PreserveWorkingCapital",
                "name": "Preserve working capital",
                "measuredByKpis": ["DaysPayableOutstanding"],
                "ownerRole": "Treasurer",
                "level": "function",
                "priority": str(wc_priority),
                **({"parentObjective": "SoundFinances"} if parent else {}),
            },
        },
        {
            "type": "objectives",
            "entity": {
                "id": "SoundFinances",
                "name": "Sound finances",
                "measuredByKpis": ["DuplicatePaymentRate"],
                "ownerRole": "Controller",
                "level": "enterprise",
            },
        },
        {
            "type": "impacts",
            "entity": {
                "action": "ApproveSupplierInvoice",
                "impactOn": "DaysPayableOutstanding",
                "materialityThreshold": "2000",
                "thresholdUnit": "USD per invoice",
            },
        },
    ]
    cs = client.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": [*items, *extra]})
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.text
    attest_basis(client, service)
    return client


def roles(entries):
    return sorted(e["role"] for e in entries)


def test_a_small_tradeoff_informs_both_owners(service):
    client = make(service)
    d = ask(client, 800)
    assert d["outcome"] == "allow"
    t = d["basis"]["tradeoffs"][0]
    assert t["objectives"] == ["PaySuppliersCorrectly", "PreserveWorkingCapital"] and not t["material"]
    assert t["priority"] == "PaySuppliersCorrectly"
    assert t["settledBy"] == {"objective": "SoundFinances", "roles": ["Controller"]}
    assert {"ApManager", "Treasurer"} <= set(roles(d["inform"]))


def test_a_material_tradeoff_consults_both_owners(service):
    client = make(service)
    d = ask(client, 3000)  # within the agent's limit, above the working-capital threshold
    assert d["outcome"] == "consult"
    assert {"ApManager", "Treasurer"} <= set(roles(d["consult"]))
    why = " ".join(w for c in d["consult"] if c["role"] == "Treasurer" for w in c["why"])
    assert "pull against each other" in why and "Pay suppliers accurately and on time has priority" in why


def test_with_no_priority_the_common_parent_s_owner_settles_it(service):
    client = make(service, wc_priority=1)
    d = ask(client, 3000)
    t = d["basis"]["tradeoffs"][0]
    assert t["priority"] is None and "Controller" in roles(d["consult"])
    assert not client.get(f"{API}/findings", params={"kind": "objective-conflict-unsettled"}).json()[
        "findings"
    ]


def test_an_unsettled_conflict_is_a_finding(service):
    client = make(service, wc_priority=1, parent=False)
    got = client.get(f"{API}/findings", params={"kind": "objective-conflict-unsettled"}).json()["findings"]
    assert sorted(f["subject"]["id"] for f in got) == ["PaySuppliersCorrectly", "PreserveWorkingCapital"]
    assert sorted(f["ownerRole"] for f in got) == ["ApManager", "Treasurer"]


def test_a_degraded_capability_consults_its_owner(service):
    client = make(service, health=90)
    d = ask(client, 800)
    assert d["outcome"] == "consult" and "ApManager" in roles(d["consult"])
    assert d["basis"]["degraded"] == [
        {
            "capability": "AccountsPayable",
            "kpi": "OnTimePaymentRate",
            "value": "90",
            "threshold": "95",
            "measuredOn": "2026-11-15",
        }
    ]
    f = client.get(f"{API}/findings", params={"kind": "capability-degraded"}).json()["findings"]
    assert [(x["subject"]["id"], x["ownerRole"], x["severity"]) for x in f] == [
        ("AccountsPayable", "ApManager", "high")
    ]


def test_a_healthy_capability_is_no_reason_to_consult(service):
    client = make(service, health=97)
    d = ask(client, 800)
    assert d["outcome"] == "allow" and "degraded" not in d["basis"]
    assert not client.get(f"{API}/findings", params={"kind": "capability-degraded"}).json()["findings"]


def _validate(client, entity):
    cs = client.post(f"{API}/changesets", json={"source": "bad", "mode": "merge"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": [{"type": "objectives", "entity": entity}]})
    r = client.post(f"{API}/changesets/{cs}/validate").json()
    return " ".join(v["message"] for v in r["violations"] + r.get("errors", []))


def test_the_rules_refuse_nonsense(service):
    client = make(service)
    loop = {"id": "Loop", "name": "Loop", "measuredByKpis": ["OnTimePaymentRate"]}
    assert "cannot conflict with, or be the parent of, itself" in _validate(
        client, {**loop, "parentObjective": "Loop"}
    )
    assert "cannot conflict with" in _validate(client, {**loop, "conflictsWithObjectives": ["Loop"]})
    assert "enterprise, function, capability, process" in _validate(client, {**loop, "level": "galaxy"})
