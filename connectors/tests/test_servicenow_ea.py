"""ServiceNow Enterprise Architecture: the capability map, its owner roles, and the capabilities domain.

A small stand-in for ServiceNow's Table API serves cmdb_ci_business_capability and sys_user,
so the connector runs through the CLI as it does against a real instance.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from veromesh_connectors.cli import main
from veromesh_connectors.plugins.servicenow import pascal

API = ["--api", "http://testserver"]
EA = ["servicenow", "ea", "--url", "http://testserver/servicenow"]


def ref(sid):
    return {"link": f"/api/now/table/x/{sid}", "value": sid} if sid else ""


def servicenow(caps: list[dict], users: dict[str, dict], apps=(), rels=()) -> TestClient:
    app = FastAPI()
    tables = {
        "cmdb_ci_business_capability": caps,
        "cmdb_ci_business_app": list(apps),
        "cmdb_rel_ci": list(rels),
    }

    @app.get("/servicenow/api/now/table/{table}")
    def rows(table: str, request: Request):
        got = tables[table]
        q = request.query_params.get("sysparm_query", "")
        if q.startswith("type.name="):  # the only query the connector sends
            got = [r for r in got if r["type"]["display_value"] == q[len("type.name=") :]]
        off = int(request.query_params.get("sysparm_offset", 0))
        lim = int(request.query_params.get("sysparm_limit", 1000))
        return {"result": got[off : off + lim]}

    @app.get("/servicenow/api/now/table/sys_user/{sid}")
    def user(sid: str):
        if sid not in users:
            raise HTTPException(404)
        return {"result": users[sid]}

    return TestClient(app)


USERS = {
    "u-lee": {
        "sys_id": "u-lee",
        "user_name": "lee.example",
        "name": "Lee Example",
        "email": "lee@example.com",
        "employee_number": "E-0042",
    },
    "u-dana": {
        "sys_id": "u-dana",
        "user_name": "dana.example",
        "name": "Dana Example",
        "email": "dana@example.com",
        "employee_number": "E-1001",
    },
}
CAPS = [
    {
        "sys_id": "c1",
        "name": "Finance",
        "short_description": "Plan, record and report money.",
        "owned_by": ref("u-lee"),
        "install_status": "1",
    },
    {
        "sys_id": "c2",
        "name": "Accounts payable",
        "short_description": "Pay suppliers correctly and on time.",
        "parent": ref("c1"),
        "owned_by": ref("u-dana"),
        "install_status": "1",
    },
    {"sys_id": "c3", "name": "Supplier onboarding", "parent": ref("c2"), "install_status": "1"},
    {
        "sys_id": "c4",
        "name": "Cheque printing",
        "parent": ref("c2"),
        "owned_by": ref("u-dana"),
        "install_status": "7",
    },
]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "x")


def capabilities(http, source=None) -> dict[str, dict]:
    params = {"source": source} if source else {}
    return {c["id"]: c for c in http.get("/api/v1/capabilities", params=params).json()}


def test_pascal_ids_match_the_workbook_style():
    assert pascal("Accounts payable") == "AccountsPayable"
    assert pascal("Order-to-cash (O2C)") == "OrderToCashO2C"


def test_the_capability_map_with_its_owner_roles(http):
    assert http.put("/api/v1/sources/servicenow-ea", json={"category": "ea"}).status_code == 200
    assert main([*API, *EA], http=http, source_http=servicenow(CAPS, USERS)) == 0
    caps = capabilities(http, "servicenow-ea")
    assert set(caps) == {"Finance", "AccountsPayable", "SupplierOnboarding"}  # the retired one is left out
    ap = caps["AccountsPayable"]
    assert ap["parentCapability"] == "Finance" and ap["ownerRole"] == "AccountsPayable.Owner"
    assert ap["definition"] == "Pay suppliers correctly and on time."
    assert caps["SupplierOnboarding"]["parentCapability"] == "AccountsPayable"
    assert "ownerRole" not in caps["SupplierOnboarding"]
    role = http.get("/api/v1/roles/AccountsPayable.Owner").json()
    assert role["name"] == "Owner of Accounts payable"
    people = {p["id"]: p for p in http.get("/api/v1/people").json()}
    assert people["E-1001"]["rolesFilled"] in ("AccountsPayable.Owner", ["AccountsPayable.Owner"])
    assert people["E-0042"]["rolesFilled"] in ("Finance.Owner", ["Finance.Owner"])


def test_an_id_field_instead_of_the_name(http):
    caps = [dict(c, number=f"BCAP{i:04d}") for i, c in enumerate(CAPS, 1)]
    assert main([*API, *EA, "--id-field", "number"], http=http, source_http=servicenow(caps, USERS)) == 0
    got = capabilities(http, "servicenow-ea")
    assert set(got) == {"BCAP0001", "BCAP0002", "BCAP0003"}
    assert got["BCAP0002"]["parentCapability"] == "BCAP0001"


def test_two_capabilities_with_one_name_stop_the_load(http, capsys):
    caps = [
        *CAPS[:3],
        {"sys_id": "c9", "name": "Accounts Payable", "parent": ref("c1"), "install_status": "1"},
    ]
    assert main([*API, *EA], http=http, source_http=servicenow(caps, USERS)) == 1
    assert "--id-field number" in capsys.readouterr().out
    assert capabilities(http, "servicenow-ea") == {}


def workbook(http, owner_role: str) -> None:
    """The workbook's view of the same capability, as source 'workbook' (category spreadsheet)."""
    items = [
        {"type": "roles", "op": "upsert", "entity": {"id": owner_role, "name": owner_role}},
        {
            "type": "capabilities",
            "op": "upsert",
            "entity": {"id": "AccountsPayable", "name": "Accounts payable", "ownerRole": owner_role},
        },
    ]
    cs = http.post("/api/v1/changesets", json={"source": "workbook"}).json()["id"]
    http.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
    assert http.post(f"/api/v1/changesets/{cs}/commit").status_code in (200, 201)


