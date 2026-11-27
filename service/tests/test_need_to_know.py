"""Need to know as a setting (issue #11): which People fields readers and agents do not see."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_auth import KEY, h, load_example, settings, token

from veromesh import auth
from veromesh.api import create_app

API = "/api/v1"
READER = h(token(roles=("VeroMesh.Reader",)))
AGENT = h(token(roles=("VeroMesh.Agent",)))
STEWARD = h(token(roles=("VeroMesh.Steward",)))


def client_with(service, **kw) -> TestClient:
    s = settings(**kw)
    app = create_app(service, s)
    app.state.verifier = auth.Verifier(s, key_for=lambda _t: KEY.public_key())
    c = TestClient(app)
    load_example(c)
    return c


def dana(c, hdr) -> dict:
    return c.get(f"{API}/people/DanaExample", headers=hdr).json()


def test_by_default_names_and_roles_are_shown(service):
    c = client_with(service)
    got = dana(c, READER)
    assert got["name"] == "Dana Example" and got["rolesFilled"]
    assert set(got["withheld"]) == {"email", "employeeId", "reportsTo"}


def test_names_can_be_withheld_too(service):
    c = client_with(service, personal_fields="email,employeeId,reportsTo,name")
    for hdr in (READER, AGENT):
        got = dana(c, hdr)
        assert "name" not in got and "name" in got["withheld"] and got["rolesFilled"]
        assert all("name" not in p for p in c.get(f"{API}/people", headers=hdr).json())
    assert dana(c, STEWARD)["name"] == "Dana Example"  # stewards have the need to know
    people = next(t for t in c.get(f"{API}/schema", headers=READER).json() if t["type"] == "people")
    assert {f["key"] for f in people["fields"] if f.get("personal")} == {
        "email",
        "employeeId",
        "reportsTo",
        "name",
    }


def test_agents_can_have_their_own_list(service):
    c = client_with(service, personal_fields="email", agent_personal_fields="email,employeeId,reportsTo,name")
    assert dana(c, READER)["reportsTo"] == "LeeExample" and dana(c, READER)["name"]
    got = dana(c, AGENT)
    assert "name" not in got and "reportsTo" not in got and got["rolesFilled"]


def test_a_setting_naming_no_people_field_stops_the_start(service):
    app = create_app(service, settings(personal_fields="email,salary"))
    with pytest.raises(ValueError, match="salary"), TestClient(app):
        pass
