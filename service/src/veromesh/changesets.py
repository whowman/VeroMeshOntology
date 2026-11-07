"""Changesets: the only way facts enter the organizational model.

Every client (the spreadsheet loader, the REST resources, later connectors and
the attestation workflow) writes through a changeset:

    create (source, mode) -> add items -> validate -> commit   (or discard)

* Each source owns one named graph. ``replace`` mode makes the changeset the
  whole new content of the source; ``merge`` upserts and deletes individual
  entities within it.
* Validation runs against the WHOLE model: references may point at entities
  from any source, and a change is rejected if it introduces a rule violation
  anywhere (e.g. deleting a role that a KPI in another source is owned by).
* Nothing is written until commit, and commit re-validates first.
* Each changeset records the source version it started from (the changeset
  that last wrote the source). Commit refuses if the source has changed since,
  so two concurrent loads can never silently overwrite each other.
* An idempotency key on create returns the existing changeset for the same
  source and key, and committing an already-committed changeset returns its
  report. Clients can therefore retry or resume any step safely.

Changesets and their items are stored in the RDF store, so they survive
restarts and remain as an audit trail after commit.

What the model believes is the *effective* model (provenance.py): per entity and
field, one source's value by the precedence rules. Validation checks the
effective model as it would be after the change, and commit records the
source's claims (with history) and stores the new effective model.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pyshacl import validate as shacl_validate
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from . import entities as E
from . import provenance as P
from .ontology import literal
from .rdf_store import RDFStore

SH = Namespace("http://www.w3.org/ns/shacl#")
VM = Namespace("https://veromesh.ai/ontology/core#")
SOURCE_GRAPH_BASE = "https://veromesh.ai/graph/source/"
PROVENANCE_GRAPH = "https://veromesh.ai/graph/provenance"
CHANGESET_GRAPH = "https://veromesh.ai/graph/changesets"
CHANGESET_BASE = "https://veromesh.ai/changeset/"
CORE_GRAPH = "https://veromesh.ai/ontology/core"
ORG_GRAPH = "https://veromesh.ai/ontology/org"
SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MODES = ("replace", "merge")
KINDS = ("api", "spreadsheet", "connector")


class Conflict(Exception):
    """The request conflicts with the current state (e.g. committing an invalid changeset)."""

    def __init__(self, message: str, report: dict | None = None) -> None:
        super().__init__(message)
        self.report = report


def _not_found(msg: str) -> Exception:
    from .service import NotFound

    return NotFound(msg)


def source_graph(source: str) -> str:
    return SOURCE_GRAPH_BASE + source


def check_source(source: str) -> str:
    if not SOURCE_NAME.match(source or ""):
        raise ValueError("Source name must be lowercase letters, digits and '-', up to 64 characters.")
    return source


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- reading the model


def _term(b: dict):
    t = b["type"]
    if t == "uri":
        return URIRef(b["value"])
    if t == "bnode":
        return BNode(b["value"])
    if "datatype" in b:
        return Literal(b["value"], datatype=URIRef(b["datatype"]))
    if "xml:lang" in b:
        return Literal(b["value"], lang=b["xml:lang"])
    return Literal(b["value"])


def read_sources(store: RDFStore) -> dict[str, Graph]:
    """All source graphs, keyed by graph IRI."""
    res = store.query(
        f"""SELECT ?g ?s ?p ?o WHERE {{ GRAPH ?g {{ ?s ?p ?o }}
            FILTER(STRSTARTS(STR(?g), "{SOURCE_GRAPH_BASE}")) }}"""
    )
    out: dict[str, Graph] = {}
    for b in res["results"]["bindings"]:
        out.setdefault(b["g"]["value"], Graph()).add((_term(b["s"]), _term(b["p"]), _term(b["o"])))
    return out


def model_graph(store: RDFStore, source: str | None = None, *, base: str = "") -> Graph:
    """One source's graph, or the effective model."""
    if source is not None:
        return read_sources(store).get(source_graph(source), Graph())
    P.ensure_effective(store, base)
    return P.effective_graph(store)