def test_ea_decides_the_owner_until_the_organization_says_otherwise(http):
    assert http.put("/api/v1/sources/servicenow-ea", json={"category": "ea"}).status_code == 200
    workbook(http, "ApManager")
    assert main([*API, *EA], http=http, source_http=servicenow(CAPS, USERS)) == 0
    domain = http.get("/api/v1/domains").json()["capabilities"]
    assert domain["authoritative"] == ["ea"]
    assert capabilities(http)["AccountsPayable"]["ownerRole"] == "AccountsPayable.Owner"  # EA wins
    # The decision is the organization's to change: the workbook decides from now on.
    r = http.put("/api/v1/domains/capabilities", json={"authoritative": ["spreadsheet"], "evidence": ["ea"]})
    assert r.status_code == 200, r.text
    assert capabilities(http)["AccountsPayable"]["ownerRole"] == "ApManager"
    assert http.delete("/api/v1/domains/capabilities").status_code == 200  # back to the default
    assert capabilities(http)["AccountsPayable"]["ownerRole"] == "AccountsPayable.Owner"


APPS = [
    {"sys_id": "a-erp", "number": "APM0001000", "name": "Oracle Fusion ERP", "install_status": "1"},
    {"sys_id": "a-bank", "number": "APM0002000", "name": "Bank portal", "install_status": "1"},
]
PROVIDES = {"value": "rt1", "display_value": "Provided by::Provides"}
RELS = [
    {"sys_id": "r1", "parent": ref("c2"), "child": ref("a-erp"), "type": PROVIDES},
    {"sys_id": "r2", "parent": ref("c2"), "child": ref("a-bank"), "type": PROVIDES},
    {"sys_id": "r3", "parent": ref("c4"), "child": ref("a-erp"), "type": PROVIDES},  # retired capability
    {
        "sys_id": "r4",
        "parent": ref("a-erp"),
        "child": ref("srv1"),
        "type": {"value": "rt2", "display_value": "Runs on::Runs"},
    },
]


def systems(http, *rows) -> None:
    items = [{"type": "systems", "op": "upsert", "entity": e} for e in rows]
    cs = http.post("/api/v1/changesets", json={"source": "servicenow"}).json()["id"]
    http.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
    assert http.post(f"/api/v1/changesets/{cs}/commit").status_code in (200, 201)


def test_capabilities_link_to_the_applications_that_provide_them(http, capsys):
    systems(http, {"id": "APM0001000", "name": "Oracle Fusion ERP", "cmdbId": "a-erp"})
    assert main([*API, *EA], http=http, source_http=servicenow(CAPS, USERS, APPS, RELS)) == 0
    caps = capabilities(http, "servicenow-ea")
    assert caps["AccountsPayable"]["supportedBySystems"] in ("APM0001000", ["APM0001000"])
    assert not caps["Finance"].get("supportedBySystems")
    out = capsys.readouterr().out
    assert "1 links to supporting systems" in out and "Bank portal" in out and "load the CMDB first" in out


def test_without_the_cmdb_loaded_nothing_is_linked_and_the_load_still_works(http, capsys):
    assert main([*API, *EA], http=http, source_http=servicenow(CAPS, USERS, APPS, RELS)) == 0
    assert not capabilities(http, "servicenow-ea")["AccountsPayable"].get("supportedBySystems")
    assert "2 supporting applications VeroMesh does not hold yet" in capsys.readouterr().out
