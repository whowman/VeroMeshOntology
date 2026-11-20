"""The compact store answers every triple pattern exactly as rdflib's own store does."""

from __future__ import annotations

import gc
import itertools
import random

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import XSD

from veromesh.compactgraph import compact_graph, decode, encode

TERMS_S = [URIRef(f"https://example.com/s{i}") for i in range(6)] + [BNode("b1"), BNode("b2")]
TERMS_P = [URIRef(f"https://example.com/p{i}") for i in range(4)]
TERMS_O = TERMS_S + [
    Literal("plain"),
    Literal("hello", lang="en"),
    Literal("hello", lang="fr"),
    Literal("7", datatype=XSD.integer),
    Literal("07", datatype=XSD.integer),  # normalized by rdflib to "7"
    Literal("2026-11-20", datatype=XSD.date),
    Literal("a\x01b\x02c\nd"),  # the separators inside a lexical form
    Literal(""),
]


def test_terms_round_trip():
    for t in TERMS_S + TERMS_P + TERMS_O:
        back = decode(encode(t))
        assert back == t and type(back) is type(t)
        if isinstance(t, Literal):
            assert back.language == t.language and back.datatype == t.datatype


def same(a: Graph, b: Graph) -> None:
    for s, p, o in itertools.product([None, *TERMS_S], [None, *TERMS_P], [None, *TERMS_O]):
        assert sorted(a.triples((s, p, o))) == sorted(b.triples((s, p, o))), (s, p, o)
    assert len(a) == len(b)
    assert set(a) == set(b)


def test_matches_rdflib_memory():
    rng = random.Random(11)
    ref, cg = Graph(), compact_graph()
    for step in range(600):
        t = (rng.choice(TERMS_S), rng.choice(TERMS_P), rng.choice(TERMS_O))
        if rng.random() < 0.65:
            ref.add(t)
            cg.add(t)
        elif rng.random() < 0.8:
            ref.remove(t)
            cg.remove(t)
        else:  # a pattern removal
            pat = (rng.choice([None, t[0]]), rng.choice([None, t[1]]), rng.choice([None, t[2]]))
            ref.remove(pat)
            cg.remove(pat)
        if step % 50 == 0:
            same(ref, cg)
    same(ref, cg)
    assert (TERMS_S[0], TERMS_P[0], TERMS_O[0]) in cg or (TERMS_S[0], TERMS_P[0], TERMS_O[0]) not in ref


def test_changing_the_graph_while_reading_it():
    cg = compact_graph((URIRef(f"https://example.com/s{i}"), TERMS_P[0], Literal(i)) for i in range(5))
    for s, p, o in cg.triples((None, TERMS_P[0], None)):
        cg.remove((s, p, o))
    assert len(cg) == 0


def test_the_collector_tracks_far_fewer_objects():
    triples = [
        (URIRef(f"https://example.com/person{i}"), TERMS_P[k], Literal(f"value {i} {k}"))
        for i in range(2000)
        for k in range(4)
    ]
    gc.collect()
    before = len(gc.get_objects())
    ref = Graph()
    for t in triples:
        ref.add(t)
    del t
    gc.collect()
    rdflib_objects = len(gc.get_objects()) - before
    del ref
    gc.collect()
    before = len(gc.get_objects())
    cg = compact_graph(triples)
    del triples
    gc.collect()
    compact_objects = len(gc.get_objects()) - before
    assert len(cg) == 8000
    assert compact_objects * 5 < rdflib_objects, (compact_objects, rdflib_objects)


def test_missing_from_is_the_difference():
    rng = random.Random(5)
    g = compact_graph()
    for _ in range(300):
        g.add((rng.choice(TERMS_S), rng.choice(TERMS_P), rng.choice(TERMS_O)))
    new = {(rng.choice(TERMS_S), rng.choice(TERMS_P), rng.choice(TERMS_O)) for _ in range(150)}
    assert sorted(g.store.missing_from(new)) == sorted(t for t in g if t not in new)
