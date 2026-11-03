from __future__ import annotations

import httpx

from veromesh.rdf_store import OxigraphHttpStore, make_store


def recording_store():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/query":
            return httpx.Response(
                200,
                json={
                    "head": {"vars": ["g"]},
                    "results": {"bindings": [{"g": {"type": "uri", "value": "x:g"}}]},
                },
            )
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OxigraphHttpStore("http://oxigraph:7878/", client=client), calls


def test_load_turtle_uses_graph_store_protocol():
    store, calls = recording_store()
    store.load_turtle("<a:s> <a:p> <a:o> .", "https://example.com/g")
    store.load_turtle("<a:s> <a:p> <a:o> .", "https://example.com/g", replace=False)

    assert [c.method for c in calls] == ["PUT", "POST"]
    assert calls[0].url.path == "/store"
    assert calls[0].url.params["graph"] == "https://example.com/g"
    assert calls[0].headers["content-type"] == "text/turtle"


def test_query_and_update_use_sparql_protocol():
    store, calls = recording_store()
    assert store.list_graphs() == ["x:g"]
    store.update("INSERT DATA { <a:s> <a:p> <a:o> }")

    assert calls[0].headers["content-type"] == "application/sparql-query"
    assert calls[0].headers["accept"] == "application/sparql-results+json"
    assert calls[1].url.path == "/update"
    assert calls[1].headers["content-type"] == "application/sparql-update"


def test_make_store():
    assert isinstance(make_store("http://oxigraph:7878"), OxigraphHttpStore)
