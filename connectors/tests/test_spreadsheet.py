from __future__ import annotations

from openpyxl import load_workbook

from veromesh_connectors.cli import main


def run(http, *args) -> int:
    if args and args[0] in ("template", "import", "export"):
        args = ("spreadsheet", *args)
    return main(["--api", "http://testserver", *args], http=http)


def set_cell(path, sheet: str, rid: str, column: str, value) -> None:
    wb = load_workbook(path)
    ws = wb[sheet]
    headers = [c.value for c in ws[1]]
    row = next(r for r in ws.iter_rows(min_row=2) if r[0].value == rid)
    row[headers.index(column)].value = value
    wb.save(path)


def test_template_is_generated_from_the_api_schema(http, tmp_path, capsys):
    out = tmp_path / "t.xlsx"
    assert run(http, "template", "-o", str(out)) == 0
    wb = load_workbook(out)
    assert wb.sheetnames[0] == "How to use" and "Authorities" in wb.sheetnames and len(wb.sheetnames) == 16
    assert [c.value for c in wb["Impacts"][1]][0] == "Action"  # no ID column for types without one
    assert "Roles, People, Agents" in wb["Authorities"]["C1"].comment.text


def test_example_round_trip_through_the_api(http, tmp_path, capsys):
    ex = tmp_path / "example.xlsx"
    assert run(http, "template", "--example", "-o", str(ex)) == 0
    assert run(http, "import", str(ex), "--source", "p2p") == 0
    out = capsys.readouterr().out
    assert "0 errors, 0 violations, 0 warnings" in out and "Committed changeset" in out

    assert run(http, "sources") == 0
    assert "p2p" in capsys.readouterr().out

    back = tmp_path / "back.xlsx"
    assert run(http, "export", "--source", "p2p", "-o", str(back)) == 0
    assert run(http, "import", str(back), "--source", "p2p", "--dry-run") == 0
    assert "0 errors, 0 violations, 0 warnings" in capsys.readouterr().out
    assert len(list(load_workbook(back)["Authorities"].iter_rows(min_row=2))) == 3


def test_violation_is_reported_against_sheet_and_row_and_nothing_is_written(http, tmp_path, capsys):
    ex = tmp_path / "example.xlsx"
    run(http, "template", "--example", "-o", str(ex))
    set_cell(ex, "Authorities", "AssistantApprovalAuthority", "Limit amount", 75000)
    assert run(http, "import", str(ex), "--source", "p2p") == 1
    out = capsys.readouterr().out
    assert "violation Authorities row" in out and "exceeds its parent" in out
    assert "Not written" in out
    run(http, "sources")
    assert "No sources loaded yet" in capsys.readouterr().out


def test_field_error_points_at_the_column(http, tmp_path, capsys):
    ex = tmp_path / "example.xlsx"
    run(http, "template", "--example", "-o", str(ex))
    set_cell(ex, "KPIs", "OnTimePaymentRate", "Owner role", "NoSuchRole")
    assert run(http, "import", str(ex), "--source", "p2p") == 1
    out = capsys.readouterr().out
    assert "error     KPIs row " in out and ", Owner role: No entity with ID 'NoSuchRole'" in out
    assert "Not written" in out


def test_workbook_problems_are_caught_before_calling_the_api(http, tmp_path, capsys):
    ex = tmp_path / "example.xlsx"
    run(http, "template", "--example", "-o", str(ex))
    set_cell(ex, "Roles", "Controller", "ID", "ApManager")
    assert run(http, "import", str(ex), "--source", "p2p") == 1
    out = capsys.readouterr().out
    assert "also defined at Roles row" in out and "nothing sent" in out


def test_references_to_another_source(http, tmp_path, capsys):
    """Roles and people loaded as source 'hr'; everything else as 'p2p', referencing them."""
    ex = tmp_path / "example.xlsx"
    run(http, "template", "--example", "-o", str(ex))
    hr_sheets = {"Roles", "People"}
    for name, keep in (("hr.xlsx", hr_sheets), ("p2p.xlsx", None)):
        wb = load_workbook(ex)
        for sheet in wb.sheetnames[1:]:
            if (keep is not None and sheet not in keep) or (keep is None and sheet in hr_sheets):
                wb[sheet].delete_rows(2, wb[sheet].max_row)
        wb.save(tmp_path / name)
    assert run(http, "import", str(tmp_path / "hr.xlsx"), "--source", "hr") == 0
    assert run(http, "import", str(tmp_path / "p2p.xlsx"), "--source", "p2p") == 0
    out = capsys.readouterr().out
    assert out.count("Committed changeset") == 2 and "0 violations" in out


def test_unreachable_api(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("VEROMESH_RETRIES", "0")
    assert main(["--api", "http://127.0.0.1:9", "sources"]) == 2
    assert "cannot reach the VeroMesh API" in capsys.readouterr().out
