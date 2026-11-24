"""A data product is about to change: what is downstream, and whom to tell (gap 3, Gate 3)."""

from __future__ import annotations

import test_decide
from test_decide import API
from test_mcp import agent_app, call, mcp  # noqa: F401

ctx = test_decide.ctx


def test_a_change_reaches_every_owner_downstream(ctx):
    client, _, _ = ctx
    got = client.get(f"{API}/impact/data-products/SupplierInvoices").json()
    assert got["dataProduct"]["id"] == "SupplierInvoices"
    assert [d["id"] for d in got["downstream"]] == ["SupplierPayments"]
    assert [k["id"] for k in got["kpis"]] == ["DuplicatePaymentRate", "OnTimePaymentRate"]
    assert [o["id"] for o in got["objectives"]] == ["PaySuppliersCorrectly"]
    assert [c["id"] for c in got["capabilities"]] == []  # accounts payable produces them, consumes none
    assert [a["id"] for a in got["agents"]] == ["InvoiceAssistant"]
    roles = {e["role"]: e for e in got["notify"]}
    assert {"ApManager", "Controller"} <= set(roles)
    assert any("downstream of Supplier invoices" in w for e in got["notify"] for w in e["why"])
    by_urn = client.get(
        f"{API}/impact/data-products", params={"urn": "urn:li:dataProduct:supplier-invoices"}
    ).json()
    assert by_urn == got
    assert client.get(f"{API}/impact/data-products/Nope").status_code == 404


def test_the_end_of_the_line_has_nothing_downstream(ctx):
    client, _, _ = ctx
    got = client.get(f"{API}/impact/data-products/SupplierPayments").json()
    assert got["downstream"] == [] and [k["id"] for k in got["kpis"]] == ["DuplicatePaymentRate"]


def test_agents_ask_before_changing_data(mcp):  # noqa: F811
    client, log = mcp
    got, err = call(
        client,
        agent_app(),
        "impact_of_data_change",
        data_product="SupplierInvoices",
        purpose="renaming a column",
    )
    assert not err and got["downstream"][0]["id"] == "SupplierPayments"
    assert log.find(kind="query")[-1]["purpose"] == "renaming a column"
