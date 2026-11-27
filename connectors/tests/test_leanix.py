"""SAP LeanIX: the capability map through the GraphQL API, the same shape as ServiceNow EA.

A small stand-in serves LeanIX's token endpoint (basic authentication as ``apitoken``) and
the GraphQL endpoint with Relay-style pages, so the connector runs through the CLI.
"""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient

from veromesh_connectors.cli import main

API = ["--api", "http://testserver"]
LX = ["leanix", "import", "--url", "http://testserver/lx"]


def sub(email, *roles, kind="RESPONSIBLE"):
    return {"node": {"type": kind, "user": {"email": email}, "roles": [{"name": r} for r in roles]}}


def app(fid, name, ext=None):
    return {
        "node": {"factSheet": {"id": fid, "name": name, "externalId": {"externalId": ext} if ext else None}}
    }


def cap(fid, name, parent=None, subs=(), status="ACTIVE", ext=None, desc=None, apps=()):
    return {
        "relBusinessCapabilityToApplication": {"edges": list(apps)},
        "id": fid,
        "name": name,
        "description": desc,
        "status": status,
        "externalId": {"externalId": ext} if ext else None,
        "relToParent": {"edges": [{"node": {"factSheet": {"id": parent}}}]} if parent else {"edges": []},
        "subscriptions": {"edges": list(subs)},
    }


CAPS = [
    cap(
        "f1",
        "Finance",
        subs=[sub("lee@example.com", "Business Owner")],
        ext="BC-1",
        desc="Plan, record and report money.",
    ),
    cap(
        "f2",
        "Accounts payable",
        "f1",
        [
            sub("ann@example.com", kind="OBSERVER"),
            sub("dana@example.com", "IT Owner"),
            sub("lee@example.com", "Business Owner"),
        ],
        ext="BC-2",
    ),
    cap("f3", "Supplier onboarding", "f2", [sub("dana@example.com")], ext="BC-3"),
    cap("f4", "Cheque printing", "f2", [sub("dana@example.com")], status="ARCHIVED", ext="BC-4"),
    cap("f5", "Treasury", "f1", [sub("nobody@example.com")], ext="BC-5"),
]


def leanix(caps: list[dict], page: int = 2) -> TestClient:
    app = FastAPI()
    good = "Basic " + base64.b64encode(b"apitoken:secret").decode()

    @app.post("/lx/services/mtm/v1/oauth2/token")
    def token(authorization: str = Header("")):
        if authorization != good:
            raise HTTPException(401)
        return {"access_token": "a", "expires_in": 3600}

    @app.post("/lx/services/pathfinder/v1/graphql")
    async def graphql(request: Request, authorization: str = Header("")):
        assert authorization == "Bearer a"
        body = await request.json()
        start = int(body["variables"].get("after") or 0)
        chunk = caps[start : start + page]
        more = start + page < len(caps)
        return {
            "data": {
                "allFactSheets": {
                    "pageInfo": {"hasNextPage": more, "endCursor": str(start + page) if more else None},
                    "edges": [{"node": n} for n in chunk],
                }
            }
        }

    return TestClient(app)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("LEANIX_API_TOKEN", "secret")


def people(http):
    items = [
        {
            "type": "people",
            "op": "upsert",
            "entity": {"id": "E-0042", "name": "Lee Example", "email": "lee@example.com"},
        },
        {
            "type": "people",
            "op": "upsert",
            "entity": {"id": "E-1001", "name": "Dana Example", "email": "dana@example.com"},
        },
    ]
    cs = http.post("/api/v1/changesets", json={"source": "hr"}).json()["id"]
    http.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
    assert http.post(f"/api/v1/changesets/{cs}/commit").status_code in (200, 201)


def capabilities(http, source="leanix") -> dict[str, dict]:
    return {c["id"]: c for c in http.get("/api/v1/capabilities", params={"source": source}).json()}


def test_the_capability_map_across_pages(http):
    people(http)
    assert main([*API, *LX], http=http, source_http=leanix(CAPS)) == 0
    caps = capabilities(http)
    assert set(caps) == {"Finance", "AccountsPayable", "SupplierOnboarding", "Treasury"}  # archived left out
    ap = caps["AccountsPayable"]
    assert ap["parentCapability"] == "Finance" and ap["ownerRole"] == "AccountsPayable.Owner"
    assert caps["Finance"]["definition"] == "Plan, record and report money."
    assert caps["SupplierOnboarding"]["parentCapability"] == "AccountsPayable"
    found = {p["id"]: p for p in http.get("/api/v1/people").json()}
    # The Business Owner subscription names the owner; else the first responsible subscriber.
    assert set(found["E-0042"]["rolesFilled"]) >= {"Finance.Owner", "AccountsPayable.Owner"}
    assert found["E-1001"]["rolesFilled"] in ("SupplierOnboarding.Owner", ["SupplierOnboarding.Owner"])


def test_external_ids_instead_of_names(http):
    people(http)
    assert main([*API, *LX, "--id-from", "externalId"], http=http, source_http=leanix(CAPS)) == 0
    caps = capabilities(http)
    assert set(caps) == {"BC-1", "BC-2", "BC-3", "BC-5"} and caps["BC-2"]["parentCapability"] == "BC-1"


def test_two_capabilities_with_one_name_stop_the_load(http, capsys):
    dup = [*CAPS[:2], cap("f9", "Accounts Payable", "f1")]
    assert main([*API, *LX], http=http, source_http=leanix(dup)) == 1
    assert "--id-from externalId" in capsys.readouterr().out


def test_a_wrong_api_token_is_refused(http, monkeypatch, capsys):
    monkeypatch.setenv("LEANIX_API_TOKEN", "wrong")
    assert main([*API, *LX], http=http, source_http=leanix(CAPS)) != 0


def test_leanix_and_servicenow_ea_describe_one_capability(http, monkeypatch):
    """Both EA tools give the same IDs and owner roles, so the model holds one capability."""
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "x")
    from test_servicenow_ea import CAPS as SN_CAPS
    from test_servicenow_ea import USERS, servicenow

    people(http)
    assert main([*API, *LX], http=http, source_http=leanix(CAPS)) == 0
    assert (
        main(
            [*API, "servicenow", "ea", "--url", "http://testserver/servicenow"],
            http=http,
            source_http=servicenow(SN_CAPS, USERS),
        )
        == 0
    )
    both = {c["id"] for c in http.get("/api/v1/capabilities").json()}
    assert {"Finance", "AccountsPayable", "SupplierOnboarding"} <= both
    assert len([c for c in both if c.lower() == "accountspayable"]) == 1


def test_capabilities_link_to_their_applications_by_external_id_or_name(http, capsys):
    from test_servicenow_ea import systems

    people(http)
    systems(
        http,
        {"id": "APM0001000", "name": "Oracle Fusion ERP", "cmdbId": "a-erp"},
        {"id": "APM0003000", "name": "Coupa", "cmdbId": "a-coupa"},
    )
    caps = [
        *CAPS[:1],
        cap(
            "f2",
            "Accounts payable",
            "f1",
            [sub("lee@example.com", "Business Owner")],
            apps=[app("x1", "Oracle ERP Cloud", ext="a-erp"), app("x2", "coupa"), app("x3", "Bank portal")],
        ),
    ]
    assert main([*API, *LX], http=http, source_http=leanix(caps)) == 0
    got = capabilities(http)["AccountsPayable"]["supportedBySystems"]
    assert sorted(got if isinstance(got, list) else [got]) == ["APM0001000", "APM0003000"]
    assert "1 applications match no system VeroMesh holds (Bank portal)" in capsys.readouterr().out
