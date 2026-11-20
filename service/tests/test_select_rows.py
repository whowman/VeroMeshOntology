"""Rows decoded a row at a time are exactly what the JSON results give."""

from __future__ import annotations

import os

import pyoxigraph as ox
import pytest

from veromesh import rdf_store
from veromesh.rdf_store import MemoryStore, fuseki_store

G = "https://example.com/g"
AWKWARD = [
    '"plain"',
    '"with \\"quotes\\" and \\\\ backslash"',
    '"tab\\there, newline\\nhere, return\\rhere"',
    '"unicode é 🤖 \\u2028 line separator \\u0085 next line"',
    '"hello"@en',
    '"hallo"@de-CH',
    '"42"^^<http://www.w3.org/2001/XMLSchema#integer>',
    '"-7"^^<http://www.w3.org/2001/XMLSchema#integer>',
    '"1.50"^^<http://www.w3.org/2001/XMLSchema#decimal>',
    '"1.0E3"^^<http://www.w3.org/2001/XMLSchema#double>',
    '"true"^^<http://www.w3.org/2001/XMLSchema#boolean>',
    '"2026-11-20T12:00:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime>',
    '"{\\"name\\": \\"x\\", \\"list\\": [1, 2]}"',
    '""',
    "<https://example.com/other>",
]
QUERY = (
    f"SELECT ?s ?p ?o ?missing WHERE {{ GRAPH <{G}> {{ ?s ?p ?o }} OPTIONAL {{ ?s <urn:none> ?missing }} }}"
)


def data() -> str:
    return "".join(
        f"<https://example.com/s{i}> <https://example.com/p> {o} .\n" for i, o in enumerate(AWKWARD)
    )


def canonical(rows):
    return sorted(sorted((k, tuple(sorted(v.items()))) for k, v in r.items()) for r in rows)


def test_the_in_memory_store_gives_the_json_rows():
    store = MemoryStore()
    store.load_turtle(data(), G)
    assert canonical(store.select(QUERY)) == canonical(store.query(QUERY)["results"]["bindings"])


def test_tsv_is_read_as_the_json_rows():
    store = MemoryStore()
    store.load_turtle(data(), G)
    tsv = store._store.query(QUERY).serialize(format=ox.QueryResultsFormat.TSV).decode("utf-8")
    chunks = [tsv[i : i + 7] for i in range(0, len(tsv), 7)]  # arriving in small pieces
    rows = rdf_store.tsv_rows(rdf_store._lines(chunks))
    assert canonical(rows) == canonical(store.query(QUERY)["results"]["bindings"])
    assert len(rows) == len(AWKWARD)


def test_turtle_short_forms():
    t = rdf_store._tsv_term
    x = rdf_store.XSD
    assert t("42") == {"type": "literal", "value": "42", "datatype": x + "integer"}
    assert t("1.5") == {"type": "literal", "value": "1.5", "datatype": x + "decimal"}
    assert t("1.0e3") == {"type": "literal", "value": "1.0e3", "datatype": x + "double"}
    assert t("true") == {"type": "literal", "value": "true", "datatype": x + "boolean"}
    assert t('"x"^^<http://www.w3.org/2001/XMLSchema#string>') == {"type": "literal", "value": "x"}
    assert t("_:b0") == {"type": "bnode", "value": "b0"}
    assert t("") is None


@pytest.mark.skipif(not os.environ.get("VEROMESH_TEST_FUSEKI_URL"), reason="no Fuseki server")
def test_fuseki_gives_the_json_rows():
    store = fuseki_store(os.environ["VEROMESH_TEST_FUSEKI_URL"])
    store.update(f"DROP SILENT GRAPH <{G}>")
    store.load_turtle(data(), G)
    rows = store.select(QUERY)
    assert canonical(rows) == canonical(store.query(QUERY)["results"]["bindings"])
    assert len(rows) == len(AWKWARD)
    store.update(f"DROP SILENT GRAPH <{G}>")
