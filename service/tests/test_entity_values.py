"""How entity field values are read: free text is one value; lists and references may split."""

from __future__ import annotations

from veromesh import entities as E

BASE = "https://example.org/id/"


def convert(type_: str, entity: dict):
    known = {"E2": "people", "E3": "people", "A": "roles", "B": "roles"}  # ID -> its type
    return E.to_triples(type_, entity, BASE, known.get)


def test_free_text_with_a_semicolon_is_one_value():
    out = convert(
        "roles", {"id": "Clinician", "name": "Clinician", "definition": "Treats patients; signs orders"}
    )
    assert out.errors == []
    assert [str(o) for _, p, o in out.triples if str(p).endswith("definition")] == [
        "Treats patients; signs orders"
    ]


def test_a_single_reference_still_refuses_two_values():
    out = convert("people", {"id": "E1", "name": "Ana", "reportsTo": "E2; E3"})
    assert [e.message for e in out.errors] == ["Only one value allowed."]


def test_a_list_field_still_splits():
    out = convert("people", {"id": "E1", "name": "Ana", "rolesFilled": "A; B"})
    assert out.errors == []
    assert sum(str(p).endswith("fills") for _, p, _ in out.triples) == 2
