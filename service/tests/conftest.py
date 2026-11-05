from __future__ import annotations

import os
from pathlib import Path

import pytest

from veromesh.datahub import Binding
from veromesh.rdf_store import RDFStore, make_store
from veromesh.service import VeromeshService

ONTOLOGY_DIR = Path(__file__).resolve().parents[2] / "ontology"
RETAIL_GRAPH = "https://example.com/ontology/retail"
CORE_GRAPH = "https://veromesh.ai/ontology/core"
BINDINGS_GRAPH = "https://veromesh.ai/graph/bindings"
RET = "https://example.com/ontology/retail#"


class FakeDataHub:
    """In-memory stand-in for DataHubSdkPort."""

    def __init__(self) -> None:
        self.bindings: dict[str, list[Binding]] = {}
        self.nodes: dict[str, dict] = {}
        self.terms: dict[str, dict] = {}
        self.asset_terms: dict[str, list[str]] = {}

    def get_bindings(self, urn: str) -> list[Binding]:
        return list(self.bindings.get(urn, []))

    def put_bindings(self, urn: str, bindings: list[Binding]) -> None:
        self.bindings[urn] = list(bindings)

    def upsert_glossary_node(self, node_id, name, definition, parent_node_urn=None) -> str:
        urn = f"urn:li:glossaryNode:{node_id}"
        self.nodes[urn] = {"name": name, "definition": definition, "parent": parent_node_urn}
        return urn

    def upsert_glossary_term(self, iri, name, definition, parent_node_urn) -> str:
        from veromesh.datahub import term_urn_for

        urn = term_urn_for(iri)
        self.terms[urn] = {"iri": iri, "name": name, "definition": definition, "parent": parent_node_urn}
        return urn

    def set_asset_terms(self, urn: str, add: list[str], remove: list[str]) -> None:
        current = [t for t in self.asset_terms.get(urn, []) if t not in remove]
        current += [t for t in add if t not in current]
        self.asset_terms[urn] = current


def fresh_store() -> RDFStore:
    """The in-memory store, or the store named by VEROMESH_TEST_STORE (emptied first).

    The regression harness sets VEROMESH_TEST_STORE=fuseki:<dataset URL> to run
    this whole suite against a real Fuseki (scripts/test.sh integration).
    """
    spec = os.environ.get("VEROMESH_TEST_STORE", "memory")
    store = make_store(spec)
    if spec != "memory":
        store.update("DROP ALL")
    return store


@pytest.fixture
def fake_datahub() -> FakeDataHub:
    return FakeDataHub()


@pytest.fixture
def service(fake_datahub: FakeDataHub) -> VeromeshService:
    svc = VeromeshService(fresh_store(), fake_datahub, bindings_graph=BINDINGS_GRAPH)
    svc.load_ontology(CORE_GRAPH, (ONTOLOGY_DIR / "veromesh-core.ttl").read_text())
    svc.load_ontology(RETAIL_GRAPH, (ONTOLOGY_DIR / "examples" / "retail.ttl").read_text())
    return svc
