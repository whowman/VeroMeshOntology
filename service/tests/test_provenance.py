"""Provenance and governance of facts: claims, history, precedence, attestation."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from veromesh import provenance as P
from veromesh.api import create_app
from veromesh.config import Settings

API = "/api/v1"


@pytest.fixture
def client(service) -> TestClient:
    c = TestClient(create_app(service, Settings(rdf_store="memory")))
    items = c.get(f"{API}/examples/procure-to-pay").json()["items"]
    cs = c.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    c.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert c.post(f"{API}/changesets/{cs}/commit").status_code == 200
    return c


def load(client, source, items, mode="merge"):
    cs = client.post(f"{API}/changesets", json={"source": source, "mode": mode}).json()["id"]
    r = client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert r.status_code == 200, r.json()
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.json()
    return r.json()


def attest(client, type_, rid, attester="LeeExample", role="Controller", **kw):
    r = client.post(
        f"{API}/{type_}/{rid}/attestations",
        json={"verdict": "attested", "attester": attester, "role": role, **kw},
    )
    assert r.status_code == 201, r.json()
    return r.json()


DANA = {"id": "DanaExample", "name": "Dana Example", "employeeId": "E-1001", "rolesFilled": ["ApManager"]}


def test_every_fact_has_a_claim_and_history_is_kept(client):
    p = client.get(f"{API}/roles/ApManager/provenance").json()
    assert p["fields"]["name"]["source"] == "p2p" and p["fields"]["name"]["status"] == "unconfirmed"
    first = p["claims"][0]
    assert first["observedAt"] and first["changeset"]

    time.sleep(1.1)
    r = client.put(f"{API}/roles/ApManager", params={"source": "p2p"}, json={"name": "AP manager"})
    assert r.status_code == 200, r.json()
    claims = r.json()["claims"]
    assert claims["added"] == 1 and claims["superseded"] == 1  # only the changed role
    history = client.get(f"{API}/roles/ApManager/provenance").json()["history"]
    assert [h["values"]["name"] for h in history] == ["Accounts payable manager", "AP manager"]
    assert history[0]["supersededAt"] and not history[1]["supersededAt"]

    then = client.get(f"{API}/roles/ApManager", params={"asOf": first["recordedAt"]}).json()
    assert then["name"] == "Accounts payable manager"
    assert client.get(f"{API}/roles/ApManager").json()["name"] == "AP manager"


def test_unchanged_reload_keeps_claims(client):
    items = client.get(f"{API}/examples/procure-to-pay").json()["items"]
    out = load(client, "p2p", items, mode="replace")
    assert out["claims"]["added"] == 0 and out["claims"]["superseded"] == 0


def test_authoritative_source_wins_and_disagreement_is_a_conflict(client):
    assert (
        client.put(f"{API}/sources/hr", json={"category": "hr", "ownerRole": "Controller"}).status_code == 200
    )
    load(client, "hr", [{"type": "people", "entity": {**DANA, "email": "dana.example@corp.example"}}])

    # Two sources, two emails: no longer a model violation, the HR value wins.
    assert client.get(f"{API}/people/DanaExample").json()["email"] == "dana.example@corp.example"
    assert client.get(f"{API}/people/DanaExample").json()["sources"] == ["hr", "p2p"]
    conflicts = client.get(f"{API}/conflicts", params={"type": "people"}).json()
    assert [(c["id"], c["field"], c["winner"]) for c in conflicts] == [("DanaExample", "email", "hr")]
    assert {c["source"]: c["value"] for c in conflicts[0]["claims"]} == {
        "hr": "dana.example@corp.example",
        "p2p": "dana@example.com",
    }
    # Fields only the spreadsheet states still come through.
    assert client.get(f"{API}/people/DanaExample").json()["reportsTo"] == "LeeExample"
    assert next(s for s in client.get(f"{API}/sources").json() if s["source"] == "hr")["category"] == "hr"


def test_a_disputed_value_is_set_aside(client):
    client.put(f"{API}/sources/hr", json={"category": "hr"})
    load(client, "hr", [{"type": "people", "entity": {**DANA, "email": "wrong@corp.example"}}])
    r = client.post(
        f"{API}/people/DanaExample/attestations",
        json={
            "verdict": "disputed",
            "attester": "DanaExample",
            "role": "ApManager",
            "fields": ["email"],
            "note": "Not my address",
        },
    )
    assert r.status_code == 201, r.json()
    assert client.get(f"{API}/people/DanaExample").json()["email"] == "dana@example.com"
    claims = client.get(f"{API}/people/DanaExample/provenance").json()["fields"]["email"]["claims"]
    assert {c["source"]: c["status"] for c in claims} == {"hr": "disputed", "p2p": "unconfirmed"}


def test_attestation_holds_until_the_value_changes(client):
    out = attest(client, "kpis", "OnTimePaymentRate", attester="DanaExample", role="ApManager")
    assert out["warnings"] == []
    fields = client.get(f"{API}/kpis/OnTimePaymentRate/provenance").json()["fields"]
    assert {f["status"] for f in fields.values()} == {"attested"}
    assert fields["ownerRole"]["attestation"]["expiresAt"]  # 180 days for ownership

    client.put(
        f"{API}/kpis/OnTimePaymentRate",
        params={"source": "p2p"},
        json={**{k: v["value"] for k, v in fields.items()}, "unit": "share"},
    )
    fields = client.get(f"{API}/kpis/OnTimePaymentRate/provenance").json()["fields"]
    assert fields["unit"]["status"] == "unconfirmed" and fields["name"]["status"] == "attested"
    queue = client.get(f"{API}/attestations", params={"type": "kpis", "ownerRole": "ApManager"}).json()
    assert queue == [
        {
            "type": "kpis",
            "id": "OnTimePaymentRate",
            "name": "On-time payment rate",
            "ownerRole": "ApManager",
            "fields": {"unit": "unconfirmed"},
        }
    ]


def test_attester_must_exist_and_is_warned_when_not_the_owner(client):
    r = client.post(
        f"{API}/kpis/OnTimePaymentRate/attestations",
        json={"verdict": "attested", "attester": "Nobody", "role": "ApManager"},
    )
    assert r.status_code == 400
    out = attest(client, "kpis", "OnTimePaymentRate", attester="DanaExample", role="Controller")
    assert out["warnings"] == [
        "DanaExample does not fill role Controller.",
        "Role Controller is not an owner or steward of kpis 'OnTimePaymentRate' (ApManager).",
    ]


def test_steward_corrections_follow_the_field_policy(client):
    r = client.post(
        f"{API}/systems/Erp/corrections",
        json={"values": {"criticality": "high"}, "attester": "LeeExample", "role": "ErpPlatformLead"},
    )
    assert r.status_code == 201, r.json()
    assert client.get(f"{API}/systems/Erp").json()["criticality"] == "high"
    assert client.get(f"{API}/systems/Erp/provenance").json()["fields"]["criticality"]["status"] == "attested"
    assert any(
        c["id"] == "Erp" and c["field"] == "criticality" for c in client.get(f"{API}/conflicts").json()
    )

    r = client.post(
        f"{API}/people/DanaExample/corrections",
        json={"values": {"email": "d@example.com"}, "attester": "LeeExample", "role": "Controller"},
    )
    assert r.status_code == 400 and "governed by its source system" in r.json()["detail"]


def test_agent_authority_needs_attested_current_facts(client):
    basis = client.get(f"{API}/authorities/ApApprovalAuthority/confirmation").json()
    assert basis["confirmed"] is False and basis["reasons"]
    for type_, rid in [
        ("authorities", "ApApprovalAuthority"),
        ("roles", "ApManager"),
        ("people", "DanaExample"),
        ("kpis", "DuplicatePaymentRate"),
        ("kpis", "OnTimePaymentRate"),
    ]:
        attest(client, type_, rid)
    basis = client.get(f"{API}/authorities/ApApprovalAuthority/confirmation").json()
    assert basis["confirmed"] is True, basis["reasons"]
    assert {c["why"] for c in basis["checks"]} == {
        "the delegated authority",
        "the role holding the authority",
        "a person filling the role",
        "ownership of an affected KPI",
    }


def test_item_times_are_recorded_and_validity_applies(client):
    load(
        client,
        "p2p",
        [
            {
                "type": "roles",
                "entity": {"id": "TempRole", "name": "Temporary role"},
                "observedAt": "2026-10-01T09:00:00Z",
                "validUntil": "2020-12-31",
            }
        ],
    )
    claim = client.get(f"{API}/roles/TempRole/provenance").json()["claims"][0]
    assert claim["observedAt"].startswith("2026-10-01T09:00:00") and claim["validUntil"] == "2020-12-31"
    assert "TempRole" not in {r["id"] for r in client.get(f"{API}/roles").json()}  # no longer valid

    cs = client.post(f"{API}/changesets", json={"source": "p2p", "mode": "merge"}).json()["id"]
    r = client.post(
        f"{API}/changesets/{cs}/items",
        json={"items": [{"type": "roles", "entity": {"id": "X", "name": "X"}, "validFrom": "soon"}]},
    )
    assert r.status_code == 400


def test_data_from_before_provenance_is_migrated(client, service):
    store = service.store
    store.update(f"DROP SILENT GRAPH <{P.CLAIMS_GRAPH}> ; DROP SILENT GRAPH <{P.EFFECTIVE_GRAPH}>")
    assert client.get(f"{API}/roles/ApManager").json()["sources"] == ["p2p"]
    assert P.has_claims(store)
    assert client.get(f"{API}/roles/ApManager/provenance").json()["fields"]["name"]["status"] == "unconfirmed"


def test_stored_provenance_passes_its_validation_rules(client, service):
    from pathlib import Path

    from pyshacl import validate
    from rdflib import Graph

    attest(client, "roles", "ApManager")
    store = service.store
    data = Graph()
    for graph in (P.CLAIMS_GRAPH, P.ATTESTATIONS_GRAPH, P.REGISTRY_GRAPH):
        rows = store.query(f"SELECT ?s ?p ?o WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}")["results"][
            "bindings"
        ]
        from veromesh.changesets import _term

        for b in rows:
            data.add((_term(b["s"]), _term(b["p"]), _term(b["o"])))
    root = Path(__file__).resolve().parents[2] / "ontology"
    conforms, _, text = validate(
        data,
        shacl_graph=Graph().parse(root / "shapes" / "veromesh-prov-shapes.ttl"),
        ont_graph=Graph().parse(root / "veromesh-prov.ttl"),
        inference="rdfs",
    )
    assert conforms, text


def test_governance_settings_are_published(client):
    g = client.get(f"{API}/governance").json()
    assert g["ranking"]["people"][0] == "hr" and "email" in g["sourceWins"]["people"]
    assert g["cadenceDays"]["authorities"] == 90
