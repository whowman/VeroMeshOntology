"""Column binding suggestions from the catalog, for stewards to confirm or reject.

DataHub already marks columns: its classifiers and people put glossary terms and
tags on them (a PII classifier's "email address" term, a "bank account" tag). A
business data element names the terms or tags that mark its columns (its *Catalog
terms*). A column carrying one of them, and not yet bound to that element, is a
suggestion. Nothing is bound until a steward accepts it.

* Accepting writes the binding into the source ``catalog-suggestions`` (category
  ``catalog``), like any other fact: it carries its source and can be attested.
* Rejecting records who rejected which pair, so it is not suggested again.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from . import changesets
from . import provenance as P
from .datahub import DataHubPort
from .ontology import check_iri, literal
from .rdf_store import RDFStore

SOURCE = "catalog-suggestions"
REJECTED_GRAPH = "https://veromesh.ai/graph/suggestions-rejected"
VM = "https://veromesh.ai/ontology/core#"


def rejected(store: RDFStore) -> set[tuple[str, str]]:
    """(data element ID, column URN) pairs a steward rejected."""
    rows = store.query(
        f"""SELECT ?el ?col WHERE {{ GRAPH <{REJECTED_GRAPH}> {{
              ?r <{VM}rejectedElement> ?el ; <{VM}rejectedColumn> ?col }} }}"""
    )["results"]["bindings"]
    return {(r["el"]["value"], r["col"]["value"]) for r in rows}


def suggestions(store: RDFStore, base: str, datahub: DataHubPort) -> dict:
    elements = [e for e in changesets.list_entities(store, "data-elements", base) if e.get("catalogTerms")]
    bound = {
        (b["dataElement"], b["column"]) for b in changesets.list_entities(store, "column-bindings", base)
    }
    no = rejected(store)
    out, problems = [], []
    for el in sorted(elements, key=lambda e: (not e.get("critical"), e.get("name") or e["id"])):
        terms = el["catalogTerms"]
        try:
            found = datahub.columns_with_terms(terms)
        except Exception as exc:  # the catalog is down or a term is unknown: report, carry on
            problems.append({"dataElement": el["id"], "problem": str(exc)})
            continue
        for hit in found:
            pair = (el["id"], hit["column"])
            if pair in bound or pair in no:
                continue
            out.append(
                {
                    "dataElement": el["id"],
                    "name": el.get("name") or el["id"],
                    "critical": el.get("critical") is True,
                    "column": hit["column"],
                    "dataset": hit.get("dataset"),
                    "fieldPath": hit.get("fieldPath"),
                    "matched": sorted(set(hit.get("terms", [])) & set(terms)),
                }
            )
    return {
        "suggestions": out,
        "elementsWithTerms": len(elements),
        "problems": problems,
    }


def _pairs(items: list[dict]) -> list[tuple[str, str]]:
    out = []
    for it in items:
        el, col = it.get("dataElement"), it.get("column")
        if not el or not col:
            raise ValueError("Each item needs a dataElement and a column.")
        out.append((str(el), str(col)))
    return out


def accept(store: RDFStore, base: str, ontology_dir: str, items: list[dict]) -> dict:
    """Bind the suggested columns: one merge changeset into the catalog-suggestions source."""
    pairs = _pairs(items)
    if SOURCE not in P.registry(store):
        P.register_source(
            store,
            SOURCE,
            "catalog",
            description="Column bindings a steward accepted from catalog suggestions.",
        )
    report = changesets.apply(
        store,
        source=SOURCE,
        items=[
            {"type": "column-bindings", "op": "upsert", "entity": {"dataElement": el, "column": col}}
            for el, col in pairs
        ],
        base=base,
        ontology_dir=ontology_dir,
        kind="api",
    )
    return {
        "bound": [{"dataElement": el, "column": col} for el, col in pairs],
        "changeset": report.get("changeset"),
    }


def reject(store: RDFStore, items: list[dict], *, by: str, note: str | None = None) -> dict:
    """Record that these pairs are not bindings, so they are not suggested again."""
    pairs = _pairs(items)
    at = datetime.now(UTC).isoformat()
    for el, col in pairs:
        node = f"{REJECTED_GRAPH}/{hashlib.sha1((el + chr(0) + col).encode()).hexdigest()[:16]}"
        store.update(
            f"""INSERT DATA {{ GRAPH <{REJECTED_GRAPH}> {{
                  <{check_iri(node)}> <{VM}rejectedElement> {literal(el)} ;
                      <{VM}rejectedColumn> {literal(col)} ;
                      <{VM}rejectedBy> {literal(by)} ;
                      <{VM}rejectedAt> {literal(at)}
                      {f"; <{VM}note> {literal(note)}" if note else ""} .
               }} }}"""
        )
    return {"rejected": [{"dataElement": el, "column": col} for el, col in pairs]}
