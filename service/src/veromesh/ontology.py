"""Ontology operations on top of an ``RDFStore``.

Vocabulary used for bindings lives in ``ontology/veromesh-core.ttl``:

    <asset>  a vm:DataAsset ;
             vm:datahubUrn "urn:li:dataset:(...)" ;
             vm:represents <https://example.com/ontology/retail#Customer> .
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

from .rdf_store import RDFStore

VM = "https://veromesh.ai/ontology/core#"
ASSET_BASE = "https://veromesh.ai/asset/"

_IRI_FORBIDDEN = re.compile(r'[\s<>"{}|\\^`]')


def check_iri(value: str) -> str:
    """Reject strings that are not safe to place inside ``<...>`` in SPARQL."""
    if not value or _IRI_FORBIDDEN.search(value) or ":" not in value:
        raise ValueError(f"Invalid IRI: {value!r}")
    return value


def literal(value: str) -> str:
    """Escape a plain string as a SPARQL string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def asset_iri(datahub_urn: str) -> str:
    """Stable IRI for a DataHub entity URN (URNs can contain characters IRIs cannot)."""
    return ASSET_BASE + quote(datahub_urn, safe="")


@dataclass
class OntologyClass:
    iri: str
    label: str | None = None
    definition: str | None = None
    parents: list[str] = field(default_factory=list)
    graph: str | None = None

    @property
    def display_name(self) -> str:
        if self.label:
            return self.label
        tail = re.split(r"[#/]", self.iri.rstrip("/#"))[-1]
        return tail or self.iri


CLASSES_QUERY = """
PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?g ?cls ?label ?definition ?parent WHERE {
  GRAPH ?g {
    ?cls a owl:Class .
    FILTER(isIRI(?cls))
    OPTIONAL { ?cls rdfs:label ?label }
    OPTIONAL { ?cls skos:definition ?definition }
    OPTIONAL { ?cls rdfs:comment ?definition }
    OPTIONAL { ?cls rdfs:subClassOf ?parent . FILTER(isIRI(?parent)) }
  }
  %(graph_filter)s
}
ORDER BY ?cls
"""


def list_classes(store: RDFStore, graph: str | None = None) -> list[OntologyClass]:
    graph_filter = f"FILTER(?g = <{check_iri(graph)}>)" if graph else ""
    results = store.query(CLASSES_QUERY % {"graph_filter": graph_filter})
    by_iri: dict[str, OntologyClass] = {}
    for row in results["results"]["bindings"]:
        iri = row["cls"]["value"]
        cls = by_iri.setdefault(iri, OntologyClass(iri=iri, graph=row["g"]["value"]))
        if "label" in row and cls.label is None:
            cls.label = row["label"]["value"]
        if "definition" in row and cls.definition is None:
            cls.definition = row["definition"]["value"]
        if "parent" in row and row["parent"]["value"] not in cls.parents:
            cls.parents.append(row["parent"]["value"])
    return list(by_iri.values())


def add_binding(store: RDFStore, bindings_graph: str, datahub_urn: str, class_iri: str) -> None:
    asset = asset_iri(datahub_urn)
    store.update(
        f"""
        PREFIX vm: <{VM}>
        INSERT DATA {{
          GRAPH <{check_iri(bindings_graph)}> {{
            <{asset}> a vm:DataAsset ;
                      vm:datahubUrn {literal(datahub_urn)} ;
                      vm:represents <{check_iri(class_iri)}> .
          }}
        }}
        """
    )


def remove_binding(store: RDFStore, bindings_graph: str, datahub_urn: str, class_iri: str) -> None:
    store.update(
        f"""
        PREFIX vm: <{VM}>
        DELETE DATA {{
          GRAPH <{check_iri(bindings_graph)}> {{
            <{asset_iri(datahub_urn)}> vm:represents <{check_iri(class_iri)}> .
          }}
        }}
        """
    )


def classes_for_asset(store: RDFStore, bindings_graph: str, datahub_urn: str) -> list[str]:
    results = store.query(
        f"""
        PREFIX vm: <{VM}>
        SELECT ?cls WHERE {{
          GRAPH <{check_iri(bindings_graph)}> {{ <{asset_iri(datahub_urn)}> vm:represents ?cls }}
        }} ORDER BY ?cls
        """
    )
    return [b["cls"]["value"] for b in results["results"]["bindings"]]


def assets_for_class(
    store: RDFStore, bindings_graph: str, class_iri: str, *, include_subclasses: bool = True
) -> list[str]:
    """DataHub URNs bound to a class (and, by default, to any of its subclasses)."""
    target = check_iri(class_iri)
    candidates = f"{{ BIND(<{target}> AS ?cls) }}"
    if include_subclasses:
        candidates += f" UNION {{ GRAPH ?og {{ ?cls rdfs:subClassOf+ <{target}> }} }}"
    match = f"""
      {candidates}
      GRAPH <{check_iri(bindings_graph)}> {{ ?asset vm:represents ?cls ; vm:datahubUrn ?urn }}
    """
    results = store.query(
        f"""
        PREFIX vm: <{VM}>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?urn WHERE {{ {match} }} ORDER BY ?urn
        """
    )
    return [b["urn"]["value"] for b in results["results"]["bindings"]]
