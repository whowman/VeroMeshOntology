"""RDF store access.

Backends share the ``RDFStore`` protocol:

* ``MemoryStore`` – embedded Oxigraph (pyoxigraph); tests and quick local runs.
* ``SparqlHttpStore`` – any server speaking the SPARQL 1.1 Protocol and Graph
  Store Protocol. ``fuseki_store`` (the production default, Apache Jena Fuseki)
  and ``oxigraph_store`` build one with that server's endpoint layout.

``VEROMESH_RDF_STORE`` selects the backend:

    memory
    fuseki:http://fuseki:3030/veromesh     (base URL of the Fuseki dataset)
    oxigraph:http://oxigraph:7878

``VEROMESH_RDF_STORE_READ_FALLBACK`` (optional) is a second Fuseki dataset URL
for reads only: the read replicas of a replicated install. Reads go to the main
store, which has every write the service has made; only when it is unreachable
or unavailable (for example while the writer Fuseki restarts) are they sent to
the fallback, which may be a moment behind. Writes never fall back.

Query results are always returned as SPARQL 1.1 JSON results (``dict``).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx
import pyoxigraph as ox

log = logging.getLogger(__name__)

# Responses meaning "the server is not available right now", as opposed to "bad request".
UNAVAILABLE = frozenset({502, 503, 504})

LIST_GRAPHS_QUERY = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g"


class StoreRequestError(Exception):
    """The RDF store refused a request (e.g. invalid Turtle or SPARQL)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"RDF store returned {status}: {detail}")
        self.status = status
        self.detail = detail


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


class SparqlHttpStore:
    """A SPARQL 1.1 Protocol + Graph Store Protocol server over HTTP."""

    def __init__(
        self,
        *,
        query_url: str,
        update_url: str,
        graph_store_url: str,
        client: httpx.Client | None = None,
        fallback_query_url: str | None = None,
    ) -> None:
        self.query_url = query_url
        self.update_url = update_url
        self.graph_store_url = graph_store_url
        self.fallback_query_url = fallback_query_url
        self._http = client or httpx.Client(timeout=30.0)

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        if 400 <= resp.status_code < 500:
            raise StoreRequestError(resp.status_code, resp.text.strip()[:2000])
        resp.raise_for_status()

    def load_turtle(self, data: str, graph: str, *, replace: bool = True) -> None:
        resp = self._http.request(
            "PUT" if replace else "POST",
            self.graph_store_url,
            params={"graph": graph},
            content=data.encode("utf-8"),
            headers={"Content-Type": "text/turtle"},
        )
        self._check(resp)

    def query(self, sparql: str) -> dict[str, Any]:
        if not self.fallback_query_url:
            return self._query(self.query_url, sparql)
        try:
            resp = self._post_query(self.query_url, sparql)
        except httpx.TransportError as e:
            log.warning("RDF store unreachable (%s); reading from %s", e, self.fallback_query_url)
            return self._query(self.fallback_query_url, sparql)
        if resp.status_code in UNAVAILABLE:
            log.warning(
                "RDF store unavailable (%s); reading from %s", resp.status_code, self.fallback_query_url
            )
            return self._query(self.fallback_query_url, sparql)
        self._check(resp)
        return resp.json()

    def _post_query(self, url: str, sparql: str) -> httpx.Response:
        return self._http.post(
            url,
            content=sparql.encode("utf-8"),
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": "application/sparql-results+json",
            },
        )

    def _query(self, url: str, sparql: str) -> dict[str, Any]:
        resp = self._post_query(url, sparql)
        self._check(resp)
        return resp.json()

    def update(self, sparql: str) -> None:
        resp = self._http.post(
            self.update_url,
            content=sparql.encode("utf-8"),
            headers={"Content-Type": "application/sparql-update"},
        )
        self._check(resp)

    def list_graphs(self) -> list[str]:
        return _graph_names(self.query(LIST_GRAPHS_QUERY))


def fuseki_store(
    dataset_url: str, *, client: httpx.Client | None = None, read_fallback: str | None = None
) -> SparqlHttpStore:
    """Apache Jena Fuseki dataset, e.g. ``http://fuseki:3030/veromesh``.

    ``read_fallback``: another Fuseki dataset URL (a read replica) for queries
    when this one is unavailable.
    """
    base = dataset_url.rstrip("/")
    return SparqlHttpStore(
        query_url=f"{base}/query",
        update_url=f"{base}/update",
        graph_store_url=f"{base}/data",
        client=client,
        fallback_query_url=f"{read_fallback.rstrip('/')}/query" if read_fallback else None,
    )


def oxigraph_store(base_url: str, *, client: httpx.Client | None = None) -> SparqlHttpStore:
    """Oxigraph server (``oxigraph serve``), e.g. ``http://oxigraph:7878``."""
    base = base_url.rstrip("/")
    return SparqlHttpStore(
        query_url=f"{base}/query",
        update_url=f"{base}/update",
        graph_store_url=f"{base}/store",
        client=client,
    )


def make_store(spec: str, read_fallback: str | None = None) -> RDFStore:
    if read_fallback and not read_fallback.startswith(("http://", "https://")):
        raise ValueError(
            f"Unsupported VEROMESH_RDF_STORE_READ_FALLBACK value {read_fallback!r}; "
            "expected a Fuseki dataset URL (http:// or https://)"
        )
    if spec == "memory":
        return MemoryStore()
    kind, _, url = spec.partition(":")
    if url.startswith(("http://", "https://")):
        if kind == "fuseki":
            return fuseki_store(url, read_fallback=read_fallback or None)
        if kind == "oxigraph":
            return oxigraph_store(url)
    raise ValueError(
        f"Unsupported VEROMESH_RDF_STORE value {spec!r}; "
        "expected 'memory', 'fuseki:<dataset URL>' or 'oxigraph:<server URL>'"
    )
