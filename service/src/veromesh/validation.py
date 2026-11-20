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

A focused validation runs pyshacl in a worker process, so decision checks keep
the service's interpreter to themselves while an edit validates.
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


# pyshacl tests each predicate of a shape against a list of ~40 constraint
# parameters, on every (nested) shape evaluation; a list that answers ``in``
# from a set does the same with a hash lookup.
class _Params(list):
    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._set = frozenset(self)

    def __contains__(self, item) -> bool:
        return item in self._set

    def copy(self) -> _Params:
        return _Params(self)

    def append(self, item) -> None:
        super().append(item)
        self._set = frozenset(self)


def _fast_parameters() -> None:
    from pyshacl import shape as _shape
    from pyshacl.constraints import ALL_CONSTRAINT_PARAMETERS, CONSTRAINT_PARAMETERS_MAP

    _shape.module.CONSTRAINT_PARAMS = (_Params(ALL_CONSTRAINT_PARAMETERS), CONSTRAINT_PARAMETERS_MAP)


_fast_parameters()


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

    def types(self, data: Graph, node) -> frozenset:
        """``node``'s classes in ``data`` once entailed (declared, from domains and ranges,
        and their superclasses)."""
        classes = set(data.objects(node, RDF.type))
        for p in set(data.predicates(node, None)):
            classes |= self.domains.get(p, set())
        for p in set(data.predicates(None, node)):
            classes |= self.ranges.get(p, set())
        out = set(classes)
        for c in classes:
            out |= self.super_classes.get(c, set())
        return frozenset(out)

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
    nodes (against the whole graph); None validates everything.

    A focused validation (an edit's) runs pyshacl in a worker process (see
    ``_Worker``): pyshacl is pure Python, and in this process it would take turns with
    every decision check for the interpreter lock while it runs."""
    sch = schema(ontology_dir)
    nodes = None
    if focus is not None:
        focus = {n for n in focus if isinstance(n, URIRef)}
        if not focus:
            return []
        nodes = sorted(str(n) for n in focus)
        data = neighbourhood(data, focus)
    if nodes is not None and _worker.usable(len(data)):
        found = _worker.run(data, ontology_dir, nodes)  # it entails, then validates
        if found is not None:
            return [(sev, _node(f), msg) for sev, f, msg in found]
    return _shacl(sch.entail(data), ontology_dir, nodes)


def _shacl(entailed: Graph, ontology_dir: str, nodes: list[str] | None) -> list[tuple[str, object, str]]:
    _, results, _ = shacl_validate(
        entailed,
        shacl_graph=schema(ontology_dir).shapes,
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


# --------------------------------------------------------------------------- the worker process


def _in_worker(triples: list[list[str]], ontology_dir: str, nodes: list[str]) -> list[tuple[str, str, str]]:
    """In the worker: entail and validate the triples given (terms encoded as
    compactgraph.encode does: much faster to read than N-Triples); focus nodes as N3."""
    from .compactgraph import compact_graph, decode

    g = compact_graph((decode(s), decode(p), decode(o)) for s, p, o in triples)
    found = _shacl(schema(ontology_dir).entail(g), ontology_dir, nodes)
    return [(sev, f.n3() if f is not None else "", msg) for sev, f, msg in found]


def _warm(ontology_dir: str) -> None:
    schema(ontology_dir)  # the shapes and the class hierarchy, loaded once per worker


def _node(n3: str):
    from rdflib import BNode, Literal

    if n3.startswith("<") and n3.endswith(">"):
        return URIRef(n3[1:-1])
    if n3.startswith("_:"):
        return BNode(n3[2:])
    return Literal(n3) if n3 else None


def worker_enabled() -> bool:
    """VEROMESH_VALIDATION_WORKER=off validates in the service's own process."""
    return os.environ.get("VEROMESH_VALIDATION_WORKER", "on").lower() not in ("off", "0", "false", "no")


# A neighbourhood larger than this (entailed triples) is validated in this process:
# sending it would cost more than it saves. An edit's is a few thousand at most.
WORKER_MAX_TRIPLES = 200_000