def list_entities(store: RDFStore, type_: str, base: str, source: str | None = None) -> list[dict]:
    E.check_type(type_)
    g = model_graph(store, source, base=base)
    return [E.to_entity(g, s, type_, base) for s in E.subjects_of_type(g, type_)]


def get_entity(store: RDFStore, type_: str, rid: str, base: str, as_of: str | None = None) -> dict:
    E.check_type(type_)
    if as_of:
        return P.as_of(store, type_, rid, as_of)
    subj = E.iri_for(base, rid)
    g = model_graph(store, base=base)
    if type_ not in E.types_of(g, subj):
        raise _not_found(f"No {type_} with ID '{rid}'.")
    found_in = sorted({c.source for c in P.read_claims(store, entity=(type_, rid))})
    if not found_in:  # claims not recorded yet (data from before provenance)
        found_in = sorted(
            iri[len(SOURCE_GRAPH_BASE) :]
            for iri, sg in read_sources(store).items()
            if type_ in E.types_of(sg, subj)
        )
    return {**E.to_entity(g, subj, type_, base), "sources": found_in}


def source_version(store: RDFStore, source: str) -> str:
    """The changeset that last wrote ``source``, or 'none' for a source never written."""
    res = store.query(
        f"""SELECT ?cs WHERE {{ GRAPH <{PROVENANCE_GRAPH}> {{
              <{source_graph(source)}> <http://www.w3.org/ns/prov#wasGeneratedBy> ?cs }} }}"""
    )
    rows = res["results"]["bindings"]
    return rows[0]["cs"]["value"][len(CHANGESET_BASE) :] if rows else "none"


def list_sources(store: RDFStore) -> list[dict]:
    res = store.query(
        f"""
        PREFIX prov: <http://www.w3.org/ns/prov#>
        PREFIX dcterms: <http://purl.org/dc/terms/>
        SELECT ?g ?id ?kind ?file ?at ?n ?cs WHERE {{
          GRAPH <{PROVENANCE_GRAPH}> {{
            ?g dcterms:identifier ?id ; dcterms:type ?kind ; prov:generatedAtTime ?at .
            OPTIONAL {{ ?g dcterms:source ?file }}
            OPTIONAL {{ ?g <{VM.tripleCount}> ?n }}
            OPTIONAL {{ ?g prov:wasGeneratedBy ?cs }}
          }}
        }} ORDER BY ?id
        """
    )
    reg = P.registry(store)
    rows = [
        {
            "source": b["id"]["value"],
            "category": reg.get(b["id"]["value"], {}).get("category")
            or ("steward" if b["id"]["value"] == P.STEWARD_SOURCE else P.DEFAULT_CATEGORY),
            "ownerRole": reg.get(b["id"]["value"], {}).get("ownerRole"),
            "description": reg.get(b["id"]["value"], {}).get("description"),
            "kind": b["kind"]["value"],
            "graph": b["g"]["value"],
            "file": b.get("file", {}).get("value"),
            "updatedAt": b["at"]["value"],
            "facts": int(b["n"]["value"]) if "n" in b else None,
            "changeset": b["cs"]["value"][len(CHANGESET_BASE) :] if "cs" in b else None,
            "version": b["cs"]["value"][len(CHANGESET_BASE) :] if "cs" in b else None,
        }
        for b in res["results"]["bindings"]
    ]
    loaded = {r["source"] for r in rows}
    for name, info in sorted(reg.items()):
        if name not in loaded:  # registered, nothing loaded yet
            rows.append(
                {
                    "source": name,
                    **info,
                    "kind": None,
                    "graph": source_graph(name),
                    "file": None,
                    "updatedAt": None,
                    "facts": None,
                    "changeset": None,
                    "version": None,
                }
            )
    return sorted(rows, key=lambda r: r["source"])


