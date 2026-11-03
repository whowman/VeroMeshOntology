from __future__ import annotations

from fastapi.testclient import TestClient

from veromesh.api import create_app

from .conftest import RET, RETAIL_GRAPH

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,sales.public.orders,PROD)"


def client_for(service) -> TestClient:
    return TestClient(create_app(service))


def test_health(service):
    assert client_for(service).get("/health").json()["status"] == "ok"


def test_load_and_list(service):
    c = client_for(service)
    ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . "
        '<https://example.com/hr#Employee> a owl:Class ; rdfs:label "Employee" .'
    )
    r = c.put(
        "/ontologies",
        params={"graph": "https://example.com/hr"},
        content=ttl,
        headers={"Content-Type": "text/turtle"},
    )
    assert r.status_code == 204
    assert "https://example.com/hr" in c.get("/ontologies").json()
    classes = c.get("/classes", params={"graph": "https://example.com/hr"}).json()
    assert classes == [
        {
            "iri": "https://example.com/hr#Employee",
            "label": "Employee",
            "definition": None,
            "parents": [],
            "graph": "https://example.com/hr",
        }
    ]


def test_bad_turtle_is_400(service):
    r = client_for(service).put(
        "/ontologies", params={"graph": "https://example.com/bad"}, content="this is not turtle"
    )
    assert r.status_code == 400


def test_bind_flow(service):
    c = client_for(service)
    r = c.post("/bindings", json={"datahub_urn": ORDERS, "class_iri": RET + "Order"})
    assert r.status_code == 201
    assert r.json()["ontologyIri"] == RETAIL_GRAPH
    assert c.get("/bindings", params={"datahub_urn": ORDERS}).json() == [RET + "Order"]
    assert c.get("/classes/assets", params={"class_iri": RET + "Order"}).json() == [ORDERS]

    r = c.request("DELETE", "/bindings", json={"datahub_urn": ORDERS, "class_iri": RET + "Order"})
    assert r.status_code == 204
    assert c.get("/bindings", params={"datahub_urn": ORDERS}).json() == []


def test_bind_unknown_class_is_404(service):
    r = client_for(service).post("/bindings", json={"datahub_urn": ORDERS, "class_iri": RET + "Nope"})
    assert r.status_code == 404


def test_bind_invalid_iri_is_400(service):
    r = client_for(service).post("/bindings", json={"datahub_urn": ORDERS, "class_iri": "bad iri"})
    assert r.status_code == 400


def test_sparql_select_and_update_rejected(service):
    c = client_for(service)
    r = c.post("/sparql", json={"query": "ASK { GRAPH ?g { ?s ?p ?o } }"})
    assert r.json()["boolean"] is True
    r = c.post("/sparql", json={"query": "DELETE WHERE { GRAPH ?g { ?s ?p ?o } }"})
    assert r.status_code == 400


def test_publish_glossary(service, fake_datahub):
    r = client_for(service).post("/glossary/publish", json={"graph": RETAIL_GRAPH, "name": "Retail"})
    assert r.status_code == 200
    assert len(r.json()["term_urns"]) == 5


def test_datahub_errors_are_502(service, fake_datahub, monkeypatch):
    from datahub.configuration.common import OperationalError

    def boom(*args, **kwargs):
        raise OperationalError("Unable to emit metadata to DataHub GMS", {"status": 401})

    monkeypatch.setattr(fake_datahub, "upsert_glossary_node", boom)
    r = client_for(service).post("/glossary/publish", json={"graph": RETAIL_GRAPH})
    assert r.status_code == 502
    assert "DataHub request failed" in r.json()["detail"]
