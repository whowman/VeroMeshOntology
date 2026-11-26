"""Databricks Unity Catalog through a small stand-in for its REST APIs (CI has no harness)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.testclient import TestClient

from veromesh_connectors.cli import main

API = ["--api", "http://testserver"]
DBX = ["databricks", "import", "--url", "http://testserver/dbx"]
TABLES = {
    "finance.bronze.ap_invoices": {"owner": "data-engineering", "tags": ["internal", "pii"]},
    "finance.silver.invoices": {
        "owner": "dana@example.com",
        "steward": "lee@example.com",
        "tags": ["internal"],
    },
    "finance.gold.on_time_payment": {"owner": "nobody@example.com", "tags": ["public"]},
}
LINEAGE = [
    ("finance.bronze.ap_invoices", "finance.silver.invoices"),
    ("finance.silver.invoices", "finance.gold.on_time_payment"),
]


def workspace() -> TestClient:
    app = FastAPI()

    def auth(authorization: str = Header("")):
        if authorization != "Bearer tok":
            raise HTTPException(401)

    def split(full):
        c, s, n = full.split(".")
        return {"catalog_name": c, "schema_name": s, "name": n}

    @app.post("/dbx/oidc/v1/token")
    def token(client_id: str = Form(""), client_secret: str = Form("")):
        if (client_id, client_secret) != ("sp", "secret"):
            raise HTTPException(401)
        return {"access_token": "tok", "expires_in": 3600}

    @app.get("/dbx/api/2.1/unity-catalog/catalogs")
    def catalogs(authorization: str = Header("")):
        auth(authorization)
        return {"catalogs": [{"name": "finance"}, {"name": "sandbox"}]}

    @app.get("/dbx/api/2.1/unity-catalog/schemas")
    def schemas(catalog_name: str, authorization: str = Header("")):
        auth(authorization)
        return {
            "schemas": [{"name": s} for s in ("bronze", "silver", "gold")]
            if catalog_name == "finance"
            else []
        }

    @app.get("/dbx/api/2.1/unity-catalog/tables")
    def tables(
        catalog_name: str, schema_name: str, page_token: str | None = None, authorization: str = Header("")
    ):
        auth(authorization)
        rows = [
            {
                **split(f),
                "full_name": f,
                "owner": t["owner"],
                "properties": {"steward": t["steward"]} if "steward" in t else {},
            }
            for f, t in TABLES.items()
            if f.startswith(f"{catalog_name}.{schema_name}.")
        ]
        return {"tables": rows}

    @app.get("/dbx/api/2.0/lineage-tracking/table-lineage")
    def lineage(table_name: str, authorization: str = Header("")):
        auth(authorization)
        return {"downstreams": [{"tableInfo": split(d)} for s, d in LINEAGE if s == table_name]}

    @app.post("/dbx/api/2.0/sql/statements")
    def sql(body: dict, authorization: str = Header("")):
        auth(authorization)
        q = body["statement"]
        if "table_lineage" in q:
            cols, rows = ["source_table_full_name", "target_table_full_name"], [list(e) for e in LINEAGE]
        else:
            cols = ["catalog_name", "schema_name", "table_name", "column_name", "tag_name", "tag_value"]
            rows = [
                [*f.split("."), f"c{i}", "classification", tag]
                for f, t in TABLES.items()
                for i, tag in enumerate(t["tags"])
            ]
        return {
            "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": c} for c in cols]}},
            "result": {"data_array": rows},
        }

    return TestClient(app)


def seed(http):
    items = [
        {
            "type": "people",
            "op": "upsert",
            "entity": {"id": "E-1001", "name": "Dana Example", "email": "dana@example.com"},
        },
        {
            "type": "people",
            "op": "upsert",
            "entity": {"id": "E-0042", "name": "Lee Example", "email": "lee@example.com"},
        },
    ]
    cs = http.post("/api/v1/changesets", json={"source": "hr"}).json()["id"]
    http.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
    assert http.post(f"/api/v1/changesets/{cs}/commit").status_code in (200, 201)


def products(http):
    return {d["id"]: d for d in http.get("/api/v1/data-products", params={"source": "databricks"}).json()}


@pytest.mark.parametrize("warehouse", [None, "wh"])
def test_tables_owners_stewards_lineage(http, monkeypatch, capsys, warehouse):
    monkeypatch.setenv("DATABRICKS_TOKEN", "tok")
    seed(http)
    args = [*API, *DBX, "--catalog", "finance", *(["--warehouse", warehouse] if warehouse else [])]
    assert main(args, http=http, source_http=workspace()) == 0
    dps = products(http)
    assert set(dps) == set(TABLES)
    assert dps["finance.bronze.ap_invoices"]["feedsDataProducts"] in (
        "finance.silver.invoices",
        ["finance.silver.invoices"],
    )
    silver = dps["finance.silver.invoices"]
    assert (
        silver["ownerRole"] == "finance.silver.invoices.Owner"
        and silver["stewardRole"] == "finance.silver.invoices.Steward"
    )
    assert "ownerRole" not in dps["finance.bronze.ap_invoices"]  # a group owns it
    assert "ownerRole" not in dps["finance.gold.on_time_payment"]  # its owner matches no one
    if warehouse:
        assert dps["finance.bronze.ap_invoices"]["sensitivity"] == "Restricted"
        assert dps["finance.gold.on_time_payment"]["sensitivity"] == "Public"
    else:
        assert not any("sensitivity" in d for d in dps.values())
    out = capsys.readouterr().out
    assert "1 owned by a group, not a person" in out and "1 owner or steward matches no one" in out


def test_oauth_machine_to_machine(http, monkeypatch):
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    seed(http)
    assert main([*API, *DBX], http=http, source_http=workspace()) == 0
    assert len(products(http)) == 3
