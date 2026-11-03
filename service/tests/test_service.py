from __future__ import annotations

import pytest

from veromesh.datahub import term_urn_for
from veromesh.ontology import asset_iri, check_iri, literal
from veromesh.service import NotFound

from .conftest import BINDINGS_GRAPH, CORE_GRAPH, RET, RETAIL_GRAPH

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,sales.public.orders,PROD)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,crm.public.customers,PROD)"
LOYALTY = "urn:li:dataset:(urn:li:dataPlatform:snowflake,crm.public.loyalty members,PROD)"


def test_lists_ontologies_without_bindings_graph(service):
    service.bind(ORDERS, RET + "Order")
    assert set(service.list_ontologies()) == {CORE_GRAPH, RETAIL_GRAPH}


def test_list_classes_reads_labels_definitions_and_parents(service):
    classes = {c.iri: c for c in service.list_classes(RETAIL_GRAPH)}
    assert set(classes) == {RET + n for n in ("Party", "Customer", "LoyaltyMember", "Product", "Order")}
    customer = classes[RET + "Customer"]
    assert customer.label == "Customer"
    assert customer.definition.startswith("A party")
    assert customer.parents == [RET + "Party"]
    assert customer.graph == RETAIL_GRAPH


def test_reloading_replaces_graph(service):
    service.load_ontology(
        RETAIL_GRAPH,
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . <https://example.com/x#Only> a owl:Class .",
    )
    assert [c.iri for c in service.list_classes(RETAIL_GRAPH)] == ["https://example.com/x#Only"]


def test_cannot_overwrite_bindings_graph(service):
    with pytest.raises(ValueError):
        service.load_ontology(BINDINGS_GRAPH, "")


def test_bind_writes_rdf_custom_aspect_and_glossary_term(service, fake_datahub):
    binding = service.bind(ORDERS, RET + "Order")

    assert binding.ontology_iri == RETAIL_GRAPH
    assert service.classes_for_asset(ORDERS) == [RET + "Order"]
    assert [b.class_iri for b in fake_datahub.bindings[ORDERS]] == [RET + "Order"]
    assert fake_datahub.asset_terms[ORDERS] == [term_urn_for(RET + "Order")]


def test_bind_is_idempotent(service, fake_datahub):
    service.bind(ORDERS, RET + "Order")
    service.bind(ORDERS, RET + "Order")
    assert len(fake_datahub.bindings[ORDERS]) == 1
    assert service.classes_for_asset(ORDERS) == [RET + "Order"]


def test_bind_unknown_class_raises(service):
    with pytest.raises(NotFound):
        service.bind(ORDERS, RET + "Nope")


def test_unbind_removes_everywhere(service, fake_datahub):
    service.bind(ORDERS, RET + "Order")
    service.bind(ORDERS, RET + "Product")
    service.unbind(ORDERS, RET + "Order")

    assert service.classes_for_asset(ORDERS) == [RET + "Product"]
    assert [b.class_iri for b in fake_datahub.bindings[ORDERS]] == [RET + "Product"]
    assert fake_datahub.asset_terms[ORDERS] == [term_urn_for(RET + "Product")]


def test_assets_for_class_follows_subclass_hierarchy(service):
    service.bind(CUSTOMERS, RET + "Customer")
    service.bind(LOYALTY, RET + "LoyaltyMember")
    service.bind(ORDERS, RET + "Order")

    assert service.assets_for_class(RET + "Party") == sorted([CUSTOMERS, LOYALTY])
    assert service.assets_for_class(RET + "Customer", include_subclasses=False) == [CUSTOMERS]


def test_publish_glossary_creates_node_and_terms(service, fake_datahub):
    result = service.publish_glossary(RETAIL_GRAPH, "Retail")

    assert len(result.term_urns) == 5
    assert fake_datahub.nodes[result.node_urn]["name"] == "Retail"
    assert fake_datahub.nodes[result.node_urn]["parent"] == "urn:li:glossaryNode:veromesh.root"
    customer = fake_datahub.terms[term_urn_for(RET + "Customer")]
    assert customer["name"] == "Customer"
    assert customer["parent"] == result.node_urn


def test_publish_empty_graph_raises(service):
    with pytest.raises(NotFound):
        service.publish_glossary("https://example.com/empty")


def test_term_urns_are_stable():
    assert term_urn_for(RET + "Order") == term_urn_for(RET + "Order")
    assert term_urn_for(RET + "Order") != term_urn_for(RET + "Product")


@pytest.mark.parametrize("bad", ["", "no-scheme", "https://x.com/a b", "https://x.com/>", 'https://x.com/"'])
def test_check_iri_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        check_iri(bad)


def test_literal_and_asset_iri_escape_injection_attempts():
    assert literal('a"} ; DROP ALL ; {"') == '"a\\"} ; DROP ALL ; {\\""'
    assert " " not in asset_iri(LOYALTY) and "(" not in asset_iri(LOYALTY)
