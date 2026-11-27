"""The licence file: issued and checked offline, bound to one install, staged by date,
and read-only after the grace period without stopping anything that keeps decisions safe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_auth import client  # noqa: F401

from veromesh import decisions as D
from veromesh import licence as L
from veromesh import usage
from veromesh.config import Settings

API = "/api/v1"
VM_KEY = usage.Signer.generate_pem()  # stands in for VeroMesh's key in the vault
OTHER_KEY = usage.Signer.generate_pem()
KEYS = {L.key_id(L.public_of(VM_KEY)): L.public_of(VM_KEY)}


def cfg(**kw) -> Settings:
    return Settings(**{"rdf_store": "memory", "auth_disabled": True, "install_id": "acme-prod-1", **kw})


def install() -> tuple[usage.Usage, L.Licensing]:
    u = usage.Usage(usage.MemoryUsage(), D.MemoryLog(), cfg())
    lic = L.Licensing(u, u.settings, keys=KEYS)
    u.licensing = lic
    return u, lic


def licence_for(u: usage.Usage, key: str = VM_KEY, **kw) -> dict:
    fields = {
        "id": "lic-1",
        "customer": "Acme",
        "installId": "acme-prod-1",
        "usageKey": u.signer().public_key,
        "tier": "large",
        "validFrom": "2026-12-01",
        **kw,
    }
    return L.issue(key, **fields)


# ---------------------------------------------------------------- issuing and checking


def test_a_licence_is_a_year_by_default_and_verifies_offline():
    u, _ = install()
    doc = licence_for(u)
    lic = L.verify(doc, install_id="acme-prod-1", usage_key=u.signer().public_key, keys=KEYS)
    assert lic["validUntil"] == "2027-11-30" and lic["graceDays"] == 30 and lic["environment"] == "production"


def test_a_changed_licence_another_key_another_install_or_usage_key_is_refused():
    u, _ = install()
    doc = licence_for(u)
    changed = json.loads(json.dumps(doc))
    changed["licence"]["tier"] = "extra-large"
    with pytest.raises(L.LicenceError, match="signature"):
        L.verify(changed, keys=KEYS)
    with pytest.raises(L.LicenceError, match="does not know"):
        L.verify(licence_for(u, key=OTHER_KEY), keys=KEYS)
    with pytest.raises(L.LicenceError, match="not this one"):
        L.verify(doc, install_id="someone-else", keys=KEYS)
    with pytest.raises(L.LicenceError, match="usage key"):
        L.verify(doc, usage_key=usage.Signer(OTHER_KEY).public_key, keys=KEYS)
    with pytest.raises(L.LicenceError, match="tier"):
        licence_for(u, tier="galactic")


def test_the_release_trusts_only_the_keys_built_into_it(tmp_path, monkeypatch):
    f = tmp_path / "keys.json"
    f.write_text(json.dumps({"keys": {"vault-1": L.public_of(VM_KEY)}}))
    monkeypatch.setattr(L, "KEYS_FILE", f)
    assert L.trusted_keys() == KEYS
    shipped = json.loads((Path(L.__file__).with_name("licence_keys.json")).read_text())
    assert "keys" in shipped  # the shipped list (VeroMesh's production key goes in by pull request)


# ---------------------------------------------------------------- stages


def at(day: str) -> datetime:
    return datetime.fromisoformat(day + "T12:00:00+00:00")


@pytest.mark.parametrize(
    ("day", "stage", "read_only"),
    [
        ("2027-06-01", "licensed", False),
        ("2027-10-05", "warning", False),  # within 60 days of 30 November
        ("2027-11-30", "warning", False),  # its last day
        ("2027-12-01", "grace", False),
        ("2027-12-30", "grace", False),  # 30 days of grace
        ("2027-12-31", "read-only", True),
    ],
)
def test_a_licence_goes_through_warning_and_grace_to_read_only(day, stage, read_only):
    u, _ = install()
    lic = licence_for(u)["licence"]
    st = L.stage_of(lic, at("2026-12-01"), at(day))
    assert (st["stage"], st["readOnly"]) == (stage, read_only)


def test_without_a_licence_an_install_runs_30_days_then_goes_read_only():
    first = at("2026-11-01")
    assert L.stage_of(None, first, at("2026-11-30"))["stage"] == "unlicensed"
    st = L.stage_of(None, first, at("2026-12-02"))
    assert st["stage"] == "read-only" and "until a licence is loaded" in st["message"]


def test_a_renewal_takes_over_when_it_begins_and_loading_ends_read_only():
    u, lic = install()
    u.store.put_setting("first-start", (datetime.now(UTC) - timedelta(days=40)).isoformat())
    assert lic.status(fresh=True)["readOnly"]  # unlicensed for 40 days
    today = datetime.now(UTC).date()
    lic.load(licence_for(u, id="current", validFrom=(today - timedelta(days=310)).isoformat()))
    st = lic.status(fresh=True)
    assert not st["readOnly"] and st["licence"]["id"] == "current" and st["stage"] == "warning"
    lic.load(licence_for(u, id="renewal", validFrom=(today + timedelta(days=65)).isoformat()))
    assert lic.status(fresh=True)["licence"]["id"] == "current"  # not begun yet
    later = datetime.now(UTC) + timedelta(days=70)
    assert lic.status(later)["licence"]["id"] == "renewal" and lic.status(later)["stage"] == "licensed"
    with pytest.raises(L.LicenceError):
        lic.load(licence_for(u, key=OTHER_KEY, id="forged"))
    assert [x["licence"]["id"] for x in lic.licences()] == ["current", "renewal"]


def test_the_usage_report_takes_its_tier_from_the_licence():
    u, lic = install()
    lic.load(
        licence_for(u, tier="medium", validFrom=(datetime.now(UTC).date() - timedelta(days=1)).isoformat())
    )
    assert u.tier() == {"name": "Medium", "identities": 25000}
    u.store.put_day(
        "2026-10-03", {"identities": {"people": 1, "agents": 0, "serviceAccounts": 0, "total": 1}}
    )
    r = u.build("2026-10")
    assert r["licence"]["id"] == "lic-1" and r["licence"]["stage"] in ("licensed", "warning")


# ---------------------------------------------------------------- read-only in the API


SAFE = {
    "POST /api/v1/decisions/check",
    "POST /api/v1/decisions/{entry_id}/outcome",
    "POST /api/v1/decisions/{entry_id}/overrides",
    "POST /api/v1/{type_}/{rid}/attestations",
    "POST /api/v1/meetings",
    "POST /api/v1/meetings/preread",
    "POST /api/v1/meetings/{mid}/attestations",
    "POST /api/v1/notify/run",
    "PUT /api/v1/notify/settings",
    "POST /api/v1/teams/messages",
    "POST /sparql",
    "POST /api/v1/usage/reports/{month}/send",
    "POST /api/v1/usage/run",
    "POST /api/v1/usage/settings/test",
    "PUT /api/v1/usage/settings",
    "PUT /api/v1/licence",
}


def test_every_write_route_is_either_stopped_or_safe_in_read_only():
    """A new write route is stopped in read-only unless it is added to SAFE here, on purpose."""
    routes = json.loads((Path(__file__).parents[2] / "tests/contracts/golden/api-routes.json").read_text())
    writes = {r for r in routes if r.split()[0] in ("POST", "PUT", "PATCH", "DELETE")} | {
        "PUT /api/v1/licence"
    }
    for r in sorted(writes):
        method, path = r.split()
        concrete = path.replace("{type_}", "people").replace("{rid}", "Dana").replace("{entry_id}", "e1")
        concrete = concrete.replace("{month}", "2026-10").replace("{cs}", "c1").replace("{name}", "hr")
        assert L.blocked(method, concrete) == (r not in SAFE), r
    assert not L.blocked("GET", "/api/v1/people") and not L.blocked("POST", "/mcp")
    assert not L.blocked("POST", "/gateway/mcp")  # agents' tool calls are decisions


@pytest.fixture
def expired(service, monkeypatch):
    from veromesh.api import create_app

    monkeypatch.setattr(L, "trusted_keys", lambda: KEYS)
    app = create_app(service, cfg())
    c = TestClient(app)
    items = c.get(f"{API}/examples/procure-to-pay").json()["items"]
    cs = c.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    c.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert c.post(f"{API}/changesets/{cs}/commit").status_code == 200
    u = app.state.usage
    u.store.put_setting("first-start", (datetime.now(UTC) - timedelta(days=31)).isoformat())
    u.licensing._cached = None
    return c, u


def test_read_only_stops_loading_and_editing_but_not_decisions_or_attestations(expired):
    c, u = expired
    assert c.get(f"{API}/licence/status").json()["stage"] == "read-only"
    for method, path, body in (
        ("post", f"{API}/changesets", {"source": "hr"}),
        ("put", f"{API}/people/DanaExample", {"name": "Dana"}),
        ("delete", f"{API}/people/DanaExample", None),
        ("post", f"{API}/effective/rebuild", None),
    ):
        r = getattr(c, method)(path, json=body) if body is not None else getattr(c, method)(path)
        assert r.status_code == 403 and r.json()["detail"].startswith("licence expired"), (path, r.text)
    d = c.post(
        f"{API}/decisions/check",
        json={
            "agent": "InvoiceAssistant",
            "action": "ApproveSupplierInvoice",
            "amount": 100,
            "currency": "USD",
        },
    )
    assert d.status_code == 201, d.text
    att = c.post(
        f"{API}/people/DanaExample/attestations",
        json={"verdict": "attested", "attester": "LeeExample", "role": "Controller", "fields": ["name"]},
    )
    assert att.status_code != 403, att.text
    assert c.get(f"{API}/people").status_code == 200
    assert c.post("/sparql", json={"query": "ASK { ?s ?p ?o }"}).status_code != 403


def test_loading_a_licence_ends_read_only_at_once(expired):
    c, u = expired
    overview = c.get(f"{API}/licence").json()
    assert (
        overview["install"]["id"] == "acme-prod-1"
        and overview["install"]["usageKey"] == u.signer().public_key
    )
    doc = licence_for(u, validFrom=(datetime.now(UTC).date() - timedelta(days=1)).isoformat())
    r = c.put(f"{API}/licence", json=doc)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "licensed"
    assert c.post(f"{API}/changesets", json={"source": "hr"}).status_code == 201
    forged = licence_for(u, key=OTHER_KEY)
    assert c.put(f"{API}/licence", json=forged).status_code == 400


def test_licence_details_are_for_administrators(client):  # noqa: F811
    from test_auth import h, token

    reader = h(token(roles=("VeroMesh.Reader",)))
    assert client.get(f"{API}/licence", headers=reader).status_code == 403
    assert client.get(f"{API}/licence/status", headers=reader).status_code == 200


# ---------------------------------------------------------------- command line


def test_keygen_issue_and_verify_on_the_command_line(tmp_path, monkeypatch, capsys):
    key = tmp_path / "vm.pem"
    assert L.main(["keygen", "--out", str(key)]) == 0
    pub = capsys.readouterr().out.split("licence_keys.json: ")[1].split()[0]
    monkeypatch.setattr(L, "trusted_keys", lambda: {L.key_id(pub): pub})
    install_key = usage.Signer(usage.Signer.generate_pem()).public_key
    out = tmp_path / "lic.json"
    args = ["issue", "--key-file", str(key), "--id", "lic-9", "--customer", "Acme", "--install-id", "i-1"]
    args += ["--usage-key", install_key, "--tier", "small", "--from", "2026-12-01", "--out", str(out)]
    assert L.main(args) == 0
    assert L.main(["verify", str(out), "--install-id", "i-1"]) == 0
    assert "genuine" in capsys.readouterr().out
    assert L.main(["verify", str(out), "--install-id", "i-2"]) == 1
