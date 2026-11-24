"""Source fidelity (step 7a): how well each source is kept, measured per domain it decides,
starting from a declared rating, and what a low band changes."""

from __future__ import annotations

import test_decide
from test_decide import API, DANA, LEE, attest_basis, load_source

from veromesh import fidelity

ctx = test_decide.ctx  # the procure-to-pay example in source p2p, sign-in off


def register(client, name, category, **kw):
    r = client.put(f"{API}/sources/{name}", json={"category": category, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def load(client, source, items):
    cs = client.post(f"{API}/changesets", json={"source": source, "mode": "merge"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 200, r.text


def people(*ps):
    return [{"type": "people", "entity": p} for p in ps]


def staff(n, with_manager=True):
    return [
        {
            "id": f"S{i:03d}",
            "name": f"Staff {i}",
            "employeeId": f"E-{i:03d}",
            **({"reportsTo": "LeeExample"} if with_manager or i % 3 else {}),
        }
        for i in range(n)
    ]


def test_a_source_starts_from_its_declared_rating(ctx):
    client, _, _ = ctx
    assert register(client, "workday", "hr", fidelity="medium")["fidelity"] == "medium"
    load(client, "workday", people(DANA, LEE))  # too few facts to measure
    f = client.get(f"{API}/fidelity", params={"source": "workday"}).json()["workday"]
    assert f["employment"]["band"] == "medium" and not f["employment"]["measured"]
    assert f["employment"]["reasons"] == ["declared medium: too little history yet"]
    assert client.put(f"{API}/sources/x", json={"category": "hr", "fidelity": "great"}).status_code == 422
    listed = {s["source"]: s for s in client.get(f"{API}/sources").json()}
    assert listed["workday"]["fidelity"]["organization"] == "medium"


def test_unset_counts_as_high_until_measured(ctx):
    client, _, service = ctx
    register(client, "workday", "hr")
    load_source(client, "workday", "hr", [DANA, LEE])
    assert fidelity.band(service.store, "workday", "employment") == "high"


def test_missing_managers_lower_the_measured_band(ctx):
    client, _, _ = ctx
    register(client, "workday", "hr", fidelity="high")
    load(client, "workday", people(DANA, LEE, *staff(30, with_manager=False)))
    org = client.get(f"{API}/fidelity").json()["workday"]["organization"]
    assert org["measured"] and org["band"] == "low"
    assert org["signals"]["gaps"] > 0.2
    assert any("missing fields decisions need" in r for r in org["reasons"])
    emp = client.get(f"{API}/fidelity").json()["workday"]["employment"]
    assert emp["band"] == "high"  # every one has an employee ID: employment is kept well


def test_a_low_fidelity_authority_source_is_consulted(ctx):
    client, _, service = ctx
    register(client, "ap", "erp", ownerRole="Controller", fidelity="low")
    item = next(
        i
        for i in client.get(f"{API}/examples/procure-to-pay").json()["items"]
        if i["entity"].get("id") == "AssistantApprovalAuthority"
    )
    load(client, "ap", [item])
    attest_basis(client, service)
    d = test_decide.ask(client, 800)
    assert d["outcome"] == "consult", d["reasons"]
    assert {"domain": "authority-in-force", "source": "ap", "band": "low"} in d["basis"]["fidelity"]
    assert any(c["role"] == "Controller" and "low fidelity" in " ".join(c["why"]) for c in d["consult"])


def test_a_medium_source_is_reviewed_sooner(ctx):
    client, _, _ = ctx
    register(client, "workday", "hr", fidelity="medium")
    load(client, "workday", people(DANA, LEE))
    r = client.post(
        f"{API}/people/DanaExample/attestations",
        json={
            "verdict": "attested",
            "attester": "LeeExample",
            "role": "Controller",
            "fields": ["employeeId"],
            "source": "workday",
        },
    )
    assert r.status_code == 201, r.text
    fields = client.get(f"{API}/people/DanaExample/provenance").json()["fields"]["employeeId"]
    att = fields["attestation"]
    from datetime import datetime

    days = (datetime.fromisoformat(att["expiresAt"]) - datetime.fromisoformat(att["at"])).days
    full = client.get(f"{API}/governance").json()["cadenceDays"]
    assert days < max(full.values()) and days in {round(v * 0.5) for v in full.values()} | {
        max(1, round(v * 0.5)) for v in full.values()
    }


def test_the_declared_rating_breaks_a_tie_before_recency(ctx):
    client, _, _ = ctx
    register(client, "hr-old", "hr", fidelity="high")
    register(client, "hr-new", "hr", fidelity="low")
    load(client, "hr-old", people({**DANA, "email": "dana@corp.example"}, LEE))
    load(client, "hr-new", people({**DANA, "email": "dana.new@corp.example"}, LEE))  # newer, but low
    assert client.get(f"{API}/people/DanaExample").json()["email"] == "dana@corp.example"


def test_known_hr_lateness_makes_removed_access_urgent(ctx):
    client, _, _ = ctx
    register(client, "workday", "hr")
    register(client, "entra", "iam", ownerRole="Controller")
    register(client, "workday", "hr", ownerRole="Controller")
    late = {"id": "GoneExample", "name": "Gone Example", "employeeId": "E-0999"}
    load(
        client,
        "workday",
        [
            {"type": "people", "entity": DANA},
            {"type": "people", "entity": LEE},
            # ended five days before Workday reported it
            {
                "type": "people",
                "entity": late,
                "validUntil": "2026-01-05",
                "observedAt": "2026-01-10T09:00:00Z",
            },
        ],
    )
    load_source(client, "entra", "iam", [LEE])  # Dana's account is gone; Workday still lists her
    f = client.get(f"{API}/findings", params={"kind": "access-removed-hr-may-be-late"}).json()["findings"]
    assert [x["subject"]["id"] for x in f] == ["DanaExample"]
    assert f[0]["severity"] == "high" and f[0]["about"]["lateDays"] == 5
