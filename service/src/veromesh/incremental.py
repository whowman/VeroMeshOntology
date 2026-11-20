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

import bisect
import hashlib
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import XSD

from . import clock, validation
from .compactgraph import compact_graph, decode, encode
from . import entities as E
from . import provenance as P
from .modelcache import ONTOLOGY_HASH, ONTOLOGY_SUBJECT, VERSION_GRAPH, VersionedStore, cached

_LOCK = threading.RLock()
STATS: Counter = Counter()  # "fast" and "full": which path planned each changeset (for tests and the bench)


def enabled() -> bool:
    return os.environ.get("VEROMESH_INCREMENTAL", "on").lower() not in ("off", "0", "false", "no")


Key = tuple[str, str]

# A change to at most this many source triples is written in one request.
SMALL_TRIPLES = 200


# --------------------------------------------------------------------------- the state


@dataclass
class View:
    """A resolved model as triples, with each entity's own triples and the IDs
    its conversion looked up (so a change to an ID reconverts who refers to it)."""

    resolved: dict[Key, P.Resolved]
    base: str
    # Each entity's triples, and how many entities state each triple, as encoded strings
    # (compactgraph.encode): the garbage collector need not walk them.
    triples: dict[Key, tuple] = field(default_factory=dict)
    count: Counter = field(default_factory=Counter)
    graph: Graph = field(default_factory=compact_graph)
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
            v.triples[key] = _pack(v._convert(key))
            v.count.update(v.triples[key])
            v.conflicts += len(r.conflicts)
        for t in v.count:
            v.graph.add(_unpack(t))
        return v

    def triples_of(self, key: Key) -> list:
        """``key``'s triples, as rdflib terms."""
        return [_unpack(t) for t in self.triples.get(key, ())]

    def stated(self, triple) -> int:
        """How many entities state ``triple``."""
        return self.count.get(_pack1(triple), 0)

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

    def preview(self, changes: dict[Key, P.Resolved | None]) -> Preview | None:
        """What ``update(changes)`` would convert, without changing the view: the triples of
        every entity it reconverts (none for one that goes) and their reference errors, in
        key order. None when an ID would gain a second type."""
        gone: set[str] = set()
        came: dict[str, str] = {}
        for k, r in changes.items():
            had = k in self.resolved
            cur = self.ids.get(k[1])
            if r is not None and not had and cur is not None and cur != k[0]:
                return None
            if had and r is None and cur == k[0]:
                gone.add(k[1])
            elif r is not None and cur != k[0]:
                came[k[1]] = k[0]

        def look(rid: str):
            if rid in came:
                return came[rid]
            return None if rid in gone else self.ids.get(rid)

        def present(k: Key) -> bool:
            return changes[k] is not None if k in changes else k in self.resolved

        todo = {k for k, r in changes.items() if r is not None}
        for rid in gone | set(came):
            todo |= {k for k in self.referrers.get(rid, ()) if present(k)}
        out = Preview(triples={k: [] for k, r in changes.items() if r is None}, errors=[])
        for k in sorted(todo):
            r = changes[k] if k in changes else self.resolved[k]
            ts, errs = _convert(r, self.base, look)
            out.triples[k] = ts
            out.errors.extend(e.as_dict() for e in errs)
        return out

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
                old = self.triples.pop(k, ())
                self._unlook(k)
                self.errors.pop(k, None)
                self.count.subtract(old)
                touched.update(old)
        for k in todo:
            old = self.triples.get(k, ())
            new = _pack(self._convert(k))
            self.triples[k] = new
            self.count.subtract(old)
            self.count.update(new)
            touched.update(old)
            touched.update(new)
        lost, gained = [], []
        for e in touched:
            t = _unpack(e)
            if self.count[e] <= 0:
                del self.count[e]
                if t in self.graph:
                    self.graph.remove(t)
                    lost.append(t)
            elif t not in self.graph:
                self.graph.add(t)
                gained.append(t)
        return lost, gained


@dataclass
class Preview:
    triples: dict[Key, list]
    errors: list[dict]


