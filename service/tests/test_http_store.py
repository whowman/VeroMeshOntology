from __future__ import annotations

import os

import httpx
import pytest

from veromesh.rdf_store import (
    MemoryStore,
    SparqlHttpStore,
    StoreRequestError,
    fuseki_store,
    make_store,
    oxigraph_store,
)

RESULTS = {"head": {"vars": ["g"]}, "results": {"bindings": [{"g": {"type": "uri", "value": "x:g"}}]}}


def recording(factory, base):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json=RESULTS)
        if request.content.startswith(b"BAD"):
            return httpx.Response(400, text="Parse error: line 1")
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return factory(base, client=client), calls


def test_fuseki_endpoint_layout():
    store, calls = recording(fuseki_store, "http://fuseki:3030/veromesh/")
    store.load_turtle("<a:s> <a:p> <a:o> .", "https://example.com/g")
    store.load_turtle("<a:s> <a:p> <a:o> .", "https://example.com/g", replace=False)
    assert store.list_graphs() == ["x:g"]
    store.update("INSERT DATA { <a:s> <a:p> <a:o> }")

    assert [(c.method, c.url.path) for c in calls] == [
        ("PUT", "/veromesh/data"),
        ("POST", "/veromesh/data"),
        ("POST", "/veromesh/query"),
        ("POST", "/veromesh/update"),
    ]
    assert calls[0].url.params["graph"] == "https://example.com/g"
    assert calls[0].headers["content-type"] == "text/turtle"
    assert calls[2].headers["accept"] == "application/sparql-results+json"
    assert calls[3].headers["content-type"] == "application/sparql-update"


def test_oxigraph_endpoint_layout():
    store, calls = recording(oxigraph_store, "http://oxigraph:7878")
    store.load_turtle("<a:s> <a:p> <a:o> .", "https://example.com/g")
    store.update("INSERT DATA { <a:s> <a:p> <a:o> }")
    assert [c.url.path for c in calls] == ["/store", "/update"]


def test_store_client_errors_raise_store_request_error():
    store, _ = recording(fuseki_store, "http://fuseki:3030/veromesh")
    with pytest.raises(StoreRequestError) as exc:
        store.load_turtle("BAD turtle", "https://example.com/g")
    assert exc.value.status == 400


def test_make_store():
    assert isinstance(make_store("memory"), MemoryStore)
    fuseki = make_store("fuseki:http://fuseki:3030/veromesh")
    assert isinstance(fuseki, SparqlHttpStore) and fuseki.graph_store_url.endswith("/veromesh/data")
    oxigraph = make_store("oxigraph:http://oxigraph:7878")
    assert oxigraph.graph_store_url == "http://oxigraph:7878/store"
    with pytest.raises(ValueError):
        make_store("http://fuseki:3030/veromesh")


@pytest.mark.skipif(not os.environ.get("VEROMESH_TEST_FUSEKI_URL"), reason="no Fuseki server")
def test_against_real_fuseki():
    """Runs when VEROMESH_TEST_FUSEKI_URL points at a Fuseki dataset (CI starts one)."""
    from veromesh.service import VeromeshService

    from .conftest import BINDINGS_GRAPH, ONTOLOGY_DIR, RET, RETAIL_GRAPH, FakeDataHub

    store = fuseki_store(os.environ["VEROMESH_TEST_FUSEKI_URL"])
    store.update("DROP ALL")  # start clean: other suites may have used this dataset
    svc = VeromeshService(store, FakeDataHub(), bindings_graph=BINDINGS_GRAPH)
    svc.load_ontology(RETAIL_GRAPH, (ONTOLOGY_DIR / "examples" / "retail.ttl").read_text())
    urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,crm.public.loyalty members,PROD)"
    svc.bind(urn, RET + "LoyaltyMember")

    assert RETAIL_GRAPH in svc.list_ontologies()
    assert urn in svc.assets_for_class(RET + "Party")
    with pytest.raises(StoreRequestError):
        store.load_turtle("this is not turtle", "https://example.com/bad")
    svc.unbind(urn, RET + "LoyaltyMember")


