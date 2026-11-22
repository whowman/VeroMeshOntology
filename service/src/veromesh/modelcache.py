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

import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

VERSION_GRAPH = "https://veromesh.ai/graph/version"
# What each small commit changed, keyed by the version it wrote, so another copy of the
# service can follow it without reading the whole model again (incremental.follow).
FOLLOW_GRAPH = "https://veromesh.ai/graph/follow"
_MODEL = "urn:veromesh:model"
_VERSION = "https://veromesh.ai/ontology/core#modelVersion"

T = TypeVar("T")


ONTOLOGY_SUBJECT = "urn:veromesh:ontology"
ONTOLOGY_HASH = "https://veromesh.ai/ontology/core#ontologyHash"
_FLUSH_BYTES = 4_000_000  # a deferred batch sends what it holds once it reaches this


def version_max_age() -> float:
    """How old (seconds) a version read from the store may be when a reader uses it
    again instead of asking the store (VEROMESH_VERSION_MAX_AGE, default 0.25). This
    process's own writes are seen at once; another replica's within this time."""
    return float(os.environ.get("VEROMESH_VERSION_MAX_AGE", "0.25"))


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
        # Values the writer knows the new version will have (set inside the batch);
        # installed before the version changes, so no reader finds the cache empty.
        self.primes: dict[Any, Any] = {}
        # Deferred batches only: more writes made from the new version's ID, sent in the
        # same request as the version change (incremental.commit records what it changed).
        self.follow: Callable[[str], list[str]] | None = None


class VersionedStore:
    """An RDFStore whose writes bump a model version kept in the store itself."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._cache: dict[Any, tuple[str, Any]] = {}
        self._newest: str | None = None  # the version this process last read or wrote
        self._seen: tuple[str, float] | None = None  # (version, when read): reused by readers
        self.max_age = version_max_age()
        self._writes = 0  # this process's version changes, so a read begun before one is not kept
        # Entity lists (by type) that readers have built: a commit makes them for the new
        # version even when none is cached at the old one, so no reader has to.
        self.wanted_lists: set[str] = set()
        # The values cached at the version before this process's last write, for readers
        # that began before it (a check pins its version): they are still right for it.
        self._previous: tuple[str, dict[Any, Any]] | None = None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._flights: dict[Any, _Flight] = {}
        self._pending: dict[Any, tuple[str, Any]] = {}  # a batch's primes, until its version lands
        # The last version this process followed, wrote or tried to follow (incremental.follow):
        # a reader missing a value at another version first lets the planner catch up.
        self.followed: str | None = None

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
        b = Batch(expect if expect is not None else self._read_version(), defer)
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
                with self._lock:  # readers at the new version find these the moment it lands
                    self._pending = {k: (new, v) for k, v in b.primes.items()}
                b.ops.append(
                    f"""DELETE {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?old }} }}
                        INSERT {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> "{new}" }} }}
                        WHERE {{ OPTIONAL {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?old }} }}
                                 FILTER(COALESCE(STR(?old), "0") = "{b.expect}") }}"""
                )
                if b.follow is not None and b.defer and not b.sent:
                    b.ops.extend(b.follow(new))
                self._send(b)
                b.clean = self._read_version() == new
                if b.clean:
                    b.version = new
                else:
                    self._bump()  # someone else wrote: make sure the version moves anyway
                    b.version = self._read_version()
                    b.primes = {}
            else:
                self._send(b)  # bookkeeping only
                b.version = b.expect
                b.clean = self._read_version() == b.expect
            with self._lock:  # keep only what is known to hold at the new version
                pending, self._pending = self._pending, {}
                if b.clean and b.version != b.expect:
                    self._previous = (
                        b.expect,
                        {
                            k: v
                            for k, (ver, v) in self._cache.items()
                            # not the planner's state: the commit has changed it in place
                            if ver == b.expect and not (isinstance(k, tuple) and k[:1] == ("incremental",))
                        },
                    )
                self._newest = b.version
                self.followed = b.version  # this process's own write: nothing to follow
                self._writes += 1
                self._seen = (b.version, time.monotonic())  # this process's write: seen at once
                self._cache.clear()
                if b.clean:
                    self._cache.update({k: v for k, v in pending.items() if v[0] == b.version})

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

    def entries_at(self, version: str) -> dict[Any, Any]:
        """Every value cached at ``version``, by key (a snapshot)."""
        with self._lock:
            return {k: v for k, (ver, v) in self._cache.items() if ver == version}

    def held(self, key: Any) -> tuple[str, Any] | None:
        """(version, value) cached for ``key``, at whatever version it was cached."""
        with self._lock:
            return self._cache.get(key)

    def peek(self, key: Any) -> Any:
        """The cached value for ``key`` if it is current, else None (nothing is computed)."""
        hit = self._cache.get(key)
        return hit[1] if hit is not None and hit[0] == self.version() else None

    # Reads pass through.
    def query(self, sparql: str) -> dict[str, Any]:
        return self.inner.query(sparql)

    def list_graphs(self) -> list[str]:
        return [g for g in self.inner.list_graphs() if g not in (VERSION_GRAPH, FOLLOW_GRAPH)]

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
            self._writes += 1
            self._seen = None  # the next read asks the store
            self._cache.clear()

    def ontology_hash(self) -> str | None:
        """The hash of the ontology files last loaded through the fast commit, if recorded."""
        rows = self.inner.query(
            f"SELECT ?h WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <{ONTOLOGY_SUBJECT}> <{ONTOLOGY_HASH}> ?h }} }}"
        )["results"]["bindings"]
        return rows[0]["h"]["value"] if rows else None

    @contextmanager
    def pinned(self) -> Iterator[str]:
        """Read the version once for a unit of read-only work (one decision check): every
        ``version()`` inside, on this thread, returns it instead of asking the store."""
        prev = getattr(self._local, "pinned", None)
        v = prev if prev is not None else self.version()
        self._local.pinned = v
        try:
            yield v
        finally:
            self._local.pinned = prev

    def version(self) -> str:
        """The model's version, for reading: the version this unit of work pinned, or one
        read from the store at most ``max_age`` seconds ago (a decision check need not
        ask the store every time), or a fresh one."""
        pinned = getattr(self._local, "pinned", None)
        if pinned is not None:
            return pinned
        seen = self._seen
        if seen is not None and time.monotonic() - seen[1] < self.max_age:
            return seen[0]
        return self._read_version()

    def _read_version(self) -> str:
        """The version as the store has it now (writes compare against this)."""
        t, writes = time.monotonic(), self._writes
        rows = self.inner.query(
            f"SELECT ?v WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?v }} }}"
        )["results"]["bindings"]
        v = rows[0]["v"]["value"] if rows else "0"
        with self._lock:
            # No write of ours landed meanwhile, and no read asked later has answered first
            # (two reads racing: the one asked later is the newer, whatever order they end in).
            if self._writes == writes and (self._seen is None or t >= self._seen[1]):
                self._newest = v
                self._seen = (v, t)  # as of when it was asked for
        return v

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


class _Flight:
    """One computation of a cached value that other threads are waiting for."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.failed = False


