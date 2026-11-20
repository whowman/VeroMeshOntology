"""Keep what the service reads from the store in memory until the model changes.

Reading the model is the same work on every request: fetch the effective model,
the claims and attestations, turn them into entities. ``VersionedStore`` wraps
the store; every write through it also writes a new version token into the
store. ``cached`` keeps a value with the version it was read at, and asks the
store for the current version (one tiny query) before reusing it. Every write
goes through the wrapper, in this process or in another replica of the service,
so a cached value is never served after the model it came from has changed.

A value is read after the version, so a write that lands in between makes the
cached value newer than its version: the next call sees the newer version and
reads again. It can only ever cost a re-read, never a stale answer.

Two refinements keep a commit from throwing away more than it must:

* ``batch()``: the writes inside change the version once, at the end, by a
  compare-and-swap from the version the batch expected. ``clean`` then says
  whether nobody else wrote in between, so the writer may ``prime`` the cache
  with what it knows the new state to be.
* ``unversioned()``: writes that are not part of the model (changeset
  bookkeeping) do not change the version.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

VERSION_GRAPH = "https://veromesh.ai/graph/version"
_MODEL = "urn:veromesh:model"
_VERSION = "https://veromesh.ai/ontology/core#modelVersion"

T = TypeVar("T")


ONTOLOGY_SUBJECT = "urn:veromesh:ontology"
ONTOLOGY_HASH = "https://veromesh.ai/ontology/core#ontologyHash"
_FLUSH_BYTES = 4_000_000  # a deferred batch sends what it holds once it reaches this


class Batch:
    def __init__(self, expect: str | None, defer: bool) -> None:
        self.expect = expect
        self.defer = defer
        self.ops: list[str] = []  # deferred writes, sent as one request
        self.size = 0
        self.wrote = False  # a write that changes the model (sent or held)
        self.sent = False  # some of it already reached the store
        self.version: str | None = None
        self.clean = False


class VersionedStore:
    """An RDFStore whose writes bump a model version kept in the store itself."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._cache: dict[Any, tuple[str, Any]] = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    def _batch(self) -> Batch | None:
        return getattr(self._local, "batch", None)

    @contextmanager
    def batch(self, expect: str | None = None, *, defer: bool = False) -> Iterator[Batch]:
        """Writes inside change the version once, when the batch ends (this thread only).
        ``expect``: the version the caller's knowledge is from (default: now).

        ``defer``: hold the writes and send them, with the version change, as one update
        request when the batch ends (one transaction on Fuseki; very large batches are
        sent in parts). Reads inside the batch then do not see its writes. If the batch
        fails, what is still held is never sent."""
        if self._batch() is not None:
            raise RuntimeError("batches do not nest")
        b = Batch(expect if expect is not None else self.version(), defer)
        self._local.batch = b
        ok = False
        try:
            yield b
            ok = True
        finally:
            self._local.batch = None
            if not ok:
                b.ops.clear()
                if b.sent:
                    self._bump()
            elif b.wrote:
                new = str(uuid.uuid4())
                b.ops.append(
                    f"""DELETE {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?old }} }}
                        INSERT {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> "{new}" }} }}
                        WHERE {{ OPTIONAL {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?old }} }}
                                 FILTER(COALESCE(STR(?old), "0") = "{b.expect}") }}"""
                )
                self._send(b)
                b.clean = self.version() == new
                if b.clean:
                    b.version = new
                else:
                    self._bump()  # someone else wrote: make sure the version moves anyway
                    b.version = self.version()
            else:
                self._send(b)  # bookkeeping only
                b.version = b.expect
                b.clean = self.version() == b.expect
            with self._lock:
                self._cache.clear()

    def _send(self, b: Batch) -> None:
        if b.ops:
            for request in _combine(b.ops):
                self.inner.update(request)
            b.ops.clear()
            b.size = 0
            b.sent = True

    def _hold(self, b: Batch, op: str) -> None:
        b.ops.append(op)
        b.size += len(op)
        if b.size > _FLUSH_BYTES:
            self._send(b)

    @contextmanager
    def unversioned(self) -> Iterator[None]:
        """Writes inside are not part of the model (bookkeeping): the version stays."""
        prev = getattr(self._local, "unversioned", False)
        self._local.unversioned = True
        try:
            yield
        finally:
            self._local.unversioned = prev

    def prime(self, key: Any, version: str, value: Any) -> None:
        """Cache ``value`` as what ``key`` is at ``version`` (the writer knows it)."""
        with self._lock:
            self._cache[key] = (version, value)

    def peek_at(self, key: Any, version: str) -> Any:
        """The value cached for ``key`` at ``version``, or None."""
        hit = self._cache.get(key)
        return hit[1] if hit is not None and hit[0] == version else None

    def peek(self, key: Any) -> Any:
        """The cached value for ``key`` if it is current, else None (nothing is computed)."""
        hit = self._cache.get(key)
        return hit[1] if hit is not None and hit[0] == self.version() else None

    # Reads pass through.
    def query(self, sparql: str) -> dict[str, Any]:
        return self.inner.query(sparql)

    def list_graphs(self) -> list[str]:
        return [g for g in self.inner.list_graphs() if g != VERSION_GRAPH]

    # Writes bump the version.
    def update(self, sparql: str) -> None:
        b = self._batch()
        if b is not None and b.defer:
            self._hold(b, sparql)
        else:
            self.inner.update(sparql)
        self._wrote()

    def load_turtle(self, data: str, graph: str, *, replace: bool = True) -> None:
        b = self._batch()
        if b is not None and b.defer:
            from rdflib import Graph

            nt = Graph().parse(data=data, format="turtle").serialize(format="nt")
            if replace:
                self._hold(b, f"DROP SILENT GRAPH <{graph}>")
            self._hold(b, f"INSERT DATA {{ GRAPH <{graph}> {{ {nt} }} }}")
        else:
            self.inner.load_turtle(data, graph, replace=replace)
        self._wrote()

    def _wrote(self) -> None:
        if getattr(self._local, "unversioned", False):
            return
        b = self._batch()
        if b is not None:
            b.wrote = True
            return
        self._bump()

    def _bump(self) -> None:
        self.inner.update(
            f"""DELETE WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?v }} }} ;
                INSERT DATA {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> "{uuid.uuid4()}" }} }}"""
        )
        with self._lock:
            self._cache.clear()

    def ontology_hash(self) -> str | None:
        """The hash of the ontology files last loaded through the fast commit, if recorded."""
        rows = self.inner.query(
            f"SELECT ?h WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <{ONTOLOGY_SUBJECT}> <{ONTOLOGY_HASH}> ?h }} }}"
        )["results"]["bindings"]
        return rows[0]["h"]["value"] if rows else None

    def version(self) -> str:
        rows = self.inner.query(
            f"SELECT ?v WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?v }} }}"
        )["results"]["bindings"]
        return rows[0]["v"]["value"] if rows else "0"

    def __getattr__(self, name: str) -> Any:  # other attributes of the store (URLs, etc.)
        return getattr(self.inner, name)


