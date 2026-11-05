from __future__ import annotations

import textwrap

from veromesh_connectors.cli import main


def run(http, *args) -> int:
    return main(["--api", "http://testserver", *args], http=http)


def write(path, text: str):
    path.write_text(textwrap.dedent(text).lstrip())
    return path


def test_csv_files_named_after_types(http, tmp_path, capsys):
    folder = tmp_path / "export"
    folder.mkdir()
    write(
        folder / "roles.csv",
        """
        ID,Name
        ApManager,Accounts payable manager
        Controller,Financial controller
    """,
    )
    write(
        folder / "People.csv",
        """
        ID;Name;Employee ID;Roles filled
        DanaExample;Dana Example;E-1001;ApManager
        LeeExample;Lee Example;E-0042;Controller
    """,
    )
    assert run(http, "csv", "import", str(folder), "--source", "hr") == 0
    out = capsys.readouterr().out
    assert "Read 4 records from export/" in out and "Committed changeset" in out


def test_csv_with_mapping_file_from_another_system(http, tmp_path, capsys):
    """An HR export with its own column names, translated by a mapping file."""
    hr = write(
        tmp_path / "workday_workers.csv",
        """
        Worker ID,First Name,Last Name,Work Email,Position ID,Job Title,Manager ID
        E-1001,Dana,Example,dana@example.com,POS-AP-MGR,Accounts payable manager,E-0042
        E-0042,Lee,Example,lee@example.com,POS-CTRL,Financial controller,
        E-1002,Sam,Example,sam@example.com,POS-AP-MGR,Accounts payable manager,E-0042
    """,
    )
    mapping = write(
        tmp_path / "workday.yaml",
        """
        rules:
          - type: people
            id: "{Worker ID}"
            fields:
              name: "{First Name} {Last Name}"
              employeeId: "{Worker ID}"
              email: "{Work Email}"
              rolesFilled: "{Position ID}"
              reportsTo: "{Manager ID}"
          - type: roles
            id: "{Position ID}"
            fields:
              name: "{Job Title}"
    """,
    )
    assert run(http, "csv", "import", str(hr), "--source", "hr", "--mapping", str(mapping)) == 0
    out = capsys.readouterr().out
    assert "0 errors, 0 violations, 0 warnings" in out
    people = http.get("/api/v1/people", params={"source": "hr"}).json()
    dana = next(p for p in people if p["id"] == "E-1001")
    assert (
        dana["name"] == "Dana Example"
        and dana["rolesFilled"] == ["POS-AP-MGR"]
        and dana["reportsTo"] == "E-0042"
    )
    assert (
        len(http.get("/api/v1/roles", params={"source": "hr"}).json()) == 2
    )  # same role on two rows, merged


def test_mapping_problems_point_at_the_line(http, tmp_path, capsys):
    hr = write(
        tmp_path / "w.csv",
        """
        Worker ID,Name
        E-1,Dana
    """,
    )
    mapping = write(
        tmp_path / "m.yaml",
        """
        rules:
          - type: people
            id: "{Worker ID}"
            fields: {name: "{Full Name}"}
    """,
    )
    assert run(http, "csv", "import", str(hr), "--source", "hr", "--mapping", str(mapping)) == 1
    assert "w.csv line 2: column 'Full Name' not found" in capsys.readouterr().out


def test_conflicting_definitions_in_the_source(http, tmp_path, capsys):
    f = write(
        tmp_path / "roles.csv",
        """
        ID,Name
        R1,One
        R1,Uno
    """,
    )
    assert run(http, "csv", "import", str(f), "--source", "x") == 1
    assert "roles.csv line 3: roles 'R1' is also defined at roles.csv line 2" in capsys.readouterr().out


def test_unknown_type_and_columns(http, tmp_path, capsys):
    write(tmp_path / "widgets.csv", "ID,Name\nW1,Widget\n")
    assert run(http, "csv", "import", str(tmp_path / "widgets.csv"), "--source", "x") == 1
    assert "does not match an entity type" in capsys.readouterr().out
    write(tmp_path / "roles.csv", "ID,Name,Colour\nR1,One,red\n")
    assert run(http, "csv", "import", str(tmp_path / "roles.csv"), "--source", "x") == 1
    assert "unknown column 'Colour'" in capsys.readouterr().out
