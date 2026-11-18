"""The data element inventory: column bindings, coverage, and columns in the catalog."""

from __future__ import annotations

from test_provenance import client, load  # noqa: F401

API = "/api/v1"
COL = "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:sap,erp.lfbk,PROD),bankn)"
COL3 = "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.suppliers,PROD),name)"
COL2 = "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.suppliers,PROD),iban)"


def test_the_example_binding_loads(client):  # noqa: F811
    rows = client.get(f"{API}/column-bindings").json()
    assert [(r["dataElement"], r["column"]) for r in rows] == [("SupplierBankAccount", COL)]


def test_coverage_of_the_example(client):  # noqa: F811
    cov = client.get(f"{API}/data-elements/coverage").json()
    (el,) = cov["elements"]
    assert el["id"] == "SupplierBankAccount" and el["critical"] is True
    assert el["columns"] == [COL]
    assert el["checks"] == {
        "defined": True,
        "owned": True,
        "sourced": True,
        "classified": True,
        "bound": True,
        "attested": False,  # nobody has attested it yet
    }
    assert el["gaps"] == ["attested"]
    assert cov["critical"]["elements"] == 1 and cov["critical"]["bound"] == {"count": 1, "share": 1.0}
    assert cov["critical"]["complete"] == 0


def test_attesting_the_element_completes_it(client):  # noqa: F811
    r = client.post(
        f"{API}/data-elements/SupplierBankAccount/attestations",
        json={"verdict": "attested", "attester": "LeeExample", "role": "Controller"},
    )
    assert r.status_code in (200, 201), r.text
    el = client.get(f"{API}/data-elements/coverage").json()["elements"][0]
    assert el["checks"]["attested"] is True and el["gaps"] == []


def test_bulk_binding_through_a_changeset_and_scope_by_product(client):  # noqa: F811
    load(
        client,
        "catalog-scan",
        [
            {"type": "data-elements", "entity": {"id": "SupplierName", "name": "Supplier name"}},
            {"type": "column-bindings", "entity": {"dataElement": "SupplierBankAccount", "column": COL2}},
            {"type": "column-bindings", "entity": {"dataElement": "SupplierName", "column": COL3}},
        ],
    )
    cov = client.get(f"{API}/data-elements/coverage").json()
    by_id = {e["id"]: e for e in cov["elements"]}
    assert by_id["SupplierBankAccount"]["columns"] == sorted([COL, COL2])
    assert set(by_id["SupplierName"]["gaps"]) == {"defined", "owned", "sourced", "classified", "attested"}
    assert cov["all"]["elements"] == 2 and cov["critical"]["elements"] == 1
    # One value stream: the supplier invoices product contains only the bank account.
    scoped = client.get(f"{API}/data-elements/coverage", params={"dataProduct": "SupplierInvoices"}).json()
    assert [e["id"] for e in scoped["elements"]] == ["SupplierBankAccount"]
    assert client.get(f"{API}/data-elements/coverage", params={"dataProduct": "Nope"}).status_code == 404
    only = client.get(f"{API}/data-elements/coverage", params={"criticalOnly": True}).json()
    assert [e["id"] for e in only["elements"]] == ["SupplierBankAccount"]


def test_a_bad_column_urn_is_refused(client):  # noqa: F811
    cs = client.post(f"{API}/changesets", json={"source": "bad", "mode": "merge"}).json()["id"]
    client.post(
        f"{API}/changesets/{cs}/items",
        json={
            "items": [
                {
                    "type": "column-bindings",
                    "op": "upsert",
                    "entity": {"dataElement": "SupplierBankAccount", "column": "erp.lfbk.bankn"},
                }
            ]
        },
    )
    r = client.post(f"{API}/changesets/{cs}/commit")
    assert r.status_code == 409 and "schema field URN" in r.text


def test_columns_are_published_to_the_catalog(client, fake_datahub):  # noqa: F811
    from veromesh.datahub import Binding

    # A class binding already on the column stays; an old data element entry is replaced.
    fake_datahub.bindings[COL] = [
        Binding(class_iri="https://veromesh.ai/ontology/core#Party", ontology_iri="core"),
        Binding(class_iri="x:old", ontology_iri="veromesh:data-elements", relation="holdsDataElement"),
    ]
    r = client.post(f"{API}/catalog/columns")
    assert r.status_code == 200, r.text
    assert r.json()["published"] == [{"column": COL, "dataElements": ["SupplierBankAccount"]}]
    rows = [b.to_aspect_dict() for b in fake_datahub.bindings[COL]]
    assert [b["relation"] for b in rows] == ["represents", "holdsDataElement"]
    assert rows[1]["label"] == "Supplier bank account number" and rows[1]["classIri"].endswith(
        "SupplierBankAccount"
    )
    assert client.post(f"{API}/catalog/columns", json={"ids": ["Nope"]}).status_code == 404
