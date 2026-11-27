"""Attestation in the meetings owners already hold (gap G9): a steward records it for the
owner, with the meeting as evidence; it counts at once, and the owner may object for five
business days."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_auth import KEY, h, load_example, settings, steward, token

from veromesh import auth, clock
from veromesh import decisions as D
from veromesh import meetings as M
from veromesh.api import create_app

API = "/api/v1"
LEE = steward(email="lee@example.com")  # the steward who records
DANA = h(token(roles=("VeroMesh.Reader",), email="dana@example.com"))  # the owner, not a steward


@pytest.fixture
def mtg(service):
    log = D.MemoryLog()
    app = create_app(service, settings(), decision_log=log)
    app.state.verifier = auth.Verifier(settings(), key_for=lambda _t: KEY.public_key())
    client = TestClient(app)
    load_example(client)
    admin = h(token(roles=("VeroMesh.Admin",)))
    lee = {
        "type": "people",
        "op": "upsert",
        "entity": {"id": "LeeExample", "name": "Lee Example", "email": "lee@example.com"},
    }
    cs = client.post(f"{API}/changesets", json={"source": "hr"}, headers=admin).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": [lee]}, headers=admin)
    assert client.post(f"{API}/changesets/{cs}/commit", headers=admin).status_code == 200
    return client, log


def meeting(client, **more) -> dict:
    body = {
        "name": "AP monthly review",
        "heldOn": "2026-11-26",
        "link": "https://teams.example/meet/1",
        **more,
    }
    r = client.post(f"{API}/meetings", json=body, headers=LEE)
    assert r.status_code == 201, r.text
    return r.json()


def item(**more) -> dict:
    return {"type": "kpis", "id": "OnTimePaymentRate", "owner": "DanaExample", "role": "ApManager", **more}


def test_business_days_skip_the_weekend():
    assert M.add_business_days(date(2026, 11, 26), 5) == date(2026, 12, 3)  # Thursday -> next Thursday
    assert M.add_business_days(date(2026, 11, 28), 1) == date(2026, 11, 30)  # Saturday -> Monday
    assert M.add_business_days(date(2026, 12, 3), -2) == date(2026, 12, 1)  # back from Thursday
    assert M.add_business_days(date(2026, 11, 30), -1) == date(2026, 11, 27)  # Monday -> Friday


def test_the_meeting_is_evidence_on_the_chain(mtg):
    client, log = mtg
    m = meeting(client)
    assert m["createdBy"] == "LeeExample" and m["heldOn"] == "2026-11-26"
    entry = log.find(kind="meeting")[0]
    assert entry["meetingRecord"]["id"] == m["id"] and entry["subject"] == "LeeExample"
    assert [x["id"] for x in client.get(f"{API}/meetings", headers=LEE).json()] == [m["id"]]
    assert (
        client.post(f"{API}/meetings", json={"name": "x", "heldOn": "soon"}, headers=LEE).status_code == 422
    )


def test_the_preread_lists_what_waits_for_each_owner(mtg):
    client, _ = mtg
    r = client.post(f"{API}/meetings/preread", json={"people": ["DanaExample"]}, headers=LEE)
    assert r.status_code == 200, r.text
    dana = r.json()["people"][0]
    assert "ApManager" in dana["roles"]
    assert any(w["type"] == "kpis" and w["id"] == "OnTimePaymentRate" for w in dana["waiting"])


def test_recorded_for_the_owner_counts_at_once_and_is_on_the_chain(mtg, monkeypatch):
    client, log = mtg
    monkeypatch.setattr(clock, "today", lambda: date(2026, 11, 26))
    m = meeting(client)
    r = client.post(f"{API}/meetings/{m['id']}/attestations", json={"items": [item()]}, headers=LEE)
    assert r.status_code == 201, r.text
    assert r.json()["objectionUntil"] == "2026-12-03"
    made = r.json()["recorded"][0]["attestations"]
    assert made and all(a["person"] == "DanaExample" and a["recordedBy"] == "LeeExample" for a in made)
    e = log.find(kind="attestation")[-1]
    assert (e["subject"], e["recordedBy"], e["meeting"], e["objectionUntil"]) == (
        "DanaExample",
        "LeeExample",
        m["id"],
        "2026-12-03",
    )
    # Attested now: the KPI's facts no longer wait.
    waiting = client.post(f"{API}/meetings/preread", json={"people": ["DanaExample"]}, headers=LEE).json()
    assert not any(w["id"] == "OnTimePaymentRate" for w in waiting["people"][0]["waiting"])
    v = client.get(f"{API}/attestations/verify", headers=LEE).json()
    assert v["ok"], v
    # Dana sees it, open for objection.
    mine = client.get(f"{API}/meetings/recorded", headers=DANA).json()
    assert [(x["id"], x["standing"], x["meeting"]) for x in mine][:1] == [
        ("OnTimePaymentRate", "open", m["id"])
    ]


def test_the_owner_objects_or_confirms(mtg, monkeypatch):
    client, _ = mtg
    monkeypatch.setattr(clock, "today", lambda: date(2026, 11, 26))
    m = meeting(client)
    client.post(f"{API}/meetings/{m['id']}/attestations", json={"items": [item()]}, headers=LEE)
    r = client.post(
        f"{API}/kpis/OnTimePaymentRate/attestations",
        json={"verdict": "disputed", "role": "ApManager"},
        headers=DANA,
    )
    assert r.status_code == 201, r.text
    got = client.get(f"{API}/meetings/{m['id']}", headers=LEE).json()["recorded"]
    assert {x["standing"] for x in got} == {"disputed"}


def test_after_five_business_days_it_stands(mtg, monkeypatch):
    client, _ = mtg
    monkeypatch.setattr(clock, "today", lambda: date(2026, 11, 26))
    m = meeting(client)
    client.post(f"{API}/meetings/{m['id']}/attestations", json={"items": [item()]}, headers=LEE)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 12, 4))
    assert {x["standing"] for x in client.get(f"{API}/meetings/recorded", headers=DANA).json()} == {"stands"}


@pytest.mark.parametrize(
    "bad, status",
    [
        (item(role="Controller"), 403),  # Dana does not fill the role
        (item(owner="LeeExample", role="ApManager"), 422),  # the recorder is the owner: attest yourself
        (item(id="NoSuchKpi"), 404),
        ({"type": "kpis", "id": "OnTimePaymentRate", "owner": "DanaExample"}, 422),
    ],
)
def test_bad_items_write_nothing(mtg, bad, status):
    client, log = mtg
    m = meeting(client)
    before = len(log.find(kind="attestation", limit=1000))
    r = client.post(f"{API}/meetings/{m['id']}/attestations", json={"items": [item(), bad]}, headers=LEE)
    assert r.status_code == status, r.text
    assert len(log.find(kind="attestation", limit=1000)) == before  # not even the good item


def test_only_stewards_record(mtg):
    client, _ = mtg
    m = meeting(client)
    assert (
        client.post(
            f"{API}/meetings/{m['id']}/attestations", json={"items": [item()]}, headers=DANA
        ).status_code
        == 403
    )
    assert (
        client.post(f"{API}/meetings", json={"name": "x", "heldOn": "2026-11-26"}, headers=DANA).status_code
        == 403
    )
