from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from veromesh.api import create_app
from veromesh.config import Settings
from veromesh.workbook import SHEETS

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service, Settings(rdf_store="memory")))


@pytest.fixture
def example(client) -> bytes:
    r = client.get("/model/template", params={"example": True})
    assert r.status_code == 200
    return r.content


def edit(data: bytes, sheet: str, row: int, column: str, value) -> bytes:
    wb = load_workbook(io.BytesIO(data))
    ws = wb[sheet]
    headers = [c.value for c in ws[1]]
    if row > ws.max_row and value is None:
        return data
    ws.cell(row, headers.index(column) + 1).value = value
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def find_row(data: bytes, sheet: str, rid: str) -> int:
    ws = load_workbook(io.BytesIO(data))[sheet]
    return next(i for i, r in enumerate(ws.iter_rows(values_only=True), start=1) if r[0] == rid)


def upload(client, data: bytes, **params):
    return client.post("/model/import", params=params, files={"file": ("p2p.xlsx", data, XLSX)})


def test_template_has_a_sheet_per_entity_type_with_help(client):
    r = client.get("/model/template")
    assert r.headers["content-type"] == XLSX
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["How to use"] + [s.name for s in SHEETS]
    assert wb["Authorities"]["C1"].value == "Holder"
    assert "Roles, People, Agents" in wb["Authorities"]["C1"].comment.text


def test_example_imports_cleanly(client, example):
    r = upload(client, example, source="p2p", dry_run=True)
    body = r.json()
    assert r.status_code == 200, body
    assert body["summary"] == {"errors": 0, "violations": 0, "warnings": 0}
    assert body["written"] is False
    assert body["rows"]["Authorities"] == 3


def test_import_writes_source_with_provenance_and_exports_round_trip(client, example, service):
    r = upload(client, example, source="p2p")
    assert r.status_code == 200 and r.json()["written"] is True

    sources = client.get("/model/sources").json()
    assert [s["source"] for s in sources] == ["p2p"]
    assert sources[0]["file"] == "p2p.xlsx" and sources[0]["facts"] > 100

    exported = client.get("/model/export", params={"source": "p2p"})
    assert exported.status_code == 200
    again = upload(client, exported.content, source="p2p-copy", dry_run=True).json()
    assert again["summary"] == {"errors": 0, "violations": 0, "warnings": 0}
    assert again["rows"] == r.json()["rows"]


def test_reimport_replaces_the_source(client, example, service):
    upload(client, example, source="p2p")
    row = find_row(example, "Data elements", "SupplierBankAccount")
    smaller = edit(example, "Data elements", row, "ID", None)
    for col in (
        "Name",
        "Definition",
        "Critical",
        "Owner role",
        "Steward role",
        "Authoritative source",
        "Sensitivity",
        "Data categories",
        "Retention",
    ):
        smaller = edit(smaller, "Data elements", row, col, None)
    smaller = edit(
        smaller,
        "Data products",
        find_row(example, "Data products", "SupplierInvoices"),
        "Contains data elements",
        None,
    )
    r = upload(client, smaller, source="p2p")
    assert r.status_code == 200, r.json()
    res = service.store.query(
        "ASK { GRAPH <https://veromesh.ai/graph/source/spreadsheet/p2p> { ?s a <https://veromesh.ai/ontology/org#DataElement> } }"
    )
    assert res["boolean"] is False


@pytest.mark.parametrize(
    "sheet,rid,column,value,message",
    [
        ("KPIs", "OnTimePaymentRate", "Owner role", "NoSuchRole", "No row with ID 'NoSuchRole'"),
        ("KPIs", "OnTimePaymentRate", "Owner role", "DanaExample", "is on the People sheet"),
        ("Roles", "Controller", "ID", "ApManager", "already used"),
        ("Authorities", "ApApprovalAuthority", "Valid until", "31/12/2026", "not a date"),
        ("Authorities", "ApApprovalAuthority", "Holder", None, "Required value missing"),
        ("Systems", "Erp", "Criticality", "extreme", "not one of"),
        ("Data elements", "SupplierBankAccount", "Critical", "maybe", "not yes or no"),
    ],
)
def test_row_errors_block_the_import(client, example, service, sheet, rid, column, value, message):
    bad = edit(example, sheet, find_row(example, sheet, rid), column, value)
    r = upload(client, bad, source="p2p")
    body = r.json()
    assert r.status_code == 422
    assert body["written"] is False
    assert any(
        message in e["message"] and e["sheet"] == sheet and e["column"] == column for e in body["errors"]
    ), body["errors"]
    assert client.get("/model/sources").json() == []


def test_rule_violations_block_the_import_and_point_to_the_row(client, example):
    row = find_row(example, "Authorities", "AssistantApprovalAuthority")
    bad = edit(example, "Authorities", row, "Limit amount", 75000)
    body = upload(client, bad, source="p2p").json()
    assert body["written"] is False
    hits = [v for v in body["violations"] if "exceeds its parent" in v["message"]]
    assert hits and hits[0]["sheet"] == "Authorities" and hits[0]["row"] == row
    assert hits[0]["focus"] == "AssistantApprovalAuthority"
    coapproval = [v for v in body["violations"] if "drops the co-approval by Controller" in v["message"]]
    assert coapproval, body["violations"]


def test_gaps_are_warnings_and_do_not_block(client, example):
    row = find_row(example, "KPIs", "DuplicatePaymentRate")
    gap = edit(example, "KPIs", row, "Owner role", None)
    r = upload(client, gap, source="p2p")
    body = r.json()
    assert r.status_code == 200 and body["written"] is True
    assert any("no owner" in w["message"] and w["row"] == row for w in body["warnings"])


def test_bad_inputs(client, example):
    assert upload(client, example, source="Not Valid!").status_code == 400
    assert upload(client, b"not a spreadsheet", source="x").status_code == 400
    assert client.get("/model/export", params={"source": "missing"}).status_code == 404
