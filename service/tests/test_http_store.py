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
def test_model_import_against_real_fuseki():
    from rdflib import Graph

    from veromesh import model_import
    from veromesh.config import Settings
    from veromesh.workbook import graph_to_workbook

    settings = Settings()
    store = fuseki_store(os.environ["VEROMESH_TEST_FUSEKI_URL"])
    example = Graph().parse(f"{settings.ontology_dir}/examples/org-procure-to-pay.ttl")
    data = graph_to_workbook(example, "https://example.com/org#")
    report = model_import.import_workbook(
        store,
        data,
        source="p2p",
        filename="p2p.xlsx",
        base=settings.instance_base,
        ontology_dir=settings.ontology_dir,
    )
    assert report["written"] and report["summary"]["violations"] == 0, report
    assert [s["source"] for s in model_import.list_sources(store)] == ["p2p"]
    again = model_import.import_workbook(
        store,
        data,
        source="p2p",
        filename="p2p.xlsx",
        base=settings.instance_base,
        ontology_dir=settings.ontology_dir,
    )
    assert again["written"] and len(model_import.list_sources(store)) == 1
    exported = model_import.export_workbook(store, "p2p", settings.instance_base)
    assert model_import.import_workbook(
        store,
        exported,
        source="p2p-copy",
        filename="x.xlsx",
        base=settings.instance_base,
        ontology_dir=settings.ontology_dir,
        dry_run=True,
    )["summary"] == {"errors": 0, "violations": 0, "warnings": 0}