class _Worker:
    """One process (``python -m veromesh.validation_worker``) that runs pyshacl for
    this one, started on first use and spoken to over a pipe, one JSON line each
    way. If it fails, the validation runs here and a new worker starts next time."""

    def __init__(self) -> None:
        self._proc = None
        self._lock = threading.Lock()

    def usable(self, size: int) -> bool:
        return worker_enabled() and size <= WORKER_MAX_TRIPLES

    def _started(self, ontology_dir: str):
        if self._proc is None or self._proc.poll() is not None:
            import subprocess
            import sys

            # The worker imports this package from where this process did, installed or not.
            here = str(Path(__file__).resolve().parents[1])
            env = {
                **os.environ,
                "PYTHONPATH": os.pathsep.join(filter(None, (here, os.environ.get("PYTHONPATH")))),
            }
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "veromesh.validation_worker", ontology_dir],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        return self._proc

    def start(self, ontology_dir: str) -> None:
        """Start the worker now (at service start-up), so the first edit does not wait for it."""
        if worker_enabled():
            self._ask(ontology_dir, {"warm": True})

    def run(self, data: Graph, ontology_dir: str, nodes: list[str]):
        from .compactgraph import encode

        triples = [[encode(s), encode(p), encode(o)] for s, p, o in data]
        got = self._ask(ontology_dir, {"triples": triples, "nodes": nodes})
        return None if got is None else [tuple(f) for f in got["findings"]]

    def _ask(self, ontology_dir: str, request: dict):
        import json
        import logging

        with self._lock:
            proc = None
            try:
                proc = self._started(ontology_dir)
                proc.stdin.write(json.dumps({**request, "ontology": ontology_dir}) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()  # waits without the interpreter lock
                if not line:
                    raise RuntimeError("the worker exited")
                got = json.loads(line)
                if "error" in got:
                    raise RuntimeError(got["error"])
                return got
            except Exception as exc:  # noqa: BLE001 — validate here; a new worker next time
                logging.getLogger(__name__).warning(
                    "Validation worker failed (%s); validating in process.", exc
                )
                if proc is not None:
                    proc.kill()
                self._proc = None
                return None


_worker = _Worker()


def start_worker(ontology_dir: str) -> None:
    _worker.start(ontology_dir)


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


def changed_nodes(before: Graph, after: Graph, ontology_dir: str) -> set[URIRef]:
    """The nodes whose own validation can change between ``before`` and ``after``
    (see ``changed``), from every triple added or removed."""
    delta = [t for t in before if t not in after] + [t for t in after if t not in before]
    objects = object_candidates(delta, ontology_dir)
    sch = schema(ontology_dir)
    retyped = {o for o in objects if sch.types(before, o) != sch.types(after, o)}
    return changed(delta, retyped, ontology_dir)


def object_candidates(delta: Iterable[tuple], ontology_dir: str) -> set[URIRef]:
    """Objects of changed triples that matter only if their (entailed) types change."""
    backwards = _backwards(ontology_dir)
    return {o for _, p, o in delta if isinstance(o, URIRef) and p != RDF.type and p not in backwards}


def changed(delta: Iterable[tuple], retyped: set[URIRef], ontology_dir: str) -> set[URIRef]:
    """The nodes a changed triple (s, p, o) changes for validation. Every constraint walks
    forward from the node it validates, except the inverse paths (``_INVERSE``) and the
    unique-value joins (``affected`` handles those). So s always changes; o changes when
    a shape follows p backwards to it, or when the triple changes o's entailed types
    (a property's range types its objects): ``retyped``. A value many entities share
    (a sensitivity level, a classification) does not pull them all in."""
    backwards = _backwards(ontology_dir)
    out: set[URIRef] = set(retyped)
    for s, p, o in delta:
        if isinstance(s, URIRef):
            out.add(s)
        if isinstance(o, URIRef) and p != RDF.type and p in backwards:
            out.add(o)
    return out


@lru_cache(maxsize=4)
def _backwards(ontology_dir: str) -> frozenset:
    """Properties a shape follows backwards, and their sub-properties."""
    sch = schema(ontology_dir)
    subs = {p for p, supers in sch.super_properties.items() if supers & set(_INVERSE)}
    return frozenset(set(_INVERSE) | subs)


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
