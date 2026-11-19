"""Cached reads of the model are reused until the model changes, in this process or another."""

from __future__ import annotations

from veromesh.modelcache import VERSION_GRAPH, VersionedStore, cached
from veromesh.rdf_store import MemoryStore, make_store

G = "https://example.com/g"


def count(store) -> int:
    return len(store.query(f"SELECT * WHERE {{ GRAPH <{G}> {{ ?s ?p ?o }} }}")["results"]["bindings"])


def test_reused_until_a_write():
    store = make_store("memory")
    calls = []

    def read():
        calls.append(1)
        return count(store)

    store.load_turtle("<a:s> <a:p> <a:o> .", G)
    assert cached(store, "n", read) == 1
    assert cached(store, "n", read) == 1
    assert len(calls) == 1
    store.update(f"INSERT DATA {{ GRAPH <{G}> {{ <a:s> <a:p> <a:o2> }} }}")
    assert cached(store, "n", read) == 2
    assert len(calls) == 2


def test_a_write_by_another_replica_is_seen():
    shared = MemoryStore()
    one, two = VersionedStore(shared), VersionedStore(shared)
    one.load_turtle("<a:s> <a:p> <a:o> .", G)
    assert cached(two, "n", lambda: count(two)) == 1
    one.update(f"INSERT DATA {{ GRAPH <{G}> {{ <a:s> <a:p> <a:o2> }} }}")
    assert cached(two, "n", lambda: count(two)) == 2


def test_the_version_graph_is_hidden_and_plain_stores_are_not_cached():
    store = make_store("memory")
    store.load_turtle("<a:s> <a:p> <a:o> .", G)
    assert VERSION_GRAPH not in store.list_graphs()
    plain, calls = MemoryStore(), []
    cached(plain, "n", lambda: calls.append(1))
    cached(plain, "n", lambda: calls.append(1))
    assert len(calls) == 2
