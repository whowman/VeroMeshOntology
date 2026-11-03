"""RDF store access.

Two interchangeable backends share the ``RDFStore`` protocol:

* ``MemoryStore``  – embedded Oxigraph (pyoxigraph); used for tests and quick local runs.
* ``OxigraphHttpStore`` – an Oxigraph server reached over the standard SPARQL 1.1
  Protocol and Graph Store Protocol. Any SPARQL 1.1 store that implements those
  protocols (e.g. Apache Jena Fuseki) can be swapped in with minor URL changes.

Query results are always returned as SPARQL 1.1 JSON results (``dict``).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
import pyoxigraph as ox

LIST_GRAPHS_QUERY = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g"


class RDFStore(Protocol):
    def load_turtle(self, data: str, graph: str, *, replace: bool = True) -> None: ...

    def query(self, sparql: str) -> dict[str, Any]: ...

    def update(self, sparql: str) -> None: ...

    def list_graphs(self) -> list[str]: ...


def _graph_names(results: dict[str, Any]) -> list[str]:
    return [b["g"]["value"] for b in results.get("results", {}).get("bindings", [])]


class MemoryStore:
    """Embedded Oxigraph store (not persisted)."""

    def __init__(self) -> None:
        self._store = ox.Store()

    def load_turtle(self, data: str, graph: str, *, replace: bool = True) -> None:
        target = ox.NamedNode(graph)
        if replace and target in set(self._store.named_graphs()):
            self._store.clear_graph(target)
        self._store.load(data, format=ox.RdfFormat.TURTLE, to_graph=target)

    def query(self, sparql: str) -> dict[str, Any]:
        result = self._store.query(sparql)
        if isinstance(result, ox.QueryBoolean):
            return {"head": {}, "boolean": bool(result)}
        if isinstance(result, ox.QuerySolutions):
            return json.loads(result.serialize(format=ox.QueryResultsFormat.JSON))
        raise ValueError("Only SELECT and ASK queries are supported")

    def update(self, sparql: str) -> None:
        self._store.update(sparql)

    def list_graphs(self) -> list[str]:
        return _graph_names(self.query(LIST_GRAPHS_QUERY))


class OxigraphHttpStore:
    """Oxigraph server (``oxigraph serve``) over HTTP."""

    def __init__(self, base_url: str, *, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=30.0)

    def load_turtle(self, data: str, graph: str, *, replace: bool = True) -> None:
        method = "PUT" if replace else "POST"
        resp = self._http.request(
            method,
            f"{self._base}/store",
            params={"graph": graph},
            content=data.encode("utf-8"),
            headers={"Content-Type": "text/turtle"},
        )
        resp.raise_for_status()

    def query(self, sparql: str) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/query",
            content=sparql.encode("utf-8"),
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": "application/sparql-results+json",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def update(self, sparql: str) -> None:
        resp = self._http.post(
            f"{self._base}/update",
            content=sparql.encode("utf-8"),
            headers={"Content-Type": "application/sparql-update"},
        )
        resp.raise_for_status()

    def list_graphs(self) -> list[str]:
        return _graph_names(self.query(LIST_GRAPHS_QUERY))


def make_store(spec: str) -> RDFStore:
    if spec == "memory":
        return MemoryStore()
    if spec.startswith(("http://", "https://")):
        return OxigraphHttpStore(spec)
    raise ValueError(f"Unsupported VEROMESH_RDF_STORE value: {spec!r}")
