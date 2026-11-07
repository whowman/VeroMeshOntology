from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from veromesh.api import create_app
from veromesh.config import Settings

API = "/api/v1"


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service, Settings(rdf_store="memory")))


@pytest.fixture
def example(client) -> list[dict]:
    return client.get(f"{API}/examples/procure-to-pay").json()["items"]


def entity(items: list[dict], type_: str, rid: str) -> dict:
    return next(i["entity"] for i in items if i["type"] == type_ and i["entity"].get("id") == rid)


def submit(client, items: list[dict], source: str = "p2p", mode: str = "replace") -> tuple[str, dict]:
    cs = client.post(f"{API}/changesets", json={"source": source, "mode": mode}).json()["id"]
    assert client.post(f"{API}/changesets/{cs}/items", json={"items": items}).status_code == 200
    return cs, client.post(f"{API}/changesets/{cs}/validate").json()


def load(client, items, source="p2p", mode="replace") -> dict:
    cs, report = submit(client, items, source, mode)
    assert report["valid"], report
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.json()
    return r.json()


def test_schema_describes_every_type(client):
    types = {t["type"]: t for t in client.get(f"{API}/schema").json()}
    assert len(types) == 15
    holder = next(f for f in types["authorities"]["fields"] if f["key"] == "holder")
    assert holder["targets"] == ["roles", "people", "agents"] and holder["required"]
    assert types["impacts"]["keyed"] is False


def test_example_loads_and_reads_back(client, example):
    cs, report = submit(client, example)
    assert report["valid"] and report["summary"]["errors"] == 0
    assert report["summary"]["violations"] == 0 and report["summary"]["warnings"] == 0
    committed = client.post(f"{API}/changesets/{cs}/commit").json()
    assert committed["committed"] and committed["facts"] > 100

    sources = client.get(f"{API}/sources").json()
    assert sources[0]["source"] == "p2p" and sources[0]["changeset"] == cs
    assert client.get(f"{API}/changesets/{cs}").json()["status"] == "committed"

    kpis = client.get(f"{API}/kpis").json()
    assert sorted(k["id"] for k in kpis) == ["DuplicatePaymentRate", "OnTimePaymentRate"]
    auth = client.get(f"{API}/authorities/AssistantApprovalAuthority").json()
    assert auth["holder"] == "InvoiceAssistant" and auth["parentAuthority"] == "ApApprovalAuthority"
    assert auth["sources"] == ["p2p"]


def test_violations_block_commit(client, example):
    items = copy.deepcopy(example)
    entity(items, "authorities", "AssistantApprovalAuthority")["limitAmount"] = "75000"
    cs, report = submit(client, items)
    assert report["valid"] is False
    hit = next(v for v in report["violations"] if "exceeds its parent" in v["message"])
    assert hit["type"] == "authorities" and hit["id"] == "AssistantApprovalAuthority"
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 409 and r.json()["report"]["valid"] is False
    assert client.get(f"{API}/sources").json() == []


@pytest.mark.parametrize(
    "type_,rid,key,value,message",
    [
        ("kpis", "OnTimePaymentRate", "ownerRole", "NoSuchRole", "No entity with ID 'NoSuchRole'"),
        ("kpis", "OnTimePaymentRate", "ownerRole", "DanaExample", "is a people entity"),
        ("authorities", "ApApprovalAuthority", "validUntil", "31/12/2026", "not a date"),
        ("authorities", "ApApprovalAuthority", "holder", None, "Required value missing"),
        ("systems", "Erp", "criticality", "extreme", "not one of"),
        ("kpis", "OnTimePaymentRate", "colour", "red", "Unknown field"),
    ],
)
def test_field_errors(client, example, type_, rid, key, value, message):
    items = copy.deepcopy(example)
    entity(items, type_, rid)[key] = value
    _, report = submit(client, items)
    assert not report["valid"]
    assert any(
        e["type"] == type_ and e["id"] == rid and e["field"] == key and message in e["message"]
        for e in report["errors"]
    ), report["errors"]


def test_references_across_sources(client, example):
    people_types = ("roles", "people")
    load(client, [i for i in example if i["type"] in people_types], source="hr")
    report = load(client, [i for i in example if i["type"] not in people_types], source="p2p")
    assert report["summary"]["violations"] == 0
    assert client.get(f"{API}/roles/Controller").json()["sources"] == ["hr"]

    # Replacing the HR source without the controller role breaks references in p2p.
    hr_without = [i for i in example if i["type"] in people_types and i["entity"]["id"] != "Controller"]
    for i in hr_without:
        if i["type"] == "people" and "Controller" in i["entity"].get("rolesFilled", []):
            i["entity"]["rolesFilled"].remove("Controller")
    _, broken = submit(client, hr_without, source="hr")
    assert broken["valid"] is False
    assert any(
        e["type"] == "kpis"
        and e["id"] == "DuplicatePaymentRate"
        and e["field"] == "ownerRole"
        and "no longer exists" in e["message"]
        for e in broken["errors"]
    ), broken["errors"]


