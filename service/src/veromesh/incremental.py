"""Changesets that touch a few entities, planned and committed in time that
depends on the change, not on the size of the model.

The full path (changesets._plan and _commit_locked) reads every source, resolves
every entity, validates, then rewrites the source graph and the stored effective
model. This module keeps what that work produces in a ``State`` (one per store
version) and, for a merge changeset of upserts, changes only what the changeset
touches:

* the source graph is edited in place; the entities whose triples it touched
  are re-extracted, re-resolved and re-converted;
* the model the shapes see changes by exactly those entities' triples, so the
  validation focus is computed from that difference;
* commit writes the difference (source graph, claims, stored effective model)
  rather than whole graphs, and carries the state forward to the version it
  wrote, so the next change starts warm.

It gives the same answer as the full path or it does not answer: anything it
cannot show to be equivalent (replace mode, deletes, an entity changing type,
claims out of step with the source graphs, references that already point
nowhere) returns None and the caller takes the full path. VEROMESH_INCREMENTAL=off
turns it off. The tests run both paths on the same changes and compare.
"""

from __future__ import annotations

import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from rdflib import RDF, BNode, Graph, URIRef
from rdflib.namespace import XSD

from . import clock, validation
from . import entities as E
from . import provenance as P
from .modelcache import VersionedStore, cached

_LOCK = threading.RLock()
STATS: Counter = Counter()  # "fast" and "full": which path planned each changeset (for tests and the bench)


def enabled() -> bool:
    return os.environ.get("VEROMESH_INCREMENTAL", "on").lower() not in ("off", "0", "false", "no")


Key = tuple[str, str]


# --------------------------------------------------------------------------- the state


@dataclass
class View:
    """A resolved model as triples, with each entity's own triples and the IDs
    its conversion looked up (so a change to an ID reconverts who refers to it)."""

    resolved: dict[Key, P.Resolved]
    base: str
    triples: dict[Key, list] = field(default_factory=dict)
    count: Counter = field(default_factory=Counter)
    graph: Graph = field(default_factory=Graph)
    ids: dict[str, str] = field(default_factory=dict)  # ID -> type: what references resolve against
    looked: dict[Key, set[str]] = field(default_factory=dict)
    referrers: dict[str, set[Key]] = field(default_factory=dict)
    errors: dict[Key, list] = field(default_factory=dict)
    conflicts: int = 0

    @classmethod
    def build(cls, resolved: dict[Key, P.Resolved], base: str) -> View:
        v = cls(resolved=resolved, base=base)
        v.ids = {rid: t for (t, rid) in resolved}
        for key, r in resolved.items():
            v.triples[key] = v._convert(key)
            v.count.update(v.triples[key])
            v.conflicts += len(r.conflicts)
        for t in v.count:
            v.graph.add(t)
        return v

    def error_count(self) -> int:
        return sum(len(e) for e in self.errors.values())

    def _convert(self, key: Key) -> list:
        seen: set[str] = set()

        def look(rid: str):
            seen.add(rid)
            return self.ids.get(rid)

        ts, errs = _convert(self.resolved[key], self.base, look)
        self._unlook(key)
        self.looked[key] = seen
        for rid in seen:
            self.referrers.setdefault(rid, set()).add(key)
        if errs:
            self.errors[key] = errs
        else:
            self.errors.pop(key, None)
        return ts

    def _unlook(self, key: Key) -> None:
        for rid in self.looked.pop(key, ()):
            ks = self.referrers.get(rid)
            if ks is not None:
                ks.discard(key)
                if not ks:
                    del self.referrers[rid]

    def update(self, changes: dict[Key, P.Resolved | None]) -> tuple[list, list]:
        """Set (or, with None, remove) entities; the triples the graph lost and gained.
        Entities referring to an ID that appeared or went are reconverted too."""
        changed_ids: set[str] = set()
        for k, r in changes.items():
            had = k in self.resolved
            if had:
                self.conflicts -= len(self.resolved.pop(k).conflicts)
            if r is not None:
                self.resolved[k] = r
                self.conflicts += len(r.conflicts)
            if had and r is None and self.ids.get(k[1]) == k[0]:
                del self.ids[k[1]]
                changed_ids.add(k[1])
            elif r is not None and self.ids.get(k[1]) != k[0]:
                self.ids[k[1]] = k[0]
                changed_ids.add(k[1])
        todo = {k for k, r in changes.items() if r is not None}
        for rid in changed_ids:
            todo |= {k for k in self.referrers.get(rid, ()) if k in self.resolved}
        touched: set = set()
        for k, r in changes.items():
            if r is None:
                old = self.triples.pop(k, [])
                self._unlook(k)
                self.errors.pop(k, None)
                self.count.subtract(old)
                touched.update(old)
        for k in todo:
            old = self.triples.get(k, [])
            new = self._convert(k)
            self.triples[k] = new
            self.count.subtract(old)
            self.count.update(new)
            touched.update(old)
            touched.update(new)
        lost, gained = [], []
        for t in touched:
            if self.count[t] <= 0:
                del self.count[t]
                if t in self.graph:
                    self.graph.remove(t)
                    lost.append(t)
            elif t not in self.graph:
                self.graph.add(t)
                gained.append(t)
        return lost, gained