def _share(model: dict[Key, P.Resolved], today: dict) -> dict:
    """``today`` with each entity whose claims are all valid today replaced by the model's
    own (resolving the same claims gives the same entity): one copy kept, not two."""
    for k, r in today.items():
        m = model.get(k)
        if r is not None and m is not None and len(m.claims) == len(r.claims):
            today[k] = m
    return today


def _pack1(t) -> tuple[str, str, str]:
    return (encode(t[0]), encode(t[1]), encode(t[2]))


def _pack(triples) -> tuple:
    return tuple(_pack1(t) for t in triples)


def _unpack(e: tuple[str, str, str]) -> tuple:
    return (decode(e[0]), decode(e[1]), decode(e[2]))


def _convert(r: P.Resolved, base: str, index) -> tuple[list, list]:
    conv = E.to_triples(r.type, {"id": r.id, **r.values}, base, index, partial=True)
    return list(conv.triples), conv.errors


@dataclass
class State:
    base: str
    graphs: dict[str, Graph]  # source graph IRI -> its triples (this state's own copies)
    typed: dict[str, dict[str, tuple[str, ...]]]  # local ID -> source graph IRI -> type names there
    claims: dict[Key, dict[str, P.Claim]]  # entity -> source -> current claim
    atts: dict[Key, list[P.Attestation]]
    cats: dict[str, str]
    model: View  # all claims, whatever their valid period: what validation checks
    today: View  # claims valid today: the stored effective model
    day: date
    stored_effective_ok: bool  # the stored effective model is ``today``'s graph
    ontology: str | None = None  # hash of the ontology files the store holds (fast commits record it)


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
    today = View.build(_share(resolved, P.resolve(claims_list, atts_list, day)), base)
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
        ontology=store.ontology_hash() if isinstance(store, VersionedStore) else None,
    )._index()


def _index_state(self: State) -> State:
    for iri, g in self.graphs.items():
        for rid, types in _typed(g, self.base).items():
            self.typed.setdefault(rid, {})[iri] = tuple(
                sorted(types)
            )  # a tuple of strings: not walked by the GC
    return self


State._index = _index_state  # type: ignore[attr-defined]


def state(store, base: str, version: str | None = None) -> State | None:
    """The state for the store's current version (built if not kept). ``version``: the
    version the caller just read, to reuse a kept state without asking again."""
    if isinstance(store, VersionedStore):
        if version is not None:
            kept = store.peek_at(("incremental", base), version)
            if kept is not None:
                return kept
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
    model_new: dict[Key, list]  # the triples of every entity the change reconverts
    item_meta: dict[Key, dict] = field(default_factory=dict)
    model_changes: dict = field(default_factory=dict)
    applied: bool = True


def _apply_graph(g: Graph, removed, added) -> None:
    for t in removed:
        g.remove(t)
    for t in added:
        g.add(t)


def undo(st: State, edit: Edit) -> None:
    """Put the state back as it was before ``plan`` (a plan that is not committed)."""
    if edit.applied:
        _apply_graph(st.graphs.setdefault(edit.target_iri, compact_graph()), edit.added, edit.removed)
        _retype(st, edit)
        edit.applied = False


