"""Use cases that keep the RDF store and DataHub consistent.

The RDF store is the source of truth for the ontology itself (classes,
hierarchy, axioms). DataHub is the source of truth for data assets. Bindings
are written to both: as triples (for SPARQL / reasoning) and as DataHub
aspects + glossary terms (for search, lineage and the DataHub UI).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import ontology
from .datahub import Binding, DataHubPort, term_urn_for
from .rdf_store import RDFStore

ROOT_NODE_ID = "veromesh.root"


class NotFound(LookupError):
    pass


@dataclass
class PublishResult:
    graph: str
    node_urn: str
    term_urns: list[str]


class VeromeshService:
    def __init__(
        self,
        store: RDFStore,
        datahub: DataHubPort,
        *,
        bindings_graph: str,
        glossary_root_name: str = "Veromesh Ontology",
    ) -> None:
        self.store = store
        self.datahub = datahub
        self.bindings_graph = bindings_graph
        self.glossary_root_name = glossary_root_name

    # -- ontologies --------------------------------------------------------

    def load_ontology(self, graph: str, turtle: str, *, replace: bool = True) -> None:
        ontology.check_iri(graph)
        if graph == self.bindings_graph:
            raise ValueError("The bindings graph is managed by Veromesh and cannot be loaded directly")
        self.store.load_turtle(turtle, graph, replace=replace)

    def list_ontologies(self) -> list[str]:
        return [g for g in self.store.list_graphs() if g != self.bindings_graph]

    def list_classes(self, graph: str | None = None) -> list[ontology.OntologyClass]:
        return [c for c in ontology.list_classes(self.store, graph) if c.graph != self.bindings_graph]

    def _find_class(self, class_iri: str) -> ontology.OntologyClass:
        ontology.check_iri(class_iri)
        for cls in self.list_classes():
            if cls.iri == class_iri:
                return cls
        raise NotFound(f"Class {class_iri} is not defined in any loaded ontology")

    def publish_glossary(self, graph: str, name: str | None = None) -> PublishResult:
        """Publish every owl:Class in ``graph`` as a DataHub glossary term."""
        classes = self.list_classes(graph)
        if not classes:
            raise NotFound(f"No classes found in graph {graph}")
        root = self.datahub.upsert_glossary_node(
            ROOT_NODE_ID, self.glossary_root_name, "Business concepts managed in Veromesh."
        )
        node_id = "veromesh." + hashlib.sha1(graph.encode("utf-8")).hexdigest()[:12]
        node = self.datahub.upsert_glossary_node(
            node_id, name or graph, f"Classes from ontology {graph}", parent_node_urn=root
        )
        term_urns = [
            self.datahub.upsert_glossary_term(
                cls.iri,
                cls.display_name,
                cls.definition or f"Ontology class {cls.iri}",
                parent_node_urn=node,
            )
            for cls in classes
        ]
        return PublishResult(graph=graph, node_urn=node, term_urns=term_urns)

    # -- bindings ----------------------------------------------------------

    def bind(self, datahub_urn: str, class_iri: str) -> Binding:
        cls = self._find_class(class_iri)
        binding = Binding(class_iri=cls.iri, ontology_iri=cls.graph or "", label=cls.label)

        ontology.add_binding(self.store, self.bindings_graph, datahub_urn, cls.iri)

        existing = [b for b in self.datahub.get_bindings(datahub_urn) if b.class_iri != cls.iri]
        self.datahub.put_bindings(datahub_urn, existing + [binding])
        self.datahub.set_asset_terms(datahub_urn, add=[term_urn_for(cls.iri)], remove=[])
        return binding

    def unbind(self, datahub_urn: str, class_iri: str) -> None:
        ontology.check_iri(class_iri)
        ontology.remove_binding(self.store, self.bindings_graph, datahub_urn, class_iri)

        remaining = [b for b in self.datahub.get_bindings(datahub_urn) if b.class_iri != class_iri]
        self.datahub.put_bindings(datahub_urn, remaining)
        self.datahub.set_asset_terms(datahub_urn, add=[], remove=[term_urn_for(class_iri)])

    def classes_for_asset(self, datahub_urn: str) -> list[str]:
        return ontology.classes_for_asset(self.store, self.bindings_graph, datahub_urn)

    def assets_for_class(self, class_iri: str, *, include_subclasses: bool = True) -> list[str]:
        return ontology.assets_for_class(
            self.store, self.bindings_graph, class_iri, include_subclasses=include_subclasses
        )

    def sparql(self, query: str) -> dict:
        return self.store.query(query)