@pytest.mark.skipif(not os.environ.get("VEROMESH_TEST_FUSEKI_URL"), reason="no Fuseki server")
def test_changesets_against_real_fuseki():
    from fastapi.testclient import TestClient

    from veromesh.api import create_app
    from veromesh.config import Settings
    from veromesh.service import VeromeshService

    from .conftest import BINDINGS_GRAPH, FakeDataHub

    store = fuseki_store(os.environ["VEROMESH_TEST_FUSEKI_URL"])
    store.update("DROP ALL")  # start clean: other suites may have used this dataset
    client = TestClient(
        create_app(VeromeshService(store, FakeDataHub(), bindings_graph=BINDINGS_GRAPH), Settings())
    )
    items = client.get("/api/v1/examples/procure-to-pay").json()["items"]
    for _ in range(2):  # second run replaces the source in place
        cs = client.post("/api/v1/changesets", json={"source": "p2p-fuseki"}).json()["id"]
        client.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
        r = client.post(f"/api/v1/changesets/{cs}/commit")
        assert r.status_code == 200 and r.json()["summary"]["violations"] == 0, r.json()
    assert [s["source"] for s in client.get("/api/v1/sources").json()].count("p2p-fuseki") == 1
    assert len(client.get("/api/v1/kpis", params={"source": "p2p-fuseki"}).json()) == 2
    r = client.put("/api/v1/roles/Treasurer", params={"source": "p2p-fuseki"}, json={"name": "Treasurer"})
    assert r.status_code == 200, r.json()
    assert client.get("/api/v1/changesets/" + cs).json()["status"] == "committed"


# ---------------------------------------------------------------------------- read fallback


def fallback_store(primary):
    """A Fuseki store with a read replica; ``primary`` answers the main store's requests."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append((host, request.url.path))
        if host == "writer":
            return primary(request)
        return httpx.Response(200, json=RESULTS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = fuseki_store(
        "http://writer:3030/veromesh", client=client, read_fallback="http://replicas:3030/veromesh/"
    )
    return store, calls


def test_reads_use_the_writer_while_it_is_up():
    store, calls = fallback_store(lambda r: httpx.Response(200, json=RESULTS))
    assert store.list_graphs() == ["x:g"]
    assert calls == [("writer", "/veromesh/query")]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_reads_fall_back_to_the_replicas_when_the_writer_is_unavailable(status):
    store, calls = fallback_store(lambda r: httpx.Response(status, text="restarting"))
    assert store.list_graphs() == ["x:g"]
    assert calls == [("writer", "/veromesh/query"), ("replicas", "/veromesh/query")]


def test_reads_fall_back_to_the_replicas_when_the_writer_is_unreachable():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    store, calls = fallback_store(refuse)
    assert store.list_graphs() == ["x:g"]
    assert calls[-1] == ("replicas", "/veromesh/query")


def test_bad_queries_do_not_fall_back():
    store, calls = fallback_store(lambda r: httpx.Response(400, text="Parse error"))
    with pytest.raises(StoreRequestError):
        store.query("SELEC nonsense")
    assert calls == [("writer", "/veromesh/query")]


def test_writes_never_fall_back():
    store, calls = fallback_store(lambda r: httpx.Response(503, text="restarting"))
    with pytest.raises(httpx.HTTPStatusError):
        store.update("INSERT DATA { <a:s> <a:p> <a:o> }")
    with pytest.raises(httpx.HTTPStatusError):
        store.load_turtle("<a:s> <a:p> <a:o> .", "https://example.com/g")
    assert {host for host, _ in calls} == {"writer"}


def test_make_store_takes_a_read_fallback():
    store = make_store("fuseki:http://writer:3030/veromesh", "http://replicas:3030/veromesh")
    assert isinstance(store, SparqlHttpStore)
    assert store.fallback_query_url == "http://replicas:3030/veromesh/query"
    assert make_store("fuseki:http://writer:3030/veromesh").fallback_query_url is None
    with pytest.raises(ValueError, match="READ_FALLBACK"):
        make_store("fuseki:http://writer:3030/veromesh", "replicas:3030")