def test_single_entity_writes(client, example):
    load(client, example)
    r = client.put(f"{API}/roles/Treasurer", params={"source": "p2p"}, json={"name": "Treasurer"})
    assert r.status_code == 200, r.json()
    assert any("Nobody currently fills" in w["message"] for w in r.json()["warnings"])
    assert client.get(f"{API}/roles/Treasurer").json()["name"] == "Treasurer"

    assert (
        client.post(
            f"{API}/roles", params={"source": "p2p"}, json={"id": "Treasurer", "name": "T"}
        ).status_code
        == 409
    )

    r = client.delete(f"{API}/roles/Controller", params={"source": "p2p"})
    assert r.status_code == 422
    assert any("Still referenced" in e["message"] for e in r.json()["report"]["errors"])

    assert client.delete(f"{API}/roles/Treasurer", params={"source": "p2p"}).status_code == 200
    assert client.get(f"{API}/roles/Treasurer").status_code == 404

    r = client.put(
        f"{API}/kpis/OnTimePaymentRate",
        params={"source": "p2p"},
        json={"name": "On-time payment rate", "ownerRole": "Nobody"},
    )
    assert r.status_code == 422


def test_merge_changeset_upserts_without_touching_the_rest(client, example):
    load(client, example)
    report = load(
        client,
        [{"type": "roles", "op": "upsert", "entity": {"id": "Treasurer", "name": "Treasurer"}}],
        mode="merge",
    )
    assert report["committed"]
    assert len(client.get(f"{API}/kpis", params={"source": "p2p"}).json()) == 2


def test_changeset_lifecycle_and_bad_requests(client, example):
    cs, _ = submit(client, example)
    assert client.post(f"{API}/changesets/{cs}/commit").status_code == 200
    again = client.post(f"{API}/changesets/{cs}/commit")
    assert again.status_code == 200 and again.json()["alreadyCommitted"] is True
    assert client.post(f"{API}/changesets/{cs}/items", json={"items": []}).status_code == 409

    cs2, _ = submit(client, example, source="other")
    assert client.delete(f"{API}/changesets/{cs2}").json()["status"] == "discarded"

    assert client.get(f"{API}/changesets/0123456789abcdef").status_code == 404
    assert client.get(f"{API}/unicorns").status_code == 404
    assert client.post(f"{API}/changesets", json={"source": "Not Valid!"}).status_code == 400
    _, report = submit(client, [{"type": "roles", "op": "delete", "entity": {"id": "X"}}], source="z")
    assert "only allowed in merge mode" in report["errors"][0]["message"]


def test_concurrent_loads_cannot_overwrite_each_other(client, example):
    load(client, example)
    a, _ = submit(client, example)
    b, _ = submit(client, example)
    assert client.post(f"{API}/changesets/{a}/commit").status_code == 200
    r = client.post(f"{API}/changesets/{b}/commit")
    assert r.status_code == 409 and "changed since this changeset was opened" in r.json()["detail"]
    assert client.get(f"{API}/sources").json()[0]["version"] == a


def test_idempotency_key_resumes_the_same_changeset(client, example):
    body = {"source": "p2p", "idempotencyKey": "run-1", "connector": "spreadsheet"}
    first = client.post(f"{API}/changesets", json=body)
    second = client.post(f"{API}/changesets", json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"] and second.json()["reused"] is True
    assert second.json()["connector"] == "spreadsheet"

    client.delete(f"{API}/changesets/{first.json()['id']}")
    third = client.post(f"{API}/changesets", json=body)
    assert third.status_code == 201 and third.json()["id"] != first.json()["id"]


def test_run_history(client, example):
    load(client, example)
    load(
        client, [{"type": "roles", "op": "upsert", "entity": {"id": "Treasurer", "name": "T"}}], mode="merge"
    )
    runs = client.get(f"{API}/changesets", params={"source": "p2p"}).json()
    assert [r["status"] for r in runs] == ["committed", "committed"]
    assert runs[0]["mode"] == "merge" and runs[0]["baseVersion"] == runs[1]["id"]
    assert client.get(f"{API}/changesets", params={"status": "open"}).json() == []


def test_updating_a_role_keeps_the_authorities_it_holds(client, example):
    load(client, example)
    r = client.put(f"{API}/roles/ApManager", params={"source": "p2p"}, json={"name": "AP manager"})
    assert r.status_code == 200, r.json()
    assert client.get(f"{API}/authorities/ApApprovalAuthority").json()["holder"] == "ApManager"