def _retype(st: State, edit: Edit) -> None:
    g = st.graphs.get(edit.target_iri)
    if g is None:
        g = compact_graph()
    for n in _nodes(edit.removed + edit.added):
        rid = E.local_id(st.base, n)
        types = set(E.types_of(g, n))
        per = st.typed.setdefault(rid, {})
        if types:
            per[edit.target_iri] = tuple(sorted(types))
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

    mode = meta["mode"]
    upserts = [it for it in items if it.op == "upsert"]
    deletes = [it for it in items if it.op == "delete"]
    if deletes and mode == "replace":
        return None  # the full path reports that deletes need merge mode
    if st.base != base or st.day != clock.today():
        return None
    source = meta["source"]
    target_iri = source_graph(source)
    g = st.graphs.get(target_iri)
    if g is None:
        g = compact_graph()

    # The source graph after the change, exactly as the full path builds it: in
    # replace mode the changeset is the whole source; in merge mode each item's
    # entity is removed first, then the upserts are added.
    inverse_props = {c.prop for sh in E.TYPES.values() for c in sh.cols if c.inverse}
    item_ids = {it.id for it in items}
    if mode == "replace":
        remove: set = set(g)
    else:
        remove = set()
        for it in items:
            subj = E.iri_for(base, it.id)
            for t in g.triples((subj, None, None)):
                if t[1] not in inverse_props:
                    remove.add(t)
            for col in E.TYPES[it.type].cols:
                if col.inverse:
                    remove.update(g.triples((None, col.prop, subj)))
    in_source = {it.id: it.type in E.types_of(g, E.iri_for(base, it.id)) for it in deletes}

    def known(rid: str) -> str | None:
        per = st.typed.get(rid, {})
        others = set().union(*(ts for iri, ts in per.items() if iri != target_iri))
        if others:
            return _first(others)
        if mode == "replace" or rid in item_ids:
            return None  # its triples in this source are gone before the index is built
        return _first(set(per.get(target_iri, ())))

    errors: list[dict] = []
    index: dict[str, str] = {}
    for it in upserts:
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
    for it in upserts:
        conv = E.to_triples(it.type, it.entity, base, lookup, partial=partial)
        errors.extend(e.as_dict() for e in conv.errors)
        new.update(conv.triples)
    removed = [t for t in remove if t not in new]
    added = [t for t in new if t not in g]
    if any(isinstance(x, BNode) for t in removed + added for x in t):
        return None  # DELETE DATA cannot name a blank node: the full path rewrites the graph
    nodes = _nodes(removed + added)
    source_nodes = nodes
    before_types = {n: set(E.types_of(g, n)) for n in nodes}
    _apply_graph(st.graphs.setdefault(target_iri, g), removed, added)
    edit = Edit(target_iri, removed, added, [], {}, [], [], {})
    _retype(st, edit)

    # The entities whose statement by this source changed (None: it no longer states them).
    item_meta = {(it.type, it.id): it.meta or {} for it in upserts}
    keys: list[Key] = []
    values: dict[Key, dict | None] = {}
    for n in sorted(nodes, key=str):
        after = set(E.types_of(g, n))
        if before_types[n] and after and before_types[n] != after:
            undo(st, edit)
            return None  # an entity changing type in this source: full path
        for t in sorted(after):
            ent = E.to_entity(g, n, t, base)
            rid = ent.pop("id")
            key = (t, rid)
            vals = {k: v for k, v in ent.items() if not P.empty(v)}
            old = st.claims.get(key, {}).get(source)
            if old is not None and P.value_hash(old.values) == P.value_hash(vals) and not item_meta.get(key):
                continue  # same claim as before
            keys.append(key)
            values[key] = vals
        for t in sorted(before_types[n] - after):
            key = (t, E.local_id(base, n))
            keys.append(key)
            values[key] = None
    edit.keys, edit.values, edit.item_meta = keys, values, item_meta

    # Those entities in the model, re-resolved (the claims of other sources stay), and
    # every entity that refers to an ID that appears or goes, reconverted.
    at = _now()
    model = st.model
    changes: dict[Key, P.Resolved | None] = {}
    for k in sorted(keys):
        cs = [c for s, c in st.claims.get(k, {}).items() if s != source]
        if values[k] is not None:
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
        changes[k] = P.resolve(cs, st.atts.get(k, []), ignore_validity=True).get(k)
    preview = model.preview(changes)
    if preview is None:
        undo(st, edit)
        return None  # an ID gaining a second type changes how references resolve
    edit.model_new = preview.triples
    edit.model_changes = changes

    deleted_ids = {it.id for it in deletes}
    still_referenced: dict[str, list[dict]] = {}
    for e in preview.errors:
        mm = re.search(r"No entity with ID '([^']*)'", e["message"])
        if mm and mm.group(1) in deleted_ids:
            still_referenced.setdefault(mm.group(1), []).append(e)
            continue
        if mm:
            e = {**e, "message": f"References '{mm.group(1)}', which no longer exists after this change."}
        errors.append(e)
    for it in deletes:
        if not in_source[it.id]:
            errors.append(
                E.FieldError(
                    it.type, it.id, "id", f"No {it.type} '{it.id}' in source '{source}' to delete."
                ).as_dict()
            )
            continue
        key = (it.type, it.id)
        if (changes[key] is not None) if key in changes else key in model.resolved:
            continue  # still defined by another source
        for e in still_referenced.get(it.id, []):
            errors.append(
                E.FieldError(
                    it.type, it.id, "id", f"Still referenced by {e['type']} '{e['id']}' ({e['field']})."
                ).as_dict()
            )

    report = {
        "changeset": meta["id"],
        "source": source,
        "mode": mode,
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
        return report, errors, edit

    # The model's change, triple by triple (a triple two entities state stays until both drop it).
    count = Counter()
    for k, ts in edit.model_new.items():
        count.subtract(model.triples_of(k))
        count.update(ts)
    for t, d in count.items():
        if d < 0 and model.stated(t) + d <= 0 and t in model.graph:
            edit.model_removed.append(t)
        elif d > 0 and model.stated(t) == 0:
            edit.model_added.append(t)
    delta = edit.model_removed + edit.model_added
    objects = validation.object_candidates(delta, ontology_dir)
    sch = validation.schema(ontology_dir)
    types_before = {o: sch.types(model.graph, o) for o in objects}
    _apply_graph(model.graph, edit.model_removed, edit.model_added)
    retyped = {o for o in objects if sch.types(model.graph, o) != types_before[o]}
    _apply_graph(model.graph, edit.model_added, edit.model_removed)
    changed = validation.changed(delta, retyped, ontology_dir)
    touched = {E.iri_for(base, it.id) for it in upserts if it.meta or E.iri_for(base, it.id) in source_nodes}
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
        (len(r.conflicts) if r is not None else 0)
        - (len(model.resolved[k].conflicts) if k in model.resolved else 0)
        for k, r in changes.items()
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
    readers = store.entries_at(version) if versioned else {}  # shared lists, indexes, current-index
    # A small change goes to the store as one update request when the batch ends (one
    # transaction); a large one is sent as it goes, where bulk uploads are faster than
    # one big SPARQL text. Either way the version changes once.
    small = len(edit.removed) + len(edit.added) <= SMALL_TRIPLES
    batch = store.batch(expect=version, defer=small) if versioned else None
    ctx = batch if batch is not None else _Null()
    with ctx as b:
        root = Path(ontology_dir)
        core = (root / "veromesh-core.ttl").read_text()
        org = (root / "veromesh-org.ttl").read_text()
        digest = hashlib.sha256((core + "\n" + org).encode()).hexdigest()
        if not versioned or st.ontology != digest:  # unchanged ontology files: nothing to send
            store.load_turtle(core, CORE_GRAPH)
            store.load_turtle(org, ORG_GRAPH)
            if versioned:
                store.update(
                    f"""DELETE WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <{ONTOLOGY_SUBJECT}> <{ONTOLOGY_HASH}> ?h }} }} ;
                        INSERT DATA {{ GRAPH <{VERSION_GRAPH}> {{ <{ONTOLOGY_SUBJECT}> <{ONTOLOGY_HASH}> "{digest}" }} }}"""
                )
            st.ontology = digest
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
        stated = {k: v for k, v in edit.values.items() if v is not None}
        claims = P.record_source(
            store,
            source,
            stated,
            item_meta,
            changeset=cs,
            at=at,
            current=current,
            changes=changes,
            read_back=not (versioned and small),  # deferred: read back once written, below
            cats=st.cats,
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
                k: P.resolve(
                    list(st.claims.get(k, {}).values()), st.atts.get(k, []), ignore_validity=True
                ).get(k)
                for k in sorted(edit.keys)
            }
        )

        # The stored effective model: what is believed today.
        lost, gained = st.today.update(
            _share(
                st.model.resolved,
                {
                    k: P.resolve(list(st.claims.get(k, {}).values()), st.atts.get(k, []), st.day).get(k)
                    for k in sorted(edit.keys)
                },
            )
        )
        effective_changed: set | None = None  # None: the whole graph was rewritten
        if st.stored_effective_ok:
            _write_delta(store, P.EFFECTIVE_GRAPH, lost, gained)
            effective_changed = _nodes(lost + gained)
        else:
            P.store_effective(store, st.today.graph)
            st.stored_effective_ok = True

        # What readers will find at the new version, carried forward rather than rebuilt:
        # installed just before the version changes, so no check finds the cache empty.
        if b is not None and effective_changed is not None:
            for t, rows in lists.items():
                if rows is not None:
                    new_rows, gone, came = _patch(rows, st.today.graph, t, base, effective_changed)
                    b.primes[("entities", t, base, None)] = new_rows
                    b.primes.update(_shared_primes(readers, t, base, new_rows, gone, came))
            if believed is not None:  # a changeset writes no attestations: they carry over
                now = dict(st.today.resolved)
                b.primes[("current", st.day)] = (now, believed[1])
                before = readers.get(("current-index", st.day))
                if before is not None:
                    people = [k for k in edit.keys if k[0] == "people"]
                    b.primes[("current-index", st.day)] = (
                        now,
                        before[1],
                        _refill(before[2], believed[0], now, people),
                    )

        result = {**report, "committed": True, "facts": size, "version": cs, "claims": claims}
        _set_status(store, cs, "committed", result, extra=f'<{VM.committedAt}> "{at}"^^<{XSD.dateTime}>')
    if versioned and small:
        # The claims as the store gives them back (it may normalise times), now written.
        for c in P.read_written(store, changes, st.cats):
            st.claims.setdefault(c.key, {})[source] = c
    if b is not None and b.clean:
        store.prime(("incremental", base), b.version, st)
    return result