# Called on a miss at a version this process has not followed yet (incremental sets it):
# it brings the planner's state and the readers' values forward from the records other
# copies wrote, so the reader finds its value carried forward instead of rebuilding it.
_FOLLOWER: Callable[[Any, str], bool] | None = None


def set_follower(fn: Callable[[Any, str], bool] | None) -> None:
    global _FOLLOWER
    _FOLLOWER = fn


def cached(store: Any, key: Any, compute: Callable[[], T]) -> T:
    """``compute()``, reused until the model changes. Stores without versions are not cached.

    When several threads miss the same value at the same version (right after an edit,
    under load), one computes it and the others wait for that result instead of each
    computing it again."""
    if not isinstance(store, VersionedStore) or store._batch() is not None:
        return compute()  # inside a batch the store is changing: read it as it is
    version = store.version()
    hit = store._cache.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]
    hit = store._pending.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]
    prev = store._previous
    if prev is not None and prev[0] == version and key in prev[1]:
        return prev[1][key]  # a reader that began before this process's last write
    follower = _FOLLOWER
    if (
        follower is not None
        and store.followed != version
        and not (isinstance(key, tuple) and key[:1] in (("incremental",), ("no-follow",)))
        and follower(store, version)
    ):
        hit = store._cache.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]
    with store._lock:
        flight = store._flights.get((key, version))
        leader = flight is None
        if leader:
            flight = store._flights[(key, version)] = _Flight()
    if not leader:
        flight.done.wait()
        return compute() if flight.failed else flight.value
    try:
        value = compute()
        flight.value = value
        with store._lock:
            # A computation that began before an edit can end after it: it must not
            # replace what the edit carried forward to the new version.
            held = store._cache.get(key)
            if held is None or held[0] == version or held[0] != store._newest:
                store._cache[key] = (version, value)
        return value
    except BaseException:
        flight.failed = True
        raise
    finally:
        flight.done.set()
        with store._lock:
            store._flights.pop((key, version), None)


@contextmanager
def unversioned(store: Any) -> Iterator[None]:
    """``store.unversioned()`` for a VersionedStore; nothing for any other store."""
    if isinstance(store, VersionedStore):
        with store.unversioned():
            yield
    else:
        yield
