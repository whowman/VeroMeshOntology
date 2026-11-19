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
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

VERSION_GRAPH = "https://veromesh.ai/graph/version"
_MODEL = "urn:veromesh:model"
_VERSION = "https://veromesh.ai/ontology/core#modelVersion"

T = TypeVar("T")


class VersionedStore:
    """An RDFStore whose writes bump a model version kept in the store itself."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._cache: dict[Any, tuple[str, Any]] = {}
        self._lock = threading.Lock()

    # Reads pass through.
    def query(self, sparql: str) -> dict[str, Any]:
        return self.inner.query(sparql)

    def list_graphs(self) -> list[str]:
        return [g for g in self.inner.list_graphs() if g != VERSION_GRAPH]

    # Writes bump the version.
    def update(self, sparql: str) -> None:
        self.inner.update(sparql)
        self._bump()

    def load_turtle(self, data: str, graph: str, *, replace: bool = True) -> None:
        self.inner.load_turtle(data, graph, replace=replace)
        self._bump()

    def _bump(self) -> None:
        self.inner.update(
            f"""DELETE WHERE {{ GRAPH <{VERSION_GRAPH}> {{ ?s ?p ?o }} }} ;
                INSERT DATA {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> "{uuid.uuid4()}" }} }}"""
        )
        with self._lock:
            self._cache.clear()

    def version(self) -> str:
        rows = self.inner.query(
            f"SELECT ?v WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <{_MODEL}> <{_VERSION}> ?v }} }}"
        )["results"]["bindings"]
        return rows[0]["v"]["value"] if rows else "0"

    def __getattr__(self, name: str) -> Any:  # other attributes of the store (URLs, etc.)
        return getattr(self.inner, name)


def cached(store: Any, key: Any, compute: Callable[[], T]) -> T:
    """``compute()``, reused until the model changes. Stores without versions are not cached."""
    if not isinstance(store, VersionedStore):
        return compute()
    version = store.version()
    hit = store._cache.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]
    value = compute()
    with store._lock:
        store._cache[key] = (version, value)
    return value