def _shared_primes(readers: dict, type_: str, base: str, rows, gone, came) -> dict:
    """A type's shared list, its by-ID lookup and its field indexes carried forward,
    patched for the entities that went (``gone``) and came (``came``)."""
    out: dict = {}
    shared = readers.get(("shared-entities", type_, base))
    if shared is not None:
        by_id = dict(shared[1])
        for e in gone:
            by_id.pop(e.get("id"), None)
        for e in came:
            if "id" in e:
                by_id[e["id"]] = e
        out[("shared-entities", type_, base)] = ([e for _, e in rows], by_id)
    for key, index in readers.items():
        if not (isinstance(key, tuple) and key[:3] == ("shared-index", type_, base)):
            continue
        field = key[3]
        new = dict(index)
        for e in gone:
            for v in _values(e, field):
                bucket = [x for x in new.get(v, []) if x.get("id") != e.get("id")]
                if bucket:
                    new[v] = bucket
                else:
                    new.pop(v, None)
        for e in came:
            for v in _values(e, field):
                bucket = list(new.get(v, []))
                keys = [str(E.iri_for(base, x["id"])) for x in bucket]
                bucket.insert(bisect.bisect(keys, str(E.iri_for(base, e["id"]))), e)
                new[v] = bucket
        out[key] = new
    return out


