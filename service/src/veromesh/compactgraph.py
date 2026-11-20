"""An in-memory rdflib store that Python's garbage collector does not have to walk.

The service keeps the model in memory: for 100,000 people, millions of triples.
rdflib's own Memory store keeps each triple as rdflib terms in three levels of
nested dicts, so a model that size is tens of millions of objects the collector
tracks, and every full collection visits all of them while every thread waits
(16-22 seconds on the large reference organization, measured by the capacity
runs).

This store keeps each term as a plain string and each triple as strings in
three indexes (by subject, by predicate, by object). Strings are not tracked by
the collector; an index entry with one triple is a string, and only an entry
with several is a set. A model then costs the collector about one object per
subject instead of a dozen per triple. rdflib terms are made again when a
triple is read.

Use it as ``compact_graph()`` (an ordinary rdflib ``Graph``): everything that
reads a graph, pyshacl included, works unchanged. Contexts and quoted graphs are
not supported; the store holds one graph.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.store import Store
from rdflib.term import Node

_SEP = "\x01"  # between the two terms of an index entry (never inside an IRI or a blank node ID)
_LIT = "\x02"  # between the parts of an encoded literal


def encode(t: Node) -> str:
    """A term as a plain string: '<' IRI, '_' blank node ID, or '"' lang \\x02 datatype \\x02 lexical."""
    # f-strings: rdflib terms overload '+', and the result must be a plain str.
    if type(t) is URIRef:
        return f"<{t}"
    if isinstance(t, Literal):
        return f'"{t.language or ""}{_LIT}{t.datatype or ""}{_LIT}{t}'
    if isinstance(t, URIRef):
        return f"<{t}"
    if isinstance(t, BNode):
        return f"_{t}"
    raise TypeError(f"Cannot store {t!r} in a compact graph")


def decode(s: str) -> Node:
    k = s[0]
    if k == "<":
        return URIRef(s[1:])
    if k == "_":
        return BNode(s[1:])
    lang, dt, lex = s[1:].split(_LIT, 2)
    return Literal(lex, lang=lang or None, datatype=URIRef(dt) if dt else None)


def _add(index: dict[str, Any], key: str, entry: str) -> bool:
    """Add ``entry`` under ``key``; False when it was already there."""
    cur = index.get(key)
    if cur is None:
        index[key] = entry
        return True
    if type(cur) is str:
        if cur == entry:
            return False
        index[key] = {cur, entry}
        return True
    if entry in cur:
        return False
    cur.add(entry)
    return True


def _discard(index: dict[str, Any], key: str, entry: str) -> None:
    cur = index.get(key)
    if cur is None:
        return
    if type(cur) is str:
        if cur == entry:
            del index[key]
        return
    cur.discard(entry)
    if len(cur) == 1:
        index[key] = next(iter(cur))
    elif not cur:
        del index[key]


def _entries(index: dict[str, Any], key: str) -> Iterable[str]:
    cur = index.get(key)
    if cur is None:
        return ()
    if type(cur) is str:
        return (cur,)
    return tuple(cur)  # a copy: callers may change the graph while they read


def _has(index: dict[str, Any], key: str, entry: str) -> bool:
    cur = index.get(key)
    if cur is None:
        return False
    return cur == entry if type(cur) is str else entry in cur


def _none() -> Iterator[Any]:
    return iter(())


class CompactMemory(Store):
    """One graph's triples as plain strings, indexed by subject, predicate and object."""

    context_aware = False
    formula_aware = False
    graph_aware = False
    transaction_aware = False

    def __init__(self, configuration: str | None = None, identifier: Any = None) -> None:
        super().__init__(configuration)
        self.identifier = identifier
        self._s: dict[str, Any] = {}  # subject -> "predicate\x01object" (one) or a set of them
        self._p: dict[str, Any] = {}  # predicate -> "subject\x01object"
        self._o: dict[str, Any] = {}  # object -> "subject\x01predicate"
        self._n = 0
        self._namespace: dict[str, URIRef] = {}
        self._prefix: dict[URIRef, str] = {}

    # ------------------------------------------------------------------ writes

    def add(self, triple, context=None, quoted: bool = False) -> None:
        s, p, o = triple
        es, ep, eo = encode(s), encode(p), encode(o)
        if _add(self._s, es, ep + _SEP + eo):
            _add(self._p, ep, es + _SEP + eo)
            _add(self._o, eo, es + _SEP + ep)
            self._n += 1

    def addN(self, quads) -> None:  # noqa: N802
        for s, p, o, _c in quads:
            self.add((s, p, o))

    def remove(self, triple_pattern, context=None) -> None:
        for (s, p, o), _ in list(self.triples(triple_pattern)):
            es, ep, eo = encode(s), encode(p), encode(o)
            if _has(self._s, es, ep + _SEP + eo):
                _discard(self._s, es, ep + _SEP + eo)
                _discard(self._p, ep, es + _SEP + eo)
                _discard(self._o, eo, es + _SEP + ep)
                self._n -= 1

    # ------------------------------------------------------------------ reads

    def triples(self, triple_pattern, context=None):
        s, p, o = triple_pattern
        for t in self._match(s, p, o):
            yield t, _none()

    def _match(self, s, p, o) -> Iterator[tuple]:
        if s is not None:
            es = encode(s)
            if p is not None:
                ep = encode(p)
                if o is not None:
                    if _has(self._s, es, ep + _SEP + encode(o)):
                        yield (s, p, o)
                    return
                head = ep + _SEP
                for e in _entries(self._s, es):
                    if e.startswith(head):
                        yield (s, p, decode(e[len(head) :]))
                return
            if o is not None:
                eo = encode(o)
                for e in _entries(self._o, eo):
                    ps, pp = e.split(_SEP, 1)
                    if ps == es:
                        yield (s, decode(pp), o)
                return
            for e in _entries(self._s, es):
                ep, eo = e.split(_SEP, 1)
                yield (s, decode(ep), decode(eo))
            return
        if o is not None:
            eo = encode(o)
            want = encode(p) if p is not None else None
            for e in _entries(self._o, eo):
                es, ep = e.split(_SEP, 1)
                if want is None or ep == want:
                    yield (decode(es), p if want is not None else decode(ep), o)
            return
        if p is not None:
            for e in _entries(self._p, encode(p)):
                es, eo = e.split(_SEP, 1)
                yield (decode(es), p, decode(eo))
            return
        for es in list(self._s):
            subj = decode(es)
            for e in _entries(self._s, es):
                ep, eo = e.split(_SEP, 1)
                yield (subj, decode(ep), decode(eo))

    def __len__(self, context=None) -> int:
        return self._n

    def contexts(self, triple=None):
        return _none()

    # ------------------------------------------------------------------ namespaces

    def bind(self, prefix: str, namespace: URIRef, override: bool = True) -> None:
        bound_ns = self._namespace.get(prefix)
        bound_prefix = self._prefix.get(namespace)
        if bound_prefix is None and bound_ns is not None:
            bound_prefix = self._prefix.get(bound_ns)
        if override:
            if bound_prefix is not None:
                self._namespace.pop(bound_prefix, None)
            if bound_ns is not None:
                self._prefix.pop(bound_ns, None)
            self._prefix[namespace] = prefix
            self._namespace[prefix] = namespace
        else:
            ns = bound_ns if bound_ns is not None else namespace
            pf = bound_prefix if bound_prefix is not None else prefix
            self._prefix[ns] = pf
            self._namespace[pf] = ns

    def namespace(self, prefix: str) -> URIRef | None:
        return self._namespace.get(prefix)

    def prefix(self, namespace: URIRef) -> str | None:
        return self._prefix.get(namespace)

    def namespaces(self):
        yield from list(self._namespace.items())


def compact_graph(triples: Iterable[tuple] = ()) -> Graph:
    """An rdflib Graph on a CompactMemory store, holding ``triples``."""
    g = Graph(store=CompactMemory())
    for t in triples:
        g.add(t)
    return g
