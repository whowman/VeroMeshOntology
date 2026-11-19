"""SHACL validation of the model, fast enough to run on every change.

Three things make it cheap, without changing what it finds:

* **The RDFS entailments the shapes need are computed directly** (types of
  superclasses, triples of super-properties, types from property domains and
  ranges) instead of pyshacl running a general RDFS reasoner over the whole
  graph on every call.
* **Each SPARQL constraint is parsed once.** pyshacl hands rdflib the same
  constraint text for every focus node; parsing it is most of the cost of
  SPARQL-based constraints, so parsed queries are kept and reused.
* **Only the nodes a change can affect are validated** (``focus``), when the
  caller knows which they are; see ``affected``.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from pyshacl import validate as shacl_validate
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import SH
from rdflib.plugins.sparql import processor as _sparql

VMO = "https://veromesh.ai/ontology/org#"

# --------------------------------------------------------------------------- parsed SPARQL, reused

_prepared: dict[tuple, object] = {}
_prepared_lock = threading.Lock()
_PREPARED_MAX = 2048
_original_query = _sparql.SPARQLProcessor.query


def _cached_query(self, strOrQuery, initBindings=None, initNs=None, base=None, DEBUG=False):  # noqa: N803
    if isinstance(strOrQuery, str):
        key = (strOrQuery, base, tuple(sorted((k, str(v)) for k, v in (initNs or {}).items())))
        q = _prepared.get(key)
        if q is None:
            q = _sparql.prepareQuery(strOrQuery, initNs=initNs or {}, base=base)
            with _prepared_lock:
                if len(_prepared) >= _PREPARED_MAX:
                    _prepared.clear()
                _prepared[key] = q
        strOrQuery = q
    return _original_query(self, strOrQuery, initBindings, initNs, base, DEBUG)


_sparql.SPARQLProcessor.query = _cached_query


# --------------------------------------------------------------------------- the model's schema


class Schema:
    """The ontology and shapes, with the closures RDFS entailment needs."""

    def __init__(self, ontology_dir: str) -> None:
        root = Path(ontology_dir)
        self.ont = Graph().parse(root / "veromesh-core.ttl").parse(root / "veromesh-org.ttl")
        self.shapes = Graph().parse(root / "shapes" / "veromesh-org-shapes.ttl")
        self.super_classes = self._closure(RDFS.subClassOf)
        self.super_properties = self._closure(RDFS.subPropertyOf)
        self.domains = self._by_property(RDFS.domain)
        self.ranges = self._by_property(RDFS.range)
        self.ont_triples = list(self.ont)

    def _closure(self, prop: URIRef) -> dict[URIRef, set[URIRef]]:
        direct: dict[URIRef, set[URIRef]] = {}
        for s, o in self.ont.subject_objects(prop):
            direct.setdefault(s, set()).add(o)
        out: dict[URIRef, set[URIRef]] = {}
        for start in direct:
            seen: set[URIRef] = set()
            todo = deque(direct[start])
            while todo:
                n = todo.popleft()
                if n not in seen:
                    seen.add(n)
                    todo.extend(direct.get(n, ()))
            out[start] = seen
        return out

    def _by_property(self, prop: URIRef) -> dict[URIRef, set[URIRef]]:
        out: dict[URIRef, set[URIRef]] = {}
        for p, c in self.ont.subject_objects(prop):
            out.setdefault(p, set()).add(c)
        # A super-property's domain and range hold for its sub-properties too.
        for p, supers in self.super_properties.items():
            for sp in supers:
                out.setdefault(p, set()).update(out.get(sp, ()))
        return out

    def entail(self, data: Graph) -> Graph:
        """data + the ontology + the RDFS entailments the shapes rely on."""
        g = Graph()
        for t in self.ont_triples:
            g.add(t)
        types: list[tuple] = []
        for s, p, o in data:
            g.add((s, p, o))
            for sp in self.super_properties.get(p, ()):
                g.add((s, sp, o))
            for c in self.domains.get(p, ()):
                types.append((s, c))
            if not isinstance(o, URIRef):
                continue
            if p == RDF.type:
                types.append((s, o))
            for c in self.ranges.get(p, ()):
                types.append((o, c))
        for s, c in types:
            g.add((s, RDF.type, c))
            for sc in self.super_classes.get(c, ()):
                g.add((s, RDF.type, sc))
        return g


@lru_cache(maxsize=4)
def schema(ontology_dir: str) -> Schema:
    return Schema(ontology_dir)


# --------------------------------------------------------------------------- validating


def validate(
    data: Graph, ontology_dir: str, focus: Iterable[URIRef] | None = None
) -> list[tuple[str, object, str]]:
    """Findings as (severity, focus node, message). ``focus``: validate only these
    nodes (against the whole graph); None validates everything."""
    sch = schema(ontology_dir)
    nodes = None
    if focus is not None:
        focus = {n for n in focus if isinstance(n, URIRef)}
        if not focus:
            return []
        nodes = sorted(str(n) for n in focus)
        data = neighbourhood(data, focus)
    _, results, _ = shacl_validate(
        sch.entail(data),
        shacl_graph=sch.shapes,
        inference="none",
        inplace=True,
        advanced=True,
        allow_warnings=True,
        focus_nodes=nodes,
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


# --------------------------------------------------------------------------- what a change can affect


def full_validation() -> bool:
    """VEROMESH_VALIDATION=full validates the whole model on every change (slow; for checking)."""
    return os.environ.get("VEROMESH_VALIDATION", "incremental").lower() == "full"


# Constraints reach at most this many links out from the node they validate (an
# agent's authority's parent's co-approval). Capability nesting is followed to any depth.
REACH = 3
_TRANSITIVE = URIRef(VMO + "parentCapability")
# Values that must be unique across the model: a clash is reported on every holder,
# so the other holders of a changed node's value are affected too.
_UNIQUE = (URIRef(VMO + "signInClientId"), URIRef(VMO + "columnUrn"))


# Properties the shapes follow backwards from the node they validate (sh:inversePath).
_INVERSE = tuple(URIRef(VMO + p) for p in ("realizedBy", "fills", "holds", "requiresCoApproval", "produces"))


def neighbourhood(data: Graph, focus: set[URIRef]) -> Graph:
    """The part of ``data`` that validating ``focus`` can look at: every node a
    constraint can reach from a focus node (up to REACH + 1 links out, any number
    along capability nesting), the nodes that point at a focus node through a
    property the shapes follow backwards, the other holders of unique values, and
    the types of everything reached."""
    nodes: dict[URIRef, int] = {}
    todo: deque = deque()

    def visit(n, d):
        if isinstance(n, URIRef) and (n not in nodes or nodes[n] > d):
            nodes[n] = d
            todo.append(n)

    for n in focus:
        visit(n, 0)
        for p in _INVERSE:
            for s in data.subjects(p, n):
                visit(s, 1)
        for p in _UNIQUE:
            for value in data.objects(n, p):
                for s in data.subjects(p, value):
                    visit(s, 1)
    while todo:
        n = todo.popleft()
        d = nodes[n]
        for p, o in data.predicate_objects(n):
            if p == RDF.type:
                continue
            nd = d if p == _TRANSITIVE else d + 1
            if nd <= REACH + 1:
                visit(o, nd)
    sub = Graph()
    for n in nodes:
        for p, o in data.predicate_objects(n):
            sub.add((n, p, o))
            if isinstance(o, URIRef) and p != RDF.type:
                for t in data.objects(o, RDF.type):
                    sub.add((o, RDF.type, t))
    return sub


def changed_nodes(before: Graph, after: Graph) -> set[URIRef]:
    """Subjects and objects of every triple added or removed."""
    out: set[URIRef] = set()
    for g1, g2 in ((before, after), (after, before)):
        for s, p, o in g1:
            if (s, p, o) not in g2:
                if isinstance(s, URIRef):
                    out.add(s)
                if isinstance(o, URIRef) and p != RDF.type:
                    out.add(o)
    return out


def affected(graphs: Iterable[Graph], changed: set[URIRef]) -> set[URIRef]:
    """Nodes whose validation can change when ``changed`` change: the changed
    nodes, and every node with a path of at most REACH links (any number along
    capability nesting) to one of them, in any of ``graphs`` (before and after)."""
    graphs = list(graphs)
    changed = set(changed)
    for g in graphs:
        for n in list(changed):
            for p in _UNIQUE:
                for value in g.objects(n, p):
                    changed.update(s for s in g.subjects(p, value) if isinstance(s, URIRef))
    out = set(changed)
    frontier = {n: 0 for n in changed}
    todo = deque(changed)
    while todo:
        n = todo.popleft()
        depth = frontier[n]
        for g in graphs:
            for s, p in g.subject_predicates(n):
                if not isinstance(s, URIRef) or p == RDF.type:
                    continue
                d = depth if p == _TRANSITIVE else depth + 1
                if d > REACH:
                    continue
                if s not in frontier or frontier[s] > d:
                    frontier[s] = d
                    out.add(s)
                    todo.append(s)
    return out
