"""dbt: data products, lineage, owners, sensitivity and KPIs from a project's run artifacts."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from veromesh_connectors.cli import main

API = ["--api", "http://testserver"]


def col(name, cls=None):
    return {"name": name, "data_type": "string", "meta": {"classification": cls} if cls else {}}


def table(kind, db, sch, name, deps=(), owner=None, cols=(), desc=None):
    uid = f"{kind}.acme.{db}_{sch}_{name}"
    return uid, {
        "unique_id": uid,
        "resource_type": kind,
        "database": db,
        "schema": sch,
        "name": name,
        "description": desc,
        "depends_on": {"nodes": list(deps)},
        "meta": {"owner": owner} if owner else {},
        "columns": {c["name"]: c for c in cols},
    }


SRC, src = table(
    "source",
    "finance",
    "bronze",
    "ap_invoices",
    owner="dana@example.com",
    cols=[col("invoice_id", "internal"), col("supplier_tax_id", "pii")],
)
MID, mid = table(
    "model",
    "finance",
    "silver",
    "invoices",
    [SRC],
    owner="dana@example.com",
    cols=[col("invoice_id", "internal")],
    desc="Cleaned supplier invoices.",
)
TOP, top = table(
    "model",
    "finance",
    "gold",
    "on_time_payment",
    [MID],
    owner="lee@example.com",
    cols=[col("rate", "public")],
)
TEST = "test.acme.not_null_invoices_invoice_id"
MANIFEST = {
    "metadata": {"adapter_type": "databricks", "project_name": "acme"},
    "sources": {SRC: src},
    "nodes": {
        MID: mid,
        TOP: top,
        TEST: {"unique_id": TEST, "resource_type": "test", "depends_on": {"nodes": [MID]}},
    },
    "metrics": {
        "metric.acme.on_time_payment_rate": {"name": "on_time_payment_rate", "depends_on": {"nodes": [TOP]}},
        "metric.acme.dpo": {
            "name": "dpo",
            "meta": {"veromesh_kpi": "DaysPayableOutstanding"},
            "depends_on": {"nodes": [TOP, MID]},
        },
        "metric.acme.untracked": {"name": "untracked_metric", "depends_on": {"nodes": [TOP]}},
    },
}
RUN = {"results": [{"unique_id": MID, "status": "error"}, {"unique_id": TOP, "status": "skipped"}]}


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
        {"type": "systems", "op": "upsert", "entity": {"id": "LAKE", "name": "Lakehouse"}},
        {
            "type": "kpis",
            "op": "upsert",
            "entity": {"id": "OnTimePaymentRate", "name": "On-time payment rate"},
        },
        {
            "type": "kpis",
            "op": "upsert",
            "entity": {"id": "DaysPayableOutstanding", "name": "Days payable outstanding"},
        },
    ]
    cs = http.post("/api/v1/changesets", json={"source": "hr"}).json()["id"]
    http.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
    r = http.post(f"/api/v1/changesets/{cs}/commit")
    assert r.status_code in (200, 201), r.text


@pytest.fixture
def target(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    (tmp_path / "run_results.json").write_text(json.dumps(RUN))
    return tmp_path


def products(http):
    return {d["id"]: d for d in http.get("/api/v1/data-products", params={"source": "dbt"}).json()}


def test_tables_lineage_owners_and_sensitivity(http, target, capsys):
    seed(http)
    assert main([*API, "dbt", "import", "--dir", str(target), "--runs-on", "LAKE"], http=http) == 0
    dps = products(http)
    assert set(dps) == {
        "finance.bronze.ap_invoices",
        "finance.silver.invoices",
        "finance.gold.on_time_payment",
    }  # no tests
    b, s = dps["finance.bronze.ap_invoices"], dps["finance.silver.invoices"]
    assert b["feedsDataProducts"] in ("finance.silver.invoices", ["finance.silver.invoices"])
    assert s["feedsDataProducts"] in ("finance.gold.on_time_payment", ["finance.gold.on_time_payment"])
    assert b["sensitivity"] == "Restricted" and s["sensitivity"] == "Internal"  # the highest column
    assert dps["finance.gold.on_time_payment"]["sensitivity"] == "Public"
    assert s["runsOnSystems"] in ("LAKE", ["LAKE"])
    assert s["catalogUrn"] == "urn:li:dataset:(urn:li:dataPlatform:databricks,finance.silver.invoices,PROD)"
    assert s["ownerRole"] == "finance.silver.invoices.Owner"
    people = {p["id"]: p for p in http.get("/api/v1/people", params={"source": "dbt"}).json()}
    assert set(people["E-1001"]["rolesFilled"]) == {
        "finance.bronze.ap_invoices.Owner",
        "finance.silver.invoices.Owner",
    }
    out = capsys.readouterr().out
    assert "1 model failed in the last run (1 skipped after)" in out and "1 metric is not a KPI" in out


def test_metrics_link_the_kpis_the_model_tracks(http, target):
    seed(http)
    assert main([*API, "dbt", "import", "--dir", str(target)], http=http) == 0
    kpis = {k["id"]: k for k in http.get("/api/v1/kpis").json()}
    assert kpis["OnTimePaymentRate"]["computedFromDataProducts"] in (
        "finance.gold.on_time_payment",
        ["finance.gold.on_time_payment"],
    )
    assert set(kpis["DaysPayableOutstanding"]["computedFromDataProducts"]) == {
        "finance.gold.on_time_payment",
        "finance.silver.invoices",
    }
    assert kpis["OnTimePaymentRate"]["name"] == "On-time payment rate"  # the name stays the model's
    assert "UntrackedMetric" not in kpis  # dbt says how a KPI is computed, not that it is tracked


def test_data_change_impact_follows_the_lineage(http, target):
    seed(http)
    assert main([*API, "dbt", "import", "--dir", str(target), "--runs-on", "LAKE"], http=http) == 0
    got = http.get("/api/v1/impact/data-products/finance.bronze.ap_invoices").json()
    assert [d["id"] for d in got["downstream"]] == ["finance.silver.invoices", "finance.gold.on_time_payment"]
    assert {k["id"] for k in got["kpis"]} == {"OnTimePaymentRate", "DaysPayableOutstanding"}
    by_urn = http.get(
        "/api/v1/impact/data-products",
        params={"urn": "urn:li:dataset:(urn:li:dataPlatform:databricks,finance.bronze.ap_invoices,PROD)"},
    ).json()
    assert by_urn["dataProduct"]["id"] == "finance.bronze.ap_invoices"


def test_dbt_cloud_job_artifacts(http, monkeypatch):
    seed(http)
    monkeypatch.setenv("DBT_CLOUD_TOKEN", "svc-token")
    app = FastAPI()

    @app.get("/api/v2/accounts/{account}/jobs/{job}/artifacts/{name}")
    def artifact(account: int, job: int, name: str, authorization: str = Header("")):
        if authorization != "Bearer svc-token":
            raise HTTPException(401)
        if (account, job) != (70, 42):
            raise HTTPException(404)
        return {"manifest.json": MANIFEST, "run_results.json": RUN}.get(name) or {}

    cloud = ["dbt", "import", "--dbt-cloud", "--url", "http://testserver", "--account", "70", "--job", "42"]
    assert main([*API, *cloud], http=http, source_http=TestClient(app)) == 0
    assert len(products(http)) == 3


def test_a_missing_manifest_sends_nothing(http, tmp_path):
    assert main([*API, "dbt", "import", "--dir", str(tmp_path)], http=http) != 0
    assert products(http) == {}
