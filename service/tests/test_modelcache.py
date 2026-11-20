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


def test_concurrent_misses_compute_once():
    import threading
    import time

    store = make_store("memory")
    store.load_turtle("<a:s> <a:p> <a:o> .", G)
    calls, results = [], []

    def compute():
        calls.append(1)
        time.sleep(0.2)  # long enough for every thread to miss
        return count(store)

    threads = [
        threading.Thread(target=lambda: results.append(cached(store, "slow", compute))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and results == [1] * 8


def test_a_failed_computation_does_not_strand_the_waiters():
    import threading
    import time

    store = make_store("memory")
    attempts, results = [], []

    def compute():
        attempts.append(1)
        time.sleep(0.1)
        if len(attempts) == 1:
            raise RuntimeError("the first one fails")
        return "ok"

    def ask():
        try:
            results.append(cached(store, "flaky", compute))
        except RuntimeError:
            results.append("failed")

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("failed") == 1 and results.count("ok") == 3


def test_a_pinned_unit_of_work_reads_the_version_once():
    inner = MemoryStore()
    store = VersionedStore(inner)
    store.load_turtle("<a:s> <a:p> <a:o> .", G)
    asked = []
    real = inner.query
    inner.query = lambda q: (asked.append(q) if "modelVersion" in q else None, real(q))[1]
    with store.pinned():
        for k in range(5):
            cached(store, ("n", k), lambda: count(store))
    assert len(asked) == 1


def test_a_late_computation_does_not_replace_what_an_edit_carried_forward():
    """A read that began before an edit and ends after it must not overwrite the value
    the edit primed for the new version (the next reader would compute it again)."""
    import threading

    store = make_store("memory")
    store.load_turtle("<a:s> <a:p> <a:o> .", G)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "stale"

    t = threading.Thread(target=lambda: cached(store, "k", slow))
    t.start()
    started.wait(5)
    with store.batch() as b:  # an edit, which knows the new value and carries it forward
        store.update(f"INSERT DATA {{ GRAPH <{G}> {{ <a:s> <a:p> <a:o2> }} }}")
        b.primes["k"] = "fresh"
    release.set()
    t.join()
    assert cached(store, "k", lambda: "computed again") == "fresh"