# --------------------------------------------------------------------------- changeset storage


def _cs_iri(cs: str) -> str:
    return CHANGESET_BASE + cs


def _items_graph(cs: str) -> str:
    return CHANGESET_BASE + cs + "/items"


def _item_iri(cs: str, type_: str, rid: str) -> URIRef:
    return URIRef(f"{CHANGESET_BASE}{cs}/item/{type_}/{quote(rid, safe='._-')}")


def create(
    store: RDFStore,
    *,
    source: str,
    mode: str = "replace",
    kind: str = "api",
    description: str | None = None,
    file: str | None = None,
    connector: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    check_source(source)
    if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise ValueError("Idempotency key must be 1-128 letters, digits, '.', '_', ':' or '-'.")
    if idempotency_key:
        found = store.query(
            f"""SELECT ?c WHERE {{ GRAPH <{CHANGESET_GRAPH}> {{
                  ?c <{VM.source}> {literal(source)} ; <{VM.idempotencyKey}> {literal(idempotency_key)} ;
                     <{VM.status}> ?st . FILTER(?st != "discarded") }} }}"""
        )["results"]["bindings"]
        if found:
            return {**get(store, found[0]["c"]["value"][len(CHANGESET_BASE) :]), "reused": True}
    if mode not in MODES:
        raise ValueError(f"Mode must be one of: {', '.join(MODES)}.")
    if kind not in KINDS:
        raise ValueError(f"Kind must be one of: {', '.join(KINDS)}.")
    cs = uuid.uuid4().hex[:16]
    extra = ""
    if description:
        extra += f" ; <{VM.description}> {literal(description)}"
    if file:
        extra += f" ; <{VM.file}> {literal(file)}"
    if connector:
        extra += f" ; <{VM.connector}> {literal(connector)}"
    if idempotency_key:
        extra += f" ; <{VM.idempotencyKey}> {literal(idempotency_key)}"
    extra += f" ; <{VM.baseVersion}> {literal(source_version(store, source))}"
    store.update(
        f"""INSERT DATA {{ GRAPH <{CHANGESET_GRAPH}> {{
              <{_cs_iri(cs)}> a <{VM.Changeset}> ;
                <{VM.source}> {literal(source)} ; <{VM.mode}> "{mode}" ; <{VM.kind}> "{kind}" ;
                <{VM.status}> "open" ;
                <{VM.createdAt}> "{now()}"^^<{XSD.dateTime}> {extra} .
            }} }}"""
    )
    return {**get(store, cs), "reused": False}


def get(store: RDFStore, cs: str, *, with_report: bool = True) -> dict:
    if not re.fullmatch(r"[0-9a-f]{16}", cs or ""):
        raise _not_found(f"No changeset '{cs}'.")
    res = store.query(f"""SELECT ?p ?o WHERE {{ GRAPH <{CHANGESET_GRAPH}> {{ <{_cs_iri(cs)}> ?p ?o }} }}""")
    props = {b["p"]["value"]: b["o"]["value"] for b in res["results"]["bindings"]}
    if not props:
        raise _not_found(f"No changeset '{cs}'.")
    counts = store.query(
        f"""SELECT ?op (COUNT(?n) AS ?c) WHERE {{ GRAPH <{_items_graph(cs)}> {{ ?n <{VM.op}> ?op }} }} GROUP BY ?op"""
    )
    by_op = {b["op"]["value"]: int(b["c"]["value"]) for b in counts["results"]["bindings"]}
    out = {
        "id": cs,
        "source": props[str(VM.source)],
        "mode": props[str(VM.mode)],
        "kind": props[str(VM.kind)],
        "status": props[str(VM.status)],
        "createdAt": props[str(VM.createdAt)],
        "description": props.get(str(VM.description)),
        "file": props.get(str(VM.file)),
        "committedAt": props.get(str(VM.committedAt)),
        "connector": props.get(str(VM.connector)),
        "idempotencyKey": props.get(str(VM.idempotencyKey)),
        "baseVersion": props.get(str(VM.baseVersion)),
        "items": {"upsert": by_op.get("upsert", 0), "delete": by_op.get("delete", 0)},
    }
    if with_report and str(VM.report) in props:
        out["report"] = json.loads(props[str(VM.report)])
    return out


def list_changesets(
    store: RDFStore, *, source: str | None = None, status: str | None = None, limit: int = 50
) -> list[dict]:
    """Run history, newest first."""
    filters = ""
    if source:
        check_source(source)
        filters += f" ?c <{VM.source}> {literal(source)} ."
    if status:
        filters += f" ?c <{VM.status}> {literal(status)} ."
    res = store.query(
        f"""SELECT ?c ?at WHERE {{ GRAPH <{CHANGESET_GRAPH}> {{ ?c a <{VM.Changeset}> ; <{VM.createdAt}> ?at .{filters} }} }}
            ORDER BY DESC(?at) LIMIT {max(1, min(int(limit), 500))}"""
    )
    return [
        get(store, b["c"]["value"][len(CHANGESET_BASE) :], with_report=False)
        for b in res["results"]["bindings"]
    ]


def _set_status(store: RDFStore, cs: str, status: str, report: dict | None = None, extra: str = "") -> None:
    iri = _cs_iri(cs)
    ins = f'<{iri}> <{VM.status}> "{status}"'
    if report is not None:
        ins += f" ; <{VM.report}> {literal(json.dumps(report))}"
    if extra:
        ins += " ; " + extra
    store.update(
        f"""DELETE {{ GRAPH <{CHANGESET_GRAPH}> {{ <{iri}> <{VM.status}> ?s . <{iri}> <{VM.report}> ?r }} }}
            WHERE {{ GRAPH <{CHANGESET_GRAPH}> {{ <{iri}> <{VM.status}> ?s OPTIONAL {{ <{iri}> <{VM.report}> ?r }} }} }} ;
            INSERT DATA {{ GRAPH <{CHANGESET_GRAPH}> {{ {ins} . }} }}"""
    )


def _require_open(store: RDFStore, cs: str) -> dict:
    meta = get(store, cs, with_report=False)
    if meta["status"] in ("committed", "discarded"):
        raise Conflict(f"Changeset {cs} is {meta['status']}.")
    return meta


@dataclass
class Item:
    type: str
    id: str
    op: str
    entity: dict
    meta: dict | None = None  # observedAt, validFrom, validUntil


TIME_KEYS = {"observedAt": "dateTime", "validFrom": "date", "validUntil": "date"}


def _check_times(n: int, raw: dict) -> dict:
    out = {}
    for k, kind in TIME_KEYS.items():
        v = raw.get(k)
        if v in (None, ""):
            continue
        try:
            if kind == "date":
                datetime.fromisoformat(str(v)[:10])
                out[k] = str(v)[:10]
            else:
                d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                out[k] = (d if d.tzinfo else d.replace(tzinfo=UTC)).isoformat()
        except ValueError as exc:
            raise ValueError(f"Item {n}: {k} '{v}' is not an ISO {kind}.") from exc
    if out.get("validFrom") and out.get("validUntil") and out["validUntil"] < out["validFrom"]:
        raise ValueError(f"Item {n}: validUntil is before validFrom.")
    return out


def add_items(store: RDFStore, cs: str, items: list[dict]) -> dict:
    """Add or replace items: ``{"type": "kpis", "op": "upsert"|"delete", "entity": {...}}``.
    Delete items need only ``{"id": ...}`` as the entity."""
    _require_open(store, cs)
    g = Graph()
    nodes = []
    seen: dict[tuple[str, str], int] = {}
    for n, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError(f"Item {n}: must be an object with 'type', 'op' and 'entity'.")
        type_ = raw.get("type")
        op = raw.get("op", "upsert")
        entity = raw.get("entity") or {}
        try:
            E.check_type(type_)
        except LookupError as exc:
            raise ValueError(f"Item {n}: {exc}") from exc
        if op not in ("upsert", "delete"):
            raise ValueError(f"Item {n}: op must be 'upsert' or 'delete'.")
        if not isinstance(entity, dict):
            raise ValueError(f"Item {n}: entity must be an object.")
        rid = E.entity_id(type_, entity)
        if not rid:
            raise ValueError(f"Item {n} ({type_}): entity has no 'id'.")
        if (type_, rid) in seen:
            raise ValueError(
                f"Item {n}: {type_} '{rid}' appears twice in this batch (also item {seen[(type_, rid)]})."
            )
        seen[(type_, rid)] = n
        node = _item_iri(cs, type_, rid)
        nodes.append(node)
        g.add((node, VM.itemType, Literal(type_)))
        g.add((node, VM.itemId, Literal(rid)))
        g.add((node, VM.op, Literal(op)))
        g.add((node, VM.payload, Literal(json.dumps({**entity, "id": rid}, default=str))))
        times = _check_times(n, raw)
        if times:
            g.add((node, VM.itemMeta, Literal(json.dumps(times))))
    if nodes:
        for i in range(0, len(nodes), 200):
            values = " ".join(f"<{n}>" for n in nodes[i : i + 200])
            store.update(
                f"""DELETE {{ GRAPH <{_items_graph(cs)}> {{ ?n ?p ?o }} }}
                    WHERE {{ VALUES ?n {{ {values} }} GRAPH <{_items_graph(cs)}> {{ ?n ?p ?o }} }}"""
            )
        store.load_turtle(g.serialize(format="turtle"), _items_graph(cs), replace=False)
    _set_status(store, cs, "open")
    return get(store, cs, with_report=False)


def _load_items(store: RDFStore, cs: str) -> list[Item]:
    res = store.query(
        f"""SELECT ?t ?i ?op ?p ?m WHERE {{ GRAPH <{_items_graph(cs)}> {{
              ?n <{VM.itemType}> ?t ; <{VM.itemId}> ?i ; <{VM.op}> ?op ; <{VM.payload}> ?p
              OPTIONAL {{ ?n <{VM.itemMeta}> ?m }} }} }}"""
    )
    return [
        Item(
            b["t"]["value"],
            b["i"]["value"],
            b["op"]["value"],
            json.loads(b["p"]["value"]),
            json.loads(b["m"]["value"]) if "m" in b else None,
        )
        for b in res["results"]["bindings"]
    ]


def discard(store: RDFStore, cs: str) -> dict:
    meta = _require_open(store, cs)
    store.update(f"DROP SILENT GRAPH <{_items_graph(cs)}>")
    _set_status(store, cs, "discarded")
    return {**meta, "status": "discarded"}


# --------------------------------------------------------------------------- validation


@lru_cache(maxsize=4)
def _model(ontology_dir: str) -> tuple[Graph, Graph]:
    root = Path(ontology_dir)
    ont = Graph().parse(root / "veromesh-core.ttl").parse(root / "veromesh-org.ttl")
    shapes = Graph().parse(root / "shapes" / "veromesh-org-shapes.ttl")
    return ont, shapes


def _shacl(data: Graph, ontology_dir: str) -> list[tuple[str, object, str]]:
    ont, shapes = _model(ontology_dir)
    _, results, _ = shacl_validate(
        data, shacl_graph=shapes, ont_graph=ont, inference="rdfs", advanced=True, allow_warnings=True
    )
    out = []
    for r in results.subjects(SH.resultSeverity, None):
        out.append(
            (
                str(results.value(r, SH.resultSeverity)).split("#")[-1],
                results.value(r, SH.focusNode),
                str(results.value(r, SH.resultMessage) or "Validation rule failed."),
            )
        )
    return out


def _tidy(message: str, base: str) -> str:
    """Show instance IRIs as their IDs and model terms by local name."""
    if base:
        message = re.sub(
            re.escape(base) + r"([A-Za-z0-9._%-]+)", lambda m: E.local_id(base, base + m.group(1)), message
        )
    return re.sub(r"https://veromesh\.ai/ontology/(?:org|core)#([A-Za-z]+)", r"\1", message)


@dataclass
class _Plan:
    target: Graph
    errors: list[dict]
    report: dict
    effective: Graph | None = None
    item_meta: dict | None = None


def _effective(
    graphs: dict[str, Graph],
    base: str,
    *,
    stored: dict,
    atts: list,
    cats: dict[str, str],
    target_source: str | None = None,
    item_meta: dict | None = None,
):
    """The effective model for a set of source graphs. Claims keep their recorded times
    when their values are unchanged; the changeset's items supply new ones."""
    at = now()
    claims = []
    for iri, g in graphs.items():
        source = iri[len(SOURCE_GRAPH_BASE) :]
        for key, values in P.extract(g, base).items():
            old = stored.get((source, *key))
            m = (item_meta or {}).get(key, {}) if source == target_source else {}
            same = old is not None and P.value_hash(old.values) == P.value_hash(values)
            claims.append(
                P.Claim(
                    source=source,
                    type=key[0],
                    id=key[1],
                    values=values,
                    category=cats.get(source, P.DEFAULT_CATEGORY),
                    observed_at=m.get("observedAt") or (old.observed_at if same else at),
                    valid_from=m.get("validFrom", old.valid_from if same else None),
                    valid_until=m.get("validUntil", old.valid_until if same else None),
                )
            )
    # Validation checks structure, so facts outside their valid period still count here.
    resolved = P.resolve(claims, atts, ignore_validity=True)
    graph, errors = P.to_graph(resolved, base)
    return graph, errors, resolved


def _plan(store: RDFStore, meta: dict, items: list[Item], base: str, ontology_dir: str) -> _Plan:
    graphs = read_sources(store)
    target_iri = source_graph(meta["source"])
    current = graphs.pop(target_iri, Graph())
    others = Graph()
    for g in graphs.values():
        others += g

    target = Graph()
    if meta["mode"] == "merge":
        target += current
    # Remove the old state of every entity this changeset touches (merge mode). Triples
    # with the entity as subject but an inverse field's property belong to the other
    # entity (a role "holds" an authority: that link is the authority's holder field).
    inverse_props = {c.prop for sh in E.TYPES.values() for c in sh.cols if c.inverse}
    for it in items:
        subj = E.iri_for(base, it.id)
        for t in list(target.triples((subj, None, None))):
            if t[1] not in inverse_props:
                target.remove(t)
        for col in E.TYPES[it.type].cols:
            if col.inverse:
                for t in list(target.triples((None, col.prop, subj))):
                    target.remove(t)

    # Which IDs exist, and as what type, once the changeset is applied.
    index: dict[str, str] = {}
    for g in (others, target):
        for t in E.TYPES:
            for s in E.subjects_of_type(g, t):
                index.setdefault(E.local_id(base, s), t)
    errors: list[dict] = []
    upserts = [it for it in items if it.op == "upsert"]
    deletes = [it for it in items if it.op == "delete"]
    if deletes and meta["mode"] == "replace":
        for it in deletes:
            errors.append(
                E.FieldError(
                    it.type,
                    it.id,
                    "id",
                    "Deletes are only allowed in merge mode; in replace mode, leave the entity out.",
                ).as_dict()
            )
    for it in upserts:
        prior = index.get(it.id)
        if prior is not None and prior != it.type:
            errors.append(
                E.FieldError(
                    it.type, it.id, "id", f"ID '{it.id}' is already used by a {prior} entity."
                ).as_dict()
            )
        index[it.id] = it.type

    partial = meta["source"] == P.STEWARD_SOURCE
    for it in upserts:
        conv = E.to_triples(it.type, it.entity, base, index.get, partial=partial)
        errors.extend(e.as_dict() for e in conv.errors)
        for t in conv.triples:
            target.add(t)

    stored = {(c.source, c.type, c.id): c for c in P.read_claims(store)}
    atts = P.read_attestations(store)
    cats = P.categories(store)
    item_meta = {(it.type, it.id): it.meta or {} for it in upserts}
    after, ref_errors, resolved = _effective(
        {**graphs, target_iri: target},
        base,
        stored=stored,
        atts=atts,
        cats=cats,
        target_source=meta["source"],
        item_meta=item_meta,
    )
    # References to instances that no longer exist in the model (e.g. a replace that
    # drops a role other sources still point at). Explicit deletes report below.
    deleted_ids = {it.id for it in deletes}
    still_referenced: dict[str, list[dict]] = {}
    for e in ref_errors:
        m = re.search(r"No entity with ID '([^']*)'", e["message"])
        if m and m.group(1) in deleted_ids:
            still_referenced.setdefault(m.group(1), []).append(e)
            continue
        if m:
            e = {**e, "message": f"References '{m.group(1)}', which no longer exists after this change."}
        errors.append(e)
    for it in deletes if meta["mode"] == "merge" else []:
        subj = E.iri_for(base, it.id)
        if it.type not in E.types_of(current, subj):
            errors.append(
                E.FieldError(
                    it.type, it.id, "id", f"No {it.type} '{it.id}' in source '{meta['source']}' to delete."
                ).as_dict()
            )
            continue
        if it.type in E.types_of(after, subj):
            continue  # still defined by another source
        for e in still_referenced.get(it.id, []):
            errors.append(
                E.FieldError(
                    it.type, it.id, "id", f"Still referenced by {e['type']} '{e['id']}' ({e['field']})."
                ).as_dict()
            )

    report = {
        "changeset": meta["id"],
        "source": meta["source"],
        "mode": meta["mode"],
        "summary": {
            "upserts": len(upserts),
            "deletes": len(deletes),
            "errors": len(errors),
            "violations": 0,
            "warnings": 0,
        },
        "errors": errors,
        "violations": [],
        "warnings": [],
    }
    if errors:
        report["valid"] = False
        return _Plan(target, errors, report)

    before, _, _ = _effective({**graphs, target_iri: current}, base, stored=stored, atts=atts, cats=cats)
    before_findings = {(sev, str(f), m) for sev, f, m in _shacl(before, ontology_dir)}
    touched = {E.iri_for(base, it.id) for it in upserts}

    def describe(sev: str, focus, message: str) -> dict:
        types = E.types_of(after, focus) if isinstance(focus, URIRef) else []
        return {
            "type": types[0] if types else None,
            "id": E.local_id(base, focus),
            "message": _tidy(message, base),
            "severity": sev,
        }

    for sev, focus, message in _shacl(after, ontology_dir):
        if sev == "Violation" and (sev, str(focus), message) not in before_findings:
            report["violations"].append(describe(sev, focus, message))
        elif sev != "Violation" and focus in touched:
            report["warnings"].append(describe(sev, focus, message))
    report["preexistingViolations"] = sum(1 for sev, _, _ in before_findings if sev == "Violation")
    for k in ("violations", "warnings"):
        report[k].sort(key=lambda f: (f["type"] or "", f["id"], f["message"]))
        report["summary"][k] = len(report[k])
    report["valid"] = not report["violations"]
    report["summary"]["conflicts"] = sum(len(r.conflicts) for r in resolved.values())
    return _Plan(target, errors, report, effective=after, item_meta=item_meta)


def validate(store: RDFStore, cs: str, *, base: str, ontology_dir: str) -> dict:
    meta = _require_open(store, cs)
    plan = _plan(store, meta, _load_items(store, cs), base, ontology_dir)
    _set_status(store, cs, "valid" if plan.report["valid"] else "invalid", plan.report)
    return plan.report


_commit_locks: dict[str, threading.Lock] = {}
_commit_locks_guard = threading.Lock()


def _source_lock(source: str) -> threading.Lock:
    with _commit_locks_guard:
        return _commit_locks.setdefault(source, threading.Lock())


def commit(store: RDFStore, cs: str, *, base: str, ontology_dir: str) -> dict:
    meta = get(store, cs)
    if meta["status"] == "committed":
        return {**meta.get("report", {}), "alreadyCommitted": True}
    if meta["status"] == "discarded":
        raise Conflict(f"Changeset {cs} is discarded.")
    # One commit per source at a time within this service; the version check below
    # catches anything that changed the source since the changeset was opened.
    with _source_lock(meta["source"]):
        return _commit_locked(store, cs, meta, base=base, ontology_dir=ontology_dir)


def _commit_locked(store: RDFStore, cs: str, meta: dict, *, base: str, ontology_dir: str) -> dict:
    current = source_version(store, meta["source"])
    if meta.get("baseVersion") and current != meta["baseVersion"]:
        raise Conflict(
            f"Source '{meta['source']}' changed since this changeset was opened "
            f"(now at version {current}, changeset started from {meta['baseVersion']}). "
            "Nothing was written; re-run the load against the current state."
        )
    plan = _plan(store, meta, _load_items(store, cs), base, ontology_dir)
    if not plan.report["valid"]:
        _set_status(store, cs, "invalid", plan.report)
        raise Conflict("Changeset is not valid; nothing was written.", plan.report)
    root = Path(ontology_dir)
    store.load_turtle((root / "veromesh-core.ttl").read_text(), CORE_GRAPH)
    store.load_turtle((root / "veromesh-org.ttl").read_text(), ORG_GRAPH)
    graph_iri = source_graph(meta["source"])
    store.load_turtle(plan.target.serialize(format="turtle"), graph_iri)
    at = now()
    file_triple = f"; dcterms:source {literal(meta['file'])}" if meta.get("file") else ""
    store.update(
        f"""
        PREFIX prov: <http://www.w3.org/ns/prov#>
        PREFIX dcterms: <http://purl.org/dc/terms/>
        DELETE {{ GRAPH <{PROVENANCE_GRAPH}> {{ <{graph_iri}> ?p ?o }} }}
        WHERE  {{ GRAPH <{PROVENANCE_GRAPH}> {{ <{graph_iri}> ?p ?o }} }} ;
        INSERT DATA {{ GRAPH <{PROVENANCE_GRAPH}> {{
          <{graph_iri}> a prov:Entity ;
            dcterms:identifier {literal(meta["source"])} ;
            dcterms:type "{meta["kind"]}" {file_triple} ;
            prov:generatedAtTime "{at}"^^<{XSD.dateTime}> ;
            prov:wasGeneratedBy <{_cs_iri(cs)}> ;
            <{VM.tripleCount}> {len(plan.target)} .
        }} }}
        """
    )
    claims = P.record_source(
        store, meta["source"], P.extract(plan.target, base), plan.item_meta or {}, changeset=cs, at=at
    )
    P.migrate(store, base)  # sources loaded before provenance existed get their claims
    P.refresh_effective(store, base)  # from the recorded claims, so valid times apply
    report = {**plan.report, "committed": True, "facts": len(plan.target), "version": cs, "claims": claims}
    _set_status(store, cs, "committed", report, extra=f'<{VM.committedAt}> "{at}"^^<{XSD.dateTime}>')
    return report


def apply(
    store: RDFStore, *, source: str, items: list[dict], base: str, ontology_dir: str, kind: str = "api"
) -> dict:
    """One-shot merge changeset for single-entity REST writes: commit or raise Conflict."""
    meta = create(store, source=source, mode="merge", kind=kind)
    add_items(store, meta["id"], items)
    try:
        return commit(store, meta["id"], base=base, ontology_dir=ontology_dir)
    except Conflict:
        discard(store, meta["id"])
        raise