def _values(e: dict, field: str) -> list:
    v = e.get(field)
    return [x for x in (v if isinstance(v, list) else [v]) if x is not None and x != ""]


def _refill(fillers: dict, before: dict, now: dict, people: list) -> dict:
    """The people filling each role, after ``people`` changed (each bucket in ID order)."""
    out = dict(fillers)
    for k in people:
        old, new = before.get(k), now.get(k)
        if old is not None:
            for role in old.values.get("rolesFilled", []) or []:
                bucket = [r for r in out.get(role, []) if r.id != old.id]
                if bucket:
                    out[role] = bucket
                else:
                    out.pop(role, None)
        if new is not None:
            for role in new.values.get("rolesFilled", []) or []:
                bucket = list(out.get(role, []))
                ids = [r.id for r in bucket]
                bucket.insert(bisect.bisect(ids, new.id), new)
                out[role] = bucket
    return out


def _patch(rows: list, graph: Graph, type_: str, base: str, nodes: set) -> tuple[list, list, list]:
    """An entity list (changesets.entity_rows) after the triples about ``nodes`` changed,
    with the entities that left it and the ones that came in."""
    names = {str(n) for n in nodes}
    keep, gone = [], []
    for r in rows:
        (gone if r[0] in names else keep).append(r if r[0] not in names else r[1])
    fresh = [(str(n), E.to_entity(graph, n, type_, base)) for n in nodes if type_ in E.types_of(graph, n)]
    came = [e for _, e in fresh]
    if not fresh:
        return keep, gone, came
    return sorted(keep + fresh, key=lambda r: r[0]), gone, came


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