_PREFIX = re.compile(r"^\s*PREFIX\s+([A-Za-z][\w.-]*)?:\s*<([^>]*)>", re.IGNORECASE)


def _combine(ops: list[str]) -> list[str]:
    """``ops`` as few update requests as possible: one, with every PREFIX at the top
    (not every store accepts a PREFIX after ';'), unless two ops disagree on a prefix."""
    prefixes: dict[str, str] = {}
    bodies: list[str] = []
    for op in ops:
        while (m := _PREFIX.match(op)) is not None:
            name, iri = m.group(1) or "", m.group(2)
            if prefixes.setdefault(name, iri) != iri:
                return list(ops)
            op = op[m.end() :]
        bodies.append(op.strip())
    head = "".join(f"PREFIX {n}: <{i}>\n" for n, i in prefixes.items())
    return [head + " ;\n".join(bodies)]


def cached(store: Any, key: Any, compute: Callable[[], T]) -> T:
    """``compute()``, reused until the model changes. Stores without versions are not cached."""
    if not isinstance(store, VersionedStore) or store._batch() is not None:
        return compute()  # inside a batch the store is changing: read it as it is
    version = store.version()
    hit = store._cache.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]
    value = compute()
    with store._lock:
        store._cache[key] = (version, value)
    return value


@contextmanager
def unversioned(store: Any) -> Iterator[None]:
    """``store.unversioned()`` for a VersionedStore; nothing for any other store."""
    if isinstance(store, VersionedStore):
        with store.unversioned():
            yield
    else:
        yield