def _convert(r: P.Resolved, base: str, index) -> tuple[list, list]:
    conv = E.to_triples(r.type, {"id": r.id, **r.values}, base, index, partial=True)
    return list(conv.triples), conv.errors


@dataclass
class State:
    base: str
    graphs: dict[str, Graph]  # source graph IRI -> its triples (this state's own copies)
    typed: dict[str, dict[str, set[str]]]  # local ID -> source graph IRI -> type names there
    claims: dict[Key, dict[str, P.Claim]]  # entity -> source -> current claim
    atts: dict[Key, list[P.Attestation]]
    cats: dict[str, str]
    model: View  # all claims, whatever their valid period: what validation checks
    today: View  # claims valid today: the stored effective model
    day: date
    stored_effective_ok: bool  # the stored effective model is ``today``'s graph


def _typed(graph: Graph, base: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t in E.TYPES:
        for s in E.subjects_of_type(graph, t):
            out.setdefault(E.local_id(base, s), set()).add(t)
    return out


def _build(store, base: str) -> State | None:
    """The state for the store as it is, or None when the fast path cannot be used."""
    from .changesets import SOURCE_GRAPH_BASE, _read_sources, list_sources

    graphs = _read_sources(store)
    claims_list = P._read_claims(store, f" FILTER NOT EXISTS {{ ?c <{P.PROV.invalidatedAtTime}> ?x }}")
    atts_list = P._read_attestations(store, None)
    cats = P._categories(store)
    claims: dict[Key, dict[str, P.Claim]] = {}
    for c in claims_list:
        if c.source in claims.setdefault(c.key, {}):
            return None  # two current claims from one source: leave it to the full path
        claims[c.key][c.source] = c
    # Claims must say exactly what the source graphs say (otherwise the full path
    # would date the difference "now", which a kept state cannot reproduce).
    stated: set[tuple[str, str, str]] = set()
    for iri, g in graphs.items():
        source = iri[len(SOURCE_GRAPH_BASE) :]
        for key, values in P.extract(g, base).items():
            c = claims.get(key, {}).get(source)
            if c is None or P.value_hash(c.values) != P.value_hash(values):
                return None
            stated.add((source, *key))
    if len(stated) != len(claims_list):
        return None
    with_claims = {c.source for c in claims_list}
    for s in list_sources(store):
        if (
            s["updatedAt"]
            and s["source"] not in with_claims
            and P.extract(graphs.get(s["graph"], Graph()), base)
        ):
            return None  # a source P.migrate would still give claims
    atts: dict[Key, list[P.Attestation]] = {}
    for a in atts_list:
        atts.setdefault((a.type, a.id), []).append(a)
    resolved = P.resolve(claims_list, atts_list, ignore_validity=True)
    if len({rid for (_, rid) in resolved}) != len(resolved):
        return None  # one ID with two types: how references resolve depends on order
    model = View.build(resolved, base)
    if model.error_count():
        return None  # references that already point nowhere: the full path reports them
    day = clock.today()
    today = View.build(P.resolve(claims_list, atts_list, day), base)
    stored = P._effective_graph(store)
    return State(
        base=base,
        graphs=graphs,
        typed={},
        claims=claims,
        atts=atts,
        cats=cats,
        model=model,
        today=today,
        day=day,
        stored_effective_ok=set(stored) == set(today.graph),
    )._index()


def _index_state(self: State) -> State:
    for iri, g in self.graphs.items():
        for rid, types in _typed(g, self.base).items():
            self.typed.setdefault(rid, {})[iri] = types
    return self


State._index = _index_state  # type: ignore[attr-defined]


def state(store, base: str) -> State | None:
    """The state for the store's current version (built if not kept)."""
    if isinstance(store, VersionedStore):
        return cached(store, ("incremental", base), lambda: _build(store, base))
    return _build(store, base)


# --------------------------------------------------------------------------- planning


@dataclass
class Edit:
    """What a planned changeset changes, kept so commit can write it (or undo it)."""

    target_iri: str
    removed: list  # triples removed from the source graph
    added: list  # triples added to it
    keys: list[Key]  # entities whose claim from this source changed
    values: dict[Key, dict]  # their values as the source now states them
    model_removed: list
    model_added: list
    model_new: dict[Key, tuple[P.Resolved, list]]
    item_meta: dict[Key, dict] = field(default_factory=dict)
    applied: bool = True


def _apply_graph(g: Graph, removed, added) -> None:
    for t in removed:
        g.remove(t)
    for t in added:
        g.add(t)


def undo(st: State, edit: Edit) -> None:
    """Put the state back as it was before ``plan`` (a plan that is not committed)."""
    if edit.applied:
        _apply_graph(st.graphs.setdefault(edit.target_iri, Graph()), edit.added, edit.removed)
        _retype(st, edit)
        edit.applied = False


def _retype(st: State, edit: Edit) -> None:
    g = st.graphs.get(edit.target_iri, Graph())
    for n in _nodes(edit.removed + edit.added):
        rid = E.local_id(st.base, n)
        types = set(E.types_of(g, n))
        per = st.typed.setdefault(rid, {})
        if types:
            per[edit.target_iri] = types
        else:
            per.pop(edit.target_iri, None)


def _nodes(triples) -> set[URIRef]:
    out = set()
    for s, _, o in triples:
        if isinstance(s, URIRef):
            out.add(s)
        if isinstance(o, URIRef):
            out.add(o)
    return out


def _first(types: set[str]) -> str | None:
    return next((t for t in E.TYPES if t in types), None)


def plan(st: State, meta: dict, items, base: str, ontology_dir: str):
    """(report, errors, edit) as the full path would give them, or None for "use the
    full path". The state is left changed by the plan: commit it, or ``undo`` it."""
    from .changesets import _tidy, source_graph

    if meta["mode"] != "merge" or any(it.op != "upsert" for it in items) or not items:
        return None
    if st.base != base or st.day != clock.today():
        return None
    source = meta["source"]
    target_iri = source_graph(source)
    g = st.graphs.get(target_iri, Graph())

    # The source graph after the change, exactly as the full path builds it.
    inverse_props = {c.prop for sh in E.TYPES.values() for c in sh.cols if c.inverse}
    remove: set = set()
    for it in items:
        subj = E.iri_for(base, it.id)
        for t in g.triples((subj, None, None)):
            if t[1] not in inverse_props:
                remove.add(t)
        for col in E.TYPES[it.type].cols:
            if col.inverse:
                remove.update(g.triples((None, col.prop, subj)))

    item_ids = {it.id for it in items}

    def known(rid: str) -> str | None:
        per = st.typed.get(rid, {})
        others = set().union(*(ts for iri, ts in per.items() if iri != target_iri))
        if others:
            return _first(others)
        if rid in item_ids:
            return None  # its triples in this source are removed before the index is built
        return _first(per.get(target_iri, set()))

    errors: list[dict] = []
    index: dict[str, str] = {}
    for it in items:
        prior = index[it.id] if it.id in index else known(it.id)
        if prior is not None and prior != it.type:
            errors.append(
                E.FieldError(
                    it.type, it.id, "id", f"ID '{it.id}' is already used by a {prior} entity."
                ).as_dict()
            )
        index[it.id] = it.type

    def lookup(rid: str) -> str | None:
        return index[rid] if rid in index else known(rid)

    partial = source == P.STEWARD_SOURCE
    new: set = set()
    for it in items:
        conv = E.to_triples(it.type, it.entity, base, lookup, partial=partial)
        errors.extend(e.as_dict() for e in conv.errors)
        new.update(conv.triples)
    removed = [t for t in remove if t not in new]
    added = [t for t in new if t not in g]
    if any(isinstance(x, BNode) for t in removed + added for x in t):
        return None  # DELETE DATA cannot name a blank node: the full path rewrites the graph
    nodes = _nodes(removed + added)
    before_types = {n: set(E.types_of(g, n)) for n in nodes}
    _apply_graph(st.graphs.setdefault(target_iri, g), removed, added)
    edit = Edit(target_iri, removed, added, [], {}, [], [], {})
    _retype(st, edit)

    # The entities whose statement by this source changed.
    keys: list[Key] = []
    values: dict[Key, dict] = {}
    for n in sorted(nodes, key=str):
        after = set(E.types_of(g, n))
        if before_types[n] and before_types[n] != after:
            undo(st, edit)
            return None  # an entity changing type in this source: full path
        for t in sorted(after):
            ent = E.to_entity(g, n, t, base)
            rid = ent.pop("id")
            key = (t, rid)
            vals = {k: v for k, v in ent.items() if not P.empty(v)}
            old = st.claims.get(key, {}).get(source)
            if old is not None and P.value_hash(old.values) == P.value_hash(vals) and key[1] not in item_ids:
                continue
            keys.append(key)
            values[key] = vals
    edit.keys, edit.values = keys, values
    edit.item_meta = {(it.type, it.id): it.meta or {} for it in items}

    # Those entities in the model, re-resolved; the claims of other sources stay.
    at = _now()
    item_meta = edit.item_meta
    model = st.model
    new_keys = [k for k in keys if k not in model.resolved]
    index_get = dict(model.ids)
    for k in new_keys:
        if k[1] in index_get:
            undo(st, edit)
            return None  # an ID gaining a second type changes how references resolve
    index_get.update({k[1]: k[0] for k in new_keys})
    ref_errors: list[dict] = []
    for k in sorted(keys):
        cs = [c for s, c in st.claims.get(k, {}).items() if s != source]
        old = st.claims.get(k, {}).get(source)
        m = item_meta.get(k, {})
        same = old is not None and P.value_hash(old.values) == P.value_hash(values[k])
        cs.append(
            P.Claim(
                source=source,
                type=k[0],
                id=k[1],
                values=values[k],
                category=st.cats.get(source, P.DEFAULT_CATEGORY),
                observed_at=m.get("observedAt") or (old.observed_at if same else at),
                valid_from=m.get("validFrom", old.valid_from if same else None),
                valid_until=m.get("validUntil", old.valid_until if same else None),
            )
        )
        r = P.resolve(cs, st.atts.get(k, []), ignore_validity=True)[k]
        ts, errs = _convert(r, base, index_get.get)
        ref_errors.extend(e.as_dict() for e in errs)
        edit.model_new[k] = (r, ts)
    for e in ref_errors:
        mm = re.search(r"No entity with ID '([^']*)'", e["message"])
        if mm:
            e = {**e, "message": f"References '{mm.group(1)}', which no longer exists after this change."}
        errors.append(e)

    upserts = list(items)
    report = {
        "changeset": meta["id"],
        "source": source,
        "mode": meta["mode"],
        "summary": {
            "upserts": len(upserts),
            "deletes": 0,
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
        return report, errors, edit

    # The model's change, triple by triple (a triple two entities state stays until both drop it).
    count = Counter()
    for k, (_, ts) in edit.model_new.items():
        count.subtract(model.triples.get(k, []))
        count.update(ts)
    for t, d in count.items():
        if d < 0 and model.count[t] + d <= 0 and t in model.graph:
            edit.model_removed.append(t)
        elif d > 0 and model.count[t] == 0:
            edit.model_added.append(t)
    changed: set[URIRef] = set()
    for s, p, o in edit.model_removed + edit.model_added:
        if isinstance(s, URIRef):
            changed.add(s)
        if isinstance(o, URIRef) and p != RDF.type:
            changed.add(o)
    touched = {E.iri_for(base, it.id) for it in upserts}
    added_graph = Graph()
    for t in edit.model_added:
        added_graph.add(t)
    focus = None
    if not validation.full_validation():
        focus = validation.affected([model.graph, added_graph], changed | touched)
    before_findings = {
        (sev, str(f), msg) for sev, f, msg in validation.validate(model.graph, ontology_dir, focus)
    }
    _apply_graph(model.graph, edit.model_removed, edit.model_added)
    try:
        after_findings = validation.validate(model.graph, ontology_dir, focus)

        def describe(sev: str, node, message: str) -> dict:
            types = E.types_of(model.graph, node) if isinstance(node, URIRef) else []
            return {
                "type": types[0] if types else None,
                "id": E.local_id(base, node),
                "message": _tidy(message, base),
                "severity": sev,
            }

        for sev, node, message in after_findings:
            if sev == "Violation" and (sev, str(node), message) not in before_findings:
                report["violations"].append(describe(sev, node, message))
            elif sev != "Violation" and node in touched:
                report["warnings"].append(describe(sev, node, message))
    finally:
        _apply_graph(model.graph, edit.model_added, edit.model_removed)
    report["preexistingViolations"] = sum(1 for sev, _, _ in before_findings if sev == "Violation")
    report["validated"] = "all" if focus is None else len(focus)
    for k in ("violations", "warnings"):
        report[k].sort(key=lambda f: (f["type"] or "", f["id"], f["message"]))
        report["summary"][k] = len(report[k])
    report["valid"] = not report["violations"]
    conflicts = model.conflicts + sum(
        len(r.conflicts) - len(model.resolved[k].conflicts if k in model.resolved else {})
        for k, (r, _) in edit.model_new.items()
    )
    report["summary"]["conflicts"] = conflicts
    return report, errors, edit


def _now() -> str:
    from .changesets import now

    return now()


# --------------------------------------------------------------------------- committing


def _nt(triples) -> str:
    return "\n".join(f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in triples)


def _write_delta(store, graph_iri: str, removed, added) -> None:
    removed, added = list(removed), list(added)
    for i in range(0, len(removed), 500):
        store.update(f"DELETE DATA {{ GRAPH <{graph_iri}> {{ {_nt(removed[i : i + 500])} }} }}")
    for i in range(0, len(added), 500):
        store.update(f"INSERT DATA {{ GRAPH <{graph_iri}> {{ {_nt(added[i : i + 500])} }} }}")


def commit(
    store, st: State, meta: dict, cs: str, report: dict, edit: Edit, ontology_dir: str, version: str
) -> dict:
    """Write a planned edit; returns the commit report. The state is carried forward
    to the version written when nobody else wrote meanwhile, and dropped otherwise."""
    from pathlib import Path

    from .changesets import CORE_GRAPH, ORG_GRAPH, PROVENANCE_GRAPH, VM, _cs_iri, _set_status, now
    from .ontology import literal

    base = st.base
    source = meta["source"]
    graph_iri = edit.target_iri
    versioned = isinstance(store, VersionedStore)
    # What readers have cached at this version, to carry forward with this change.
    lists = {t: store.peek_at(("entities", t, base, None), version) for t in E.TYPES} if versioned else {}
    believed = store.peek_at(("current", st.day), version) if versioned else None
    batch = store.batch(expect=version) if versioned else None
    ctx = batch if batch is not None else _Null()
    with ctx as b:
        root = Path(ontology_dir)
        store.load_turtle((root / "veromesh-core.ttl").read_text(), CORE_GRAPH)
        store.load_turtle((root / "veromesh-org.ttl").read_text(), ORG_GRAPH)
        _write_delta(store, graph_iri, edit.removed, edit.added)
        at = now()
        file_triple = f"; dcterms:source {literal(meta['file'])}" if meta.get("file") else ""
        size = len(st.graphs[graph_iri])
        store.update(
            f"""
            PREFIX prov: <http://www.w3.org/ns/prov#>
            PREFIX dcterms: <http://purl.org/dc/terms/>
            DELETE {{ GRAPH <{PROVENANCE_GRAPH}> {{ <{graph_iri}> ?p ?o }} }}
            WHERE  {{ GRAPH <{PROVENANCE_GRAPH}> {{ <{graph_iri}> ?p ?o }} }} ;
            INSERT DATA {{ GRAPH <{PROVENANCE_GRAPH}> {{
              <{graph_iri}> a prov:Entity ;
                dcterms:identifier {literal(source)} ;
                dcterms:type "{meta["kind"]}" {file_triple} ;
                prov:generatedAtTime "{at}"^^<{XSD.dateTime}> ;
                prov:wasGeneratedBy <{_cs_iri(cs)}> ;
                <{VM.tripleCount}> {size} .
            }} }}
            """
        )
        item_meta = edit.item_meta
        changes: dict = {}
        current = {k: st.claims[k][source] for k in edit.keys if source in st.claims.get(k, {})}
        claims = P.record_source(
            store, source, edit.values, item_meta, changeset=cs, at=at, current=current, changes=changes
        )
        # The state's claims, as they now are in the store.
        superseded = set(changes["superseded"])
        for k in edit.keys:
            old = st.claims.get(k, {}).get(source)
            if old is not None and old.iri in superseded:
                del st.claims[k][source]
        for c in changes["claims"]:  # created or refreshed, as stored
            st.claims.setdefault(c.key, {})[source] = c

        # Every entity of this source, for the claim counts the full path reports.
        total = sum(1 for per in st.claims.values() if source in per)
        claims = {**claims, "unchanged": total - claims["added"]}

        # The model validation checks, from the stored claims (as a fresh state would have it).
        st.model.update(
            {
                k: P.resolve(list(st.claims[k].values()), st.atts.get(k, []), ignore_validity=True)[k]
                for k in sorted(edit.keys)
            }
        )

        # The stored effective model: what is believed today.
        lost, gained = st.today.update(
            {
                k: P.resolve(list(st.claims[k].values()), st.atts.get(k, []), st.day).get(k)
                for k in sorted(edit.keys)
            }
        )
        effective_changed: set | None = None  # None: the whole graph was rewritten
        if st.stored_effective_ok:
            _write_delta(store, P.EFFECTIVE_GRAPH, lost, gained)
            effective_changed = _nodes(lost + gained)
        else:
            P.store_effective(store, st.today.graph)
            st.stored_effective_ok = True

        result = {**report, "committed": True, "facts": size, "version": cs, "claims": claims}
        _set_status(store, cs, "committed", result, extra=f'<{VM.committedAt}> "{at}"^^<{XSD.dateTime}>')
    if b is not None and b.clean:
        store.prime(("incremental", base), b.version, st)
        if effective_changed is not None:
            for t, rows in lists.items():
                if rows is not None:
                    store.prime(
                        ("entities", t, base, None),
                        b.version,
                        _patch(rows, st.today.graph, t, base, effective_changed),
                    )
            if believed is not None:  # a changeset writes no attestations: they carry over
                store.prime(("current", st.day), b.version, (dict(st.today.resolved), believed[1]))
    return result


def _patch(rows: list, graph: Graph, type_: str, base: str, nodes: set) -> list:
    """An entity list (changesets.entity_rows) after the triples about ``nodes`` changed."""
    names = {str(n) for n in nodes}
    keep = [r for r in rows if r[0] not in names]
    fresh = [(str(n), E.to_entity(graph, n, type_, base)) for n in nodes if type_ in E.types_of(graph, n)]
    if not fresh:
        return keep
    return sorted(keep + fresh, key=lambda r: r[0])


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
