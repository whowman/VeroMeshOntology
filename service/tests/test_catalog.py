"""Data products' organizational context published into the catalog (G8)."""

from __future__ import annotations

from test_provenance import client, load  # noqa: F401

API = "/api/v1"
URN = "urn:li:dataProduct:supplier-invoices"


def test_context_is_written_onto_the_catalog_entry(client, fake_datahub):  # noqa: F811
    fake_datahub.properties[URN] = {"team": "finance", "veromesh.stale": "from an earlier publish"}
    r = client.post(f"{API}/catalog/data-products")
    assert r.status_code == 200, r.json()
    report = r.json()
    assert [p["id"] for p in report["published"]] == ["SupplierInvoices"]
    assert report["noCatalogUrn"] == ["SupplierPayments"] and report["notInCatalog"] == []

    props = fake_datahub.properties[URN]
    assert props["team"] == "finance"  # properties other tools set are kept
    assert "veromesh.stale" not in props  # VeroMesh's own are replaced
    assert props["veromesh.VeroMesh ID"] == "SupplierInvoices"
    assert props["veromesh.owner role"] == "Accounts payable manager"
    assert props["veromesh.feeds data products"] == "Supplier payments"
    assert props["veromesh.agents consuming it"] == "Invoice assistant"
    assert props["veromesh.data elements"] == "Supplier bank account number"
    assert props["veromesh.runs on systems"]
    # Roles only: no person's name, email or employee ID reaches the catalog.
    assert not any(x in v for v in props.values() for x in ("Dana", "Lee", "@", "E-1001"))


def test_a_urn_missing_from_the_catalog_is_reported(client, fake_datahub):  # noqa: F811
    report = client.post(f"{API}/catalog/data-products").json()
    assert report["published"] == []
    assert report["notInCatalog"] == [{"id": "SupplierInvoices", "catalogUrn": URN}]
    assert "1 not found in the catalog" in report["summary"]


def test_publish_only_some_data_products(client, fake_datahub):  # noqa: F811
    fake_datahub.properties[URN] = {}
    report = client.post(f"{API}/catalog/data-products", json={"ids": ["SupplierPayments"]}).json()
    assert report["published"] == [] and report["noCatalogUrn"] == ["SupplierPayments"]
    assert fake_datahub.properties[URN] == {}
    r = client.post(f"{API}/catalog/data-products", json={"ids": ["Nope"]})
    assert r.status_code == 404 and "Nope" in r.json()["detail"]
