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


def test_a_deferred_batch_is_one_request_and_nothing_when_it_fails():
    inner = MemoryStore()
    sent = []
    real = inner.update
    inner.update = lambda q: (sent.append(q), real(q))[1]
    store = VersionedStore(inner)
    before = store.version()
    with store.batch(defer=True) as b:
        store.update(f"INSERT DATA {{ GRAPH <{G}> {{ <a:s> <a:p> <a:o> }} }}")
        store.update(
            f"PREFIX x: <a:> INSERT DATA {{ GRAPH <{G}> {{ x:s x:p x:o2 }} }}"
        )  # a PREFIX after ';' is moved to the top
        store.load_turtle("<a:s> <a:p> <a:o3> .", G, replace=False)
        assert count(store) == 0  # held until the batch ends
    assert len(sent) == 1 and count(store) == 3
    assert b.clean and b.version == store.version() != before

    sent.clear()
    try:
        with store.batch(defer=True):
            store.update(f"INSERT DATA {{ GRAPH <{G}> {{ <a:s> <a:p> <a:o4> }} }}")
            raise RuntimeError("the commit fails")
    except RuntimeError:
        pass
    assert sent == [] and count(store) == 3  # nothing written


def test_a_deferred_batch_is_not_clean_when_another_replica_wrote():
    shared = MemoryStore()
    one, two = VersionedStore(shared), VersionedStore(shared)
    start = one.version()
    with one.batch(expect=start, defer=True) as b:
        one.update(f"INSERT DATA {{ GRAPH <{G}> {{ <a:s> <a:p> <a:o> }} }}")
        two.update(f"INSERT DATA {{ GRAPH <{G}> {{ <a:s> <a:p> <a:other> }} }}")  # meanwhile
    assert not b.clean
    assert two.version() == one.version() != start
