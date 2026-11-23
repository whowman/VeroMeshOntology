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
import time
from typing import Any, Protocol

import httpx
import pyoxigraph as ox

log = logging.getLogger(__name__)

# Responses meaning "the server is not available right now", as opposed to "bad request".
UNAVAILABLE = frozenset({502, 503, 504})

LIST_GRAPHS_QUERY = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g"

XSD = "http://www.w3.org/2001/XMLSchema#"


def select(store: Any, sparql: str) -> list[dict[str, dict[str, str]]]:
    """A SELECT query's rows, as ``query(...)["results"]["bindings"]`` gives them.

    For a query that can return the whole model: a store that can, decodes the answer
    a row at a time (``select`` on the store) instead of decoding one large JSON
    document, which holds the interpreter lock for seconds on a large model while
    every other thread waits."""
    rows = getattr(store, "select", None)
    if rows is not None:
        return rows(sparql)
    return store.query(sparql)["results"]["bindings"]


# --------------------------------------------------------------------------- SPARQL TSV results

_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", '"': '"', "'": "'", "\\": "\\"}


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c != "\\" or i + 1 == n:
            out.append(c)
            i += 1
            continue
        e = text[i + 1]
        if e == "u":
            out.append(chr(int(text[i + 2 : i + 6], 16)))
            i += 6
        elif e == "U":
            out.append(chr(int(text[i + 2 : i + 10], 16)))
            i += 10
        else:
            out.append(_ESCAPES.get(e, e))
            i += 2
    return "".join(out)


def _tsv_term(field: str) -> dict[str, str] | None:
    """One SPARQL TSV field (a term in Turtle syntax) as a SPARQL JSON binding."""
    if not field:
        return None
    k = field[0]
    if k == "<":
        return {"type": "uri", "value": _unescape(field[1:-1])}
    if field.startswith("_:"):
        return {"type": "bnode", "value": field[2:]}
    if k == '"':
        i = 1
        while True:  # the closing quote: one not escaped
            j = field.index('"', i)
            backslashes = 0
            while field[j - 1 - backslashes] == "\\":
                backslashes += 1
            if backslashes % 2 == 0:
                break
            i = j + 1
        value = _unescape(field[1:j])
        rest = field[j + 1 :]
        if rest.startswith("@"):
            return {"type": "literal", "value": value, "xml:lang": rest[1:]}
        if rest.startswith("^^<"):
            dt = rest[3:-1]
            if dt == XSD + "string":
                return {"type": "literal", "value": value}
            return {"type": "literal", "value": value, "datatype": dt}
        return {"type": "literal", "value": value}
    # Turtle's short forms: numbers and booleans.
    if field in ("true", "false"):
        return {"type": "literal", "value": field, "datatype": XSD + "boolean"}
    if "e" in field or "E" in field:
        return {"type": "literal", "value": field, "datatype": XSD + "double"}
    if "." in field:
        return {"type": "literal", "value": field, "datatype": XSD + "decimal"}
    return {"type": "literal", "value": field, "datatype": XSD + "integer"}


def _lines(chunks):
    """Lines split at "\\n" only (httpx's iter_lines also splits at U+2028 and other
    line breaks, which a literal may hold)."""
    rest = ""
    for chunk in chunks:
        rest += chunk
        *done, rest = rest.split("\n")
        yield from done
    if rest:
        yield rest


def tsv_rows(lines) -> list[dict[str, dict[str, str]]]:
    """SPARQL TSV results (an iterable of lines) as SPARQL JSON bindings."""
    it = iter(lines)
    head = next(it, "")
    names = [v.strip()[1:] for v in head.rstrip("\r\n").split("\t")] if head.strip() else []
    out = []
    for line in it:
        line = line.rstrip("\r\n")
        if not line and len(names) != 1:
            continue
        row = {}
        for name, field in zip(names, line.split("\t"), strict=False):
            term = _tsv_term(field)
            if term is not None:
                row[name] = term
        out.append(row)
    return out


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

    def select(self, sparql: str) -> list[dict[str, dict[str, str]]]:
        """Rows one at a time from the solutions, without a JSON document in between."""
        result = self._store.query(sparql)
        names = [v.value for v in result.variables]
        out = []
        for solution in result:
            row = {}
            for name in names:
                t = solution[name]
                if t is None:
                    continue
                if isinstance(t, ox.NamedNode):
                    row[name] = {"type": "uri", "value": t.value}
                elif isinstance(t, ox.BlankNode):
                    row[name] = {"type": "bnode", "value": t.value}
                elif t.language:
                    row[name] = {"type": "literal", "value": t.value, "xml:lang": t.language}
                elif t.datatype.value != XSD + "string":
                    row[name] = {"type": "literal", "value": t.value, "datatype": t.datatype.value}
                else:
                    row[name] = {"type": "literal", "value": t.value}
            out.append(row)
        return out

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
        self._fallback_since: float | None = None  # when reads started going to the fallback
        self._fallback_reads = 0
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
            self._falling_back(f"unreachable ({e})")
            return self._query(self.fallback_query_url, sparql)
        if resp.status_code in UNAVAILABLE:
            self._falling_back(f"unavailable ({resp.status_code})")
            return self._query(self.fallback_query_url, sparql)
        if self._fallback_since is not None:
            log.warning(
                "RDF store back after %.1f s; %d reads went to %s meanwhile",
                time.monotonic() - self._fallback_since,
                self._fallback_reads,
                self.fallback_query_url,
            )
            self._fallback_since = None
        self._check(resp)
        return resp.json()

    def _falling_back(self, why: str) -> None:
        """Warn once per outage, not on every read (an outage can mean thousands of reads)."""
        self._fallback_reads += 1
        if self._fallback_since is None:
            self._fallback_since = time.monotonic()
            self._fallback_reads = 1
            log.warning("RDF store %s; reading from %s until it is back", why, self.fallback_query_url)

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

    def select(self, sparql: str) -> list[dict[str, dict[str, str]]]:
        """Rows of a SELECT, read as SPARQL TSV a line at a time as they arrive: other
        threads run between lines, where decoding one large JSON document would hold
        the interpreter lock until it is done."""
        url = self.query_url
        for attempt in (1, 2):
            try:
                with self._http.stream(
                    "POST",
                    url,
                    content=sparql.encode("utf-8"),
                    headers={
                        "Content-Type": "application/sparql-query",
                        "Accept": "text/tab-separated-values",
                    },
                ) as resp:
                    if resp.status_code in UNAVAILABLE and self.fallback_query_url and attempt == 1:
                        url = self.fallback_query_url
                        continue
                    if not resp.is_success:
                        resp.read()
                        self._check(resp)
                    return tsv_rows(_lines(resp.iter_text()))
            except httpx.TransportError:
                if attempt == 2 or not self.fallback_query_url:
                    raise
                url = self.fallback_query_url
        raise RuntimeError("unreachable")

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
    """The configured store, wrapped so the model it holds can be cached (modelcache)."""
    from .modelcache import VersionedStore

    return VersionedStore(_make_store(spec, read_fallback))


def _make_store(spec: str, read_fallback: str | None = None) -> RDFStore:
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
