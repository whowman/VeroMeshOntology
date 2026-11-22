"""The steward screen: the page, its sign-in configuration, and the inbox behind it."""

# The `client` and `ctx` fixtures are imported from the sign-in and decision suites.
# ruff: noqa: F811

from __future__ import annotations

from test_auth import client, h, load_example, reader, settings, token  # noqa: F401
from test_decide import ask, attest_basis, ctx  # noqa: F401

from veromesh import auth
from veromesh import decisions as D
from veromesh.api import create_app

API = "/api/v1"


def test_the_page_is_served_without_sign_in(ctx):
    client, _, _ = ctx
    r = client.get("/steward/")
    assert r.status_code == 200 and "VeroMesh · Steward" in r.text
    for script in ("/steward/app.js", "/common/vm.js", "/admin/app.js"):
        r = client.get(script)
        assert r.status_code == 200, script
        assert "innerHTML" not in r.text, script  # values are written as text, never HTML


def test_the_administration_area_has_the_licence_and_usage_pages(ctx):
    client, _, _ = ctx
    page = client.get("/admin/").text
    assert "VeroMesh · Administration" in page and 'data-tab="licence"' in page and 'id="usage-form"' in page
    assert 'data-page="/admin/"' in page and "/common/vm.js" in page
    js = client.get("/admin/app.js").text
    assert "/api/v1/licence" in js and "/api/v1/usage" in js
    steward = client.get("/steward/").text
    assert 'href="/admin/"' in steward and 'data-tab="usage"' not in steward  # usage moved to Administration


def test_sign_in_config_when_off(ctx):
    client, _, _ = ctx
    assert client.get(f"{API}/sign-in/config").json() == {"disabled": True}


def test_sign_in_config_when_on(service, monkeypatch):
    monkeypatch.setattr(
        auth,
        "discovery",
        lambda issuer: {"authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token"},
    )
    s = settings(oidc_ui_client_id="spa-client", oidc_ui_scope="api://veromesh/access_as_user")
    from fastapi.testclient import TestClient

    c = TestClient(create_app(service, s))
    cfg = c.get(f"{API}/sign-in/config").json()
    assert cfg["clientId"] == "spa-client" and cfg["scope"] == "api://veromesh/access_as_user"
    assert cfg["authorizationEndpoint"].endswith("/authorize") and "secret" not in str(cfg).lower()
    unset = TestClient(create_app(service, settings())).get(f"{API}/sign-in/config").json()
    assert "VEROMESH_OIDC_UI_CLIENT_ID" in unset["problem"]


def test_inbox_lists_what_waits_for_each_person(ctx):
    client, _, service = ctx
    attest_basis(client, service)
    consult = ask(client, 3000)
    refused = ask(client, 7000)
    allowed = ask(client, 800)
    lee = client.get(f"{API}/decisions/inbox", params={"person": "LeeExample"}).json()
    assert [(i["ask"], i["decision"]["id"], i["asRole"]) for i in lee] == [
        ("stop", allowed["id"], None),  # Lee's Controller role was informed of the 800 USD approval
        ("answer", consult["id"], "Controller"),
    ]
    dana = {
        i["ask"]: i["decision"]["id"]
        for i in client.get(f"{API}/decisions/inbox", params={"person": "DanaExample"}).json()
    }
    assert dana == {"stop": allowed["id"], "escalation": refused["id"]}
    # once Lee answers, it leaves his inbox
    client.post(
        f"{API}/decisions/{consult['id']}/overrides", json={"verdict": "approved", "person": "LeeExample"}
    )
    lee = client.get(f"{API}/decisions/inbox", params={"person": "LeeExample"}).json()
    assert [i["ask"] for i in lee] == ["stop"]


def test_inbox_is_the_signed_in_persons(client):
    load_example(client)
    log = D.MemoryLog()
    client.app.state.decision_log = log
    log.append(
        {
            "kind": "decision",
            "subject": "x",
            "agent": "InvoiceAssistant",
            "principal": "DanaExample",
            "action": "ApproveSupplierInvoice",
            "outcome": "refuse",
            "basis": {},
        }
    )
    mine = client.get(f"{API}/decisions/inbox", headers=reader(email="dana@example.com")).json()
    assert [i["ask"] for i in mine] == ["escalation"]
    # naming someone else is refused when signed in
    r = client.get(
        f"{API}/decisions/inbox", params={"person": "LeeExample"}, headers=reader(email="dana@example.com")
    )
    assert r.status_code == 403


def test_the_page_has_the_data_elements_tab(ctx):
    client, _, _ = ctx
    page = client.get("/steward/").text
    assert 'data-tab="coverage"' in page and 'id="suggestions"' in page
    js = client.get("/steward/app.js").text
    assert "/api/v1/data-elements/coverage" in js and "/api/v1/data-elements/suggestions/" in js
