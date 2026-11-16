"""Sign-in: tokens from an OpenID Connect provider, and who may call what."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from veromesh import auth
from veromesh.api import create_app
from veromesh.config import Settings

ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
AUDIENCE = "api://veromesh"
API = "/api/v1"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def settings(**kw) -> Settings:
    base = dict(
        rdf_store="memory",
        auth_disabled=False,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_role_prefix="VeroMesh.",
    )
    return Settings(**{**base, **kw})


def token(roles=("VeroMesh.Reader",), key=KEY, **claims) -> str:
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "oid": "oid-1",
        "name": "Pat Example",
        "scp": "access",
        "iat": now,
        "exp": now + 600,
        "roles": list(roles),
    }
    body.update(claims)
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm="RS256")


@pytest.fixture
def client(service) -> TestClient:
    app = create_app(service, settings())
    app.state.verifier = auth.Verifier(settings(), key_for=lambda _t: KEY.public_key())
    return TestClient(app)


def h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------- tokens


def test_health_and_docs_need_no_sign_in(client):
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_no_token_is_refused(client):
    r = client.get(f"{API}/schema")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_valid_token_reads(client):
    assert client.get(f"{API}/schema", headers=h(token())).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        token(key=OTHER_KEY),  # signed by someone else
        token(aud="api://something-else"),
        token(iss="https://login.microsoftonline.com/other-tenant/v2.0"),
        token(exp=int(time.time()) - 3600),
        "not-a-token",
    ],
)
def test_invalid_tokens_are_refused(client, bad):
    assert client.get(f"{API}/schema", headers=h(bad)).status_code == 401


def test_token_without_a_veromesh_role_is_forbidden(client):
    r = client.get(f"{API}/schema", headers=h(token(roles=("SomeOtherApp.User",))))
    assert r.status_code == 403


def test_issuer_trailing_slash_is_ignored(service):
    app = create_app(service, settings(oidc_issuer=ISSUER + "/"))
    app.state.verifier = auth.Verifier(
        settings(oidc_issuer=ISSUER + "/"), key_for=lambda _t: KEY.public_key()
    )
    c = TestClient(app)
    assert c.get(f"{API}/schema", headers=h(token())).status_code == 200


# ---------------------------------------------------------------- roles


def test_sparql_is_for_administrators_only(client):
    q = {"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"}
    for role in ("Reader", "Agent", "Steward", "Connector"):
        assert (
            client.post("/sparql", json=q, headers=h(token(roles=(f"VeroMesh.{role}",)))).status_code == 403
        )
    assert client.post("/sparql", json=q, headers=h(token(roles=("VeroMesh.Admin",)))).status_code == 200


def test_readers_and_agents_cannot_write(client):
    for role in ("Reader", "Agent"):
        r = client.post(
            f"{API}/changesets", json={"source": "p2p"}, headers=h(token(roles=(f"VeroMesh.{role}",)))
        )
        assert r.status_code == 403


def test_connector_writes_the_model(client):
    tok = token(roles=("VeroMesh.Connector",), idtyp="app", scp=None, name=None)
    items = client.get(f"{API}/examples/procure-to-pay", headers=h(tok)).json()["items"]
    cs = client.post(f"{API}/changesets", json={"source": "p2p"}, headers=h(tok)).json()["id"]
    assert (
        client.post(f"{API}/changesets/{cs}/items", json={"items": items}, headers=h(tok)).status_code == 200
    )
    assert client.post(f"{API}/changesets/{cs}/commit", headers=h(tok)).status_code == 200


def test_people_are_hidden_from_readers_and_agents(client):
    admin = h(token(roles=("VeroMesh.Admin",)))
    items = client.get(f"{API}/examples/procure-to-pay", headers=admin).json()["items"]
    cs = client.post(f"{API}/changesets", json={"source": "p2p"}, headers=admin).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items}, headers=admin)
    assert client.post(f"{API}/changesets/{cs}/commit", headers=admin).status_code == 200
    person = next(i["entity"]["id"] for i in items if i["type"] == "people")

    for role in ("Reader", "Agent"):
        hdr = h(token(roles=(f"VeroMesh.{role}",)))
        assert client.get(f"{API}/people", headers=hdr).status_code == 403
        assert client.get(f"{API}/people/{person}", headers=hdr).status_code == 403
        assert client.get(f"{API}/roles", headers=hdr).status_code == 200
    steward = h(token(roles=("VeroMesh.Steward",)))
    assert client.get(f"{API}/people/{person}", headers=steward).status_code == 200


def test_attestations_need_a_steward(client):
    r = client.get(f"{API}/attestations", headers=h(token(roles=("VeroMesh.Reader",))))
    assert r.status_code == 403
    r = client.get(f"{API}/attestations", headers=h(token(roles=("VeroMesh.Steward",))))
    assert r.status_code == 200


# ---------------------------------------------------------------- claims and configuration


def test_keycloak_realm_roles_and_service_accounts():
    claims = {
        "realm_access": {"roles": ["reader", "offline_access"]},
        "preferred_username": "service-account-hr",
    }
    assert auth.roles_from(claims, "realm_access.roles", "") == {"reader"}
    assert auth.kind_of(claims) == "app"


def test_entra_app_roles_with_prefix_ignore_case():
    assert auth.roles_from(
        {"roles": ["veromesh.ADMIN", "VeroMesh.Agent", "Other"]}, "roles", "VeroMesh."
    ) == {
        "admin",
        "agent",
    }
    assert auth.kind_of({"idtyp": "app", "roles": ["VeroMesh.Agent"]}) == "app"
    assert auth.kind_of({"scp": "access", "name": "Pat"}) == "user"


def test_unconfigured_sign_in_refuses_requests(service):
    c = TestClient(create_app(service, Settings(rdf_store="memory", auth_disabled=False)))
    r = c.get(f"{API}/schema", headers=h(token()))
    assert r.status_code == 503
    assert "VEROMESH_OIDC_ISSUER" in r.json()["detail"]


def test_unconfigured_sign_in_stops_the_server_starting(service):
    app = create_app(service, Settings(rdf_store="memory", auth_disabled=False))
    with pytest.raises(auth.ConfigError), TestClient(app):
        pass


def test_disabled_sign_in_acts_as_administrator(service):
    c = TestClient(create_app(service, Settings(rdf_store="memory", auth_disabled=True)))
    assert c.post("/sparql", json={"query": "ASK { ?s ?p ?o }"}).status_code == 200
