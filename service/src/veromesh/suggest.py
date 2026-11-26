"""Column binding suggestions from the catalog, for stewards to confirm or reject.

DataHub already marks columns: its classifiers and people put glossary terms and
tags on them (a PII classifier's "email address" term, a "bank account" tag). A
business data element names the terms or tags that mark its columns (its *Catalog
terms*). A column carrying one of them, and not yet bound to that element, is a
suggestion. Nothing is bound until a steward accepts it.

* Accepting writes the binding into the source ``catalog-suggestions`` (category
  ``catalog``), like any other fact: it carries its source and can be attested.
* Rejecting records who rejected which pair, so it is not suggested again.

The catalog connectors (dbt, Databricks Unity Catalog) also report the columns they read
and the tags on them, as DataHub would name the tags (``urn:li:tag:classification:phi``).
Those reports are matched the same way, so suggestions do not need DataHub to be
installed, and a column DataHub and a connector both know is suggested once.
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
COLUMNS_GRAPH = "https://veromesh.ai/graph/catalog-columns/"  # + the reporting source
VM = "https://veromesh.ai/ontology/core#"


def rejected(store: RDFStore) -> set[tuple[str, str]]:
    """(data element ID, column URN) pairs a steward rejected."""
    rows = store.query(
        f"""SELECT ?el ?col WHERE {{ GRAPH <{REJECTED_GRAPH}> {{
              ?r <{VM}rejectedElement> ?el ; <{VM}rejectedColumn> ?col }} }}"""
    )["results"]["bindings"]
    return {(r["el"]["value"], r["col"]["value"]) for r in rows}


def report_columns(store: RDFStore, source: str, columns: list[dict]) -> dict:
    """Replace what ``source`` reports about columns: each ``{"column", "dataset",
    "fieldPath", "terms"}`` (the column's DataHub schema-field URN, its dataset URN and
    name, and the tag or glossary term URNs on it)."""
    if not changesets.SOURCE_NAME.match(source):
        raise ValueError(f"'{source}' is not a source name.")
    lines = []
    for i, c in enumerate(columns):
        col, terms = c.get("column"), [str(t) for t in c.get("terms") or [] if t]
        if not col:
            raise ValueError(f"Column {i + 1} has no column URN.")
        node = f"{COLUMNS_GRAPH}{source}/{hashlib.sha1(str(col).encode()).hexdigest()[:16]}"
        props = [f"<{VM}column> {literal(str(col))}"]
        for key, pred in (("dataset", "dataset"), ("fieldPath", "fieldPath")):
            if c.get(key):
                props.append(f"<{VM}{pred}> {literal(str(c[key]))}")
        props += [f"<{VM}term> {literal(t)}" for t in sorted(set(terms))]
        lines.append(f"<{check_iri(node)}> " + " ;\n    ".join(props) + " .")
    store.load_turtle("\n".join(lines), COLUMNS_GRAPH + source, replace=True)
    return {"source": source, "columns": len(columns), "tagged": sum(1 for c in columns if c.get("terms"))}


def reported_columns(store: RDFStore) -> list[dict]:
    """Every column a catalog connector reported, with its terms and the reporting source."""
    rows = store.query(
        f"""SELECT ?g ?col ?ds ?fp ?term WHERE {{ GRAPH ?g {{
              ?c <{VM}column> ?col .
              OPTIONAL {{ ?c <{VM}dataset> ?ds }} OPTIONAL {{ ?c <{VM}fieldPath> ?fp }}
              OPTIONAL {{ ?c <{VM}term> ?term }} }}
              FILTER(STRSTARTS(STR(?g), "{COLUMNS_GRAPH}")) }}"""
    )["results"]["bindings"]
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        src = r["g"]["value"][len(COLUMNS_GRAPH) :]
        c = out.setdefault(
            (src, r["col"]["value"]),
            {
                "source": src,
                "column": r["col"]["value"],
                "dataset": r.get("ds", {}).get("value"),
                "fieldPath": r.get("fp", {}).get("value"),
                "terms": set(),
            },
        )
        if "term" in r:
            c["terms"].add(r["term"]["value"])
    return [{**c, "terms": sorted(c["terms"])} for c in out.values()]


def suggestions(store: RDFStore, base: str, datahub: DataHubPort) -> dict:
    elements = [e for e in changesets.list_entities(store, "data-elements", base) if e.get("catalogTerms")]
    bound = {
        (b["dataElement"], b["column"]) for b in changesets.list_entities(store, "column-bindings", base)
    }
    no = rejected(store)
    reported = reported_columns(store)
    out, problems = [], []
    for el in sorted(elements, key=lambda e: (not e.get("critical"), e.get("name") or e["id"])):
        terms = el["catalogTerms"] if isinstance(el["catalogTerms"], list) else [el["catalogTerms"]]
        try:
            found = list(datahub.columns_with_terms(terms))
        except Exception as exc:  # the catalog is down or a term is unknown: report, carry on
            problems.append({"dataElement": el["id"], "problem": str(exc)})
            found = []
        wanted = set(terms)
        found += [{**c, "terms": c["terms"]} for c in reported if wanted & set(c["terms"])]
        seen: set[str] = set()
        for hit in found:
            pair = (el["id"], hit["column"])
            if pair in bound or pair in no or hit["column"] in seen:
                continue
            seen.add(hit["column"])
            out.append(
                {
                    "dataElement": el["id"],
                    "name": el.get("name") or el["id"],
                    "critical": el.get("critical") is True,
                    "column": hit["column"],
                    "dataset": hit.get("dataset"),
                    "fieldPath": hit.get("fieldPath"),
                    "matched": sorted(set(hit.get("terms", [])) & set(terms)),
                    **({"reportedBy": hit["source"]} if hit.get("source") else {}),
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
