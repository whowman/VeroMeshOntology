"""Attestations on the decision log's hash chain: every attestation the service makes is
appended to the chain before the model sees it, and the attestations the model relies on
can be checked against the chain."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_auth import KEY, attest, h, load_example, settings, steward, token
from test_decisions import BACKENDS, fresh_pg

from veromesh import auth
from veromesh import decisions as D
from veromesh import provenance as P
from veromesh.api import create_app

API = "/api/v1"


@pytest.fixture(params=BACKENDS)
def chained(service, request):
    log = D.MemoryLog() if request.param == "memory" else fresh_pg()
    app = create_app(service, settings(), decision_log=log)
    app.state.verifier = auth.Verifier(settings(), key_for=lambda _t: KEY.public_key())
    client = TestClient(app)
    load_example(client)
    return client, log, service.store


def admin() -> dict:
    return h(token(roles=("VeroMesh.Steward",)))


def verify(client) -> dict:
    r = client.get(f"{API}/attestations/verify", headers=admin())
    assert r.status_code == 200, r.text
    return r.json()


def test_an_attestation_is_on_the_chain_with_what_it_attests(chained):
    client, log, store = chained
    r = attest(client, steward(email="dana@example.com"), role="ApManager", note="checked with AP")
    assert r.status_code == 201, r.text
    made = r.json()["attestations"]
    entries = log.find(kind="attestation")
    assert len(entries) == len(made) >= 1
    graph = {a.iri: a for a in P.read_attestations(store)}
    for e in entries:
        a = graph[e["attestation"]]
        assert e["outcome"] == "attested" and e["subject"] == "DanaExample" and e["role"] == "ApManager"
        assert (e["entityType"], e["entityId"]) == ("kpis", "OnTimePaymentRate")
        assert e["fieldHashes"] == a.hashes and e["note"] == "checked with AP"
    assert log.verify().ok
    v = verify(client)
    assert v["ok"] and v["attestations"] == v["recorded"] == len(made) and v["chain"]["ok"]


def test_a_dispute_and_a_correction_are_on_the_chain_too(chained):
    client, log, _ = chained
    r = attest(client, steward(email="dana@example.com"), role="ApManager", verdict="disputed")
    assert r.status_code == 201, r.text
    r = client.post(
        f"{API}/kpis/OnTimePaymentRate/corrections",
        json={"values": {"unit": "percent"}, "role": "ApManager"},
        headers=steward(email="dana@example.com"),
    )
    assert r.status_code == 201, r.text
    outcomes = [e["outcome"] for e in log.find(kind="attestation")]
    assert "disputed" in outcomes and outcomes[-1] == "attested"
    assert verify(client)["ok"]


def test_an_attestation_changed_in_the_model_is_found(chained):
    client, _, store = chained
    attest(client, steward(email="dana@example.com"), role="ApManager")
    a = P.read_attestations(store)[0]
    # someone edits the store directly: the attestation now says it was disputed
    store.update(
        f"""DELETE DATA {{ GRAPH <{P.ATTESTATIONS_GRAPH}> {{ <{a.iri}> <{P.VMP.verdict}> "{a.verdict}" }} }};
            INSERT DATA {{ GRAPH <{P.ATTESTATIONS_GRAPH}> {{ <{a.iri}> <{P.VMP.verdict}> "disputed" }} }}"""
    )
    v = verify(client)
    assert not v["ok"] and v["chain"]["ok"]
    assert v["altered"] == [{"attestation": a.iri[len(P.ATTESTATION_BASE) :], "differs": ["verdict"]}]


def test_an_attestation_written_around_the_service_is_found(chained):
    client, _, store = chained
    attest(client, steward(email="dana@example.com"), role="ApManager")
    forged = P.write_attestation(  # no log: straight into the model
        store,
        type_="kpis",
        rid="OnTimePaymentRate",
        source="p2p",
        values={"name": "x"},
        verdict="attested",
        person="DanaExample",
        role="ApManager",
        base=settings().instance_base,
    )
    v = verify(client)
    assert not v["ok"] and v["unrecorded"] == [forged.iri[len(P.ATTESTATION_BASE) :]]


def test_an_attestation_removed_from_the_model_is_found(chained):
    client, _, store = chained
    attest(client, steward(email="dana@example.com"), role="ApManager")
    a = P.read_attestations(store)[0]
    store.update(f"DELETE WHERE {{ GRAPH <{P.ATTESTATIONS_GRAPH}> {{ <{a.iri}> ?p ?o }} }}")
    v = verify(client)
    assert not v["ok"] and a.iri[len(P.ATTESTATION_BASE) :] in v["missing"]


def test_verifying_needs_a_steward(chained):
    client, _, _ = chained
    assert client.get(f"{API}/attestations/verify", headers=h(token())).status_code == 403


def test_an_attestation_entry_must_say_what_it_attests():
    with pytest.raises(D.InvalidEntry, match="needs attestation"):
        D.check({"kind": "attestation", "outcome": "attested", "subject": "x"})
    with pytest.raises(D.InvalidEntry, match="one of attested, disputed"):
        D.check({"kind": "attestation", "outcome": "allow", "subject": "x"})
    with pytest.raises(D.InvalidEntry, match="fieldHashes"):
        D.check(
            {
                "kind": "attestation",
                "outcome": "attested",
                "subject": "x",
                "attestation": "a",
                "entityType": "kpis",
                "entityId": "k",
                "source": "s",
                "role": "r",
                "attestedAt": "2026-11-22T00:00:00+00:00",
                "fieldHashes": ["not", "a", "map"],
            }
        )
