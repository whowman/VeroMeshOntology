"""Import, validate and export the organizational model.

Each import source (one workbook, later one connector) is written to its own
named graph, replaced as a whole on re-import, with a provenance record in a
separate graph: where the facts came from, when, and a checksum of the file.

Nothing is written unless the workbook parses cleanly and passes every
validation rule at Violation level. Warnings (completeness gaps) are returned
but do not block the import.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from pyshacl import validate
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from .ontology import literal
from .rdf_store import RDFStore
from .workbook import ParseResult, graph_to_workbook, local_id, workbook_to_graph

SH = Namespace("http://www.w3.org/ns/shacl#")
SOURCE_GRAPH_BASE = "https://veromesh.ai/graph/source/"
PROVENANCE_GRAPH = "https://veromesh.ai/graph/provenance"
CORE_GRAPH = "https://veromesh.ai/ontology/core"
ORG_GRAPH = "https://veromesh.ai/ontology/org"
SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def source_graph(source: str) -> str:
    return f"{SOURCE_GRAPH_BASE}spreadsheet/{source}"


def slugify_source(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")
    return slug or "workbook"


def check_source(source: str) -> str:
    if not SOURCE_NAME.match(source):
        raise ValueError("Source name must be lowercase letters, digits and '-', up to 64 characters.")
    return source


@lru_cache(maxsize=4)
def _model(ontology_dir: str) -> tuple[Graph, Graph]:
    root = Path(ontology_dir)
    ont = Graph()
    ont.parse(root / "veromesh-core.ttl")
    ont.parse(root / "veromesh-org.ttl")
    shapes = Graph().parse(root / "shapes" / "veromesh-org-shapes.ttl")
    return ont, shapes


@dataclass
class Finding:
    severity: str
    message: str
    focus: str
    sheet: str | None
    row: int | None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def validate_graph(
    data: Graph, ontology_dir: str, parsed: ParseResult | None = None, base: str = ""
) -> list[Finding]:
    ont, shapes = _model(ontology_dir)
    _, results, _ = validate(
        data, shacl_graph=shapes, ont_graph=ont, inference="rdfs", advanced=True, allow_warnings=True
    )
    findings = []
    # Messages quote values as full IRIs; show instance IRIs as the IDs written in the workbook,
    # and model terms by their local name.
    id_iri = re.compile(re.escape(base) + r"([A-Za-z0-9._%-]+)") if base else None
    model_iri = re.compile(r"https://veromesh\.ai/ontology/(?:org|core)#([A-Za-z]+)")

    def tidy(message: str) -> str:
        if id_iri is not None:
            message = id_iri.sub(lambda m: unquote(m.group(1)), message)
        return model_iri.sub(lambda m: m.group(1), message)

    for r in results.subjects(SH.resultSeverity, None):
        focus = results.value(r, SH.focusNode)
        sheet = row = None
        label = str(focus)
        if parsed is not None and isinstance(focus, URIRef):
            sheet, row = parsed.locations.get(focus, (None, None))
            label = parsed.ids.get(focus, local_id(base, focus))
        findings.append(
            Finding(
                severity=str(results.value(r, SH.resultSeverity)).split("#")[-1],
                message=tidy(str(results.value(r, SH.resultMessage) or "Validation rule failed.")),
                focus=label,
                sheet=sheet,
                row=row,
            )
        )
    findings.sort(key=lambda f: (f.severity != "Violation", f.sheet or "", f.row or 0, f.message))
    return findings


def _ensure_model_loaded(store: RDFStore, ontology_dir: str) -> None:
    root = Path(ontology_dir)
    store.load_turtle((root / "veromesh-core.ttl").read_text(), CORE_GRAPH)
    store.load_turtle((root / "veromesh-org.ttl").read_text(), ORG_GRAPH)


def import_workbook(
    store: RDFStore,
    data: bytes,
    *,
    source: str,
    filename: str,
    base: str,
    ontology_dir: str,
    dry_run: bool = False,
) -> dict:
    check_source(source)
    parsed = workbook_to_graph(data, base)
    findings = [] if parsed.errors else validate_graph(parsed.graph, ontology_dir, parsed, base)
    violations = [f for f in findings if f.severity == "Violation"]
    warnings = [f for f in findings if f.severity != "Violation"]
    ok = not parsed.errors and not violations
    graph_iri = source_graph(source)
    written = False

    if ok and not dry_run:
        _ensure_model_loaded(store, ontology_dir)
        store.load_turtle(parsed.graph.serialize(format="turtle"), graph_iri)
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        digest = hashlib.sha256(data).hexdigest()
        store.update(
            f"""
            PREFIX prov: <http://www.w3.org/ns/prov#>
            PREFIX dcterms: <http://purl.org/dc/terms/>
            PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
            DELETE {{ GRAPH <{PROVENANCE_GRAPH}> {{ <{graph_iri}> ?p ?o }} }}
            WHERE  {{ GRAPH <{PROVENANCE_GRAPH}> {{ <{graph_iri}> ?p ?o }} }} ;
            INSERT DATA {{ GRAPH <{PROVENANCE_GRAPH}> {{
              <{graph_iri}> a prov:Entity ;
                dcterms:identifier {literal(source)} ;
                dcterms:type "spreadsheet" ;
                dcterms:source {literal(filename)} ;
                prov:generatedAtTime "{now}"^^xsd:dateTime ;
                <https://veromesh.ai/ontology/core#sha256> {literal(digest)} ;
                <https://veromesh.ai/ontology/core#tripleCount> {len(parsed.graph)} .
            }} }}
            """
        )
        written = True

    return {
        "source": source,
        "graph": graph_iri,
        "dry_run": dry_run,
        "written": written,
        "rows": parsed.counts,
        "summary": {"errors": len(parsed.errors), "violations": len(violations), "warnings": len(warnings)},
        "errors": [e.as_dict() for e in parsed.errors],
        "violations": [f.as_dict() for f in violations],
        "warnings": [f.as_dict() for f in warnings],
    }


def list_sources(store: RDFStore) -> list[dict]:
    res = store.query(
        f"""
        PREFIX prov: <http://www.w3.org/ns/prov#>
        PREFIX dcterms: <http://purl.org/dc/terms/>
        SELECT ?g ?id ?type ?file ?at ?n WHERE {{
          GRAPH <{PROVENANCE_GRAPH}> {{
            ?g dcterms:identifier ?id ; dcterms:type ?type ; prov:generatedAtTime ?at .
            OPTIONAL {{ ?g dcterms:source ?file }}
            OPTIONAL {{ ?g <https://veromesh.ai/ontology/core#tripleCount> ?n }}
          }}
        }} ORDER BY ?id
        """
    )
    out = []
    for b in res["results"]["bindings"]:
        out.append(
            {
                "source": b["id"]["value"],
                "type": b["type"]["value"],
                "graph": b["g"]["value"],
                "file": b.get("file", {}).get("value"),
                "loaded_at": b["at"]["value"],
                "facts": int(b["n"]["value"]) if "n" in b else None,
            }
        )
    return out


def _term(b: dict):
    t = b["type"]
    if t == "uri":
        return URIRef(b["value"])
    if t == "bnode":
        return BNode(b["value"])
    if "datatype" in b:
        return Literal(b["value"], datatype=URIRef(b["datatype"]))
    if "xml:lang" in b:
        return Literal(b["value"], lang=b["xml:lang"])
    return Literal(b["value"], datatype=XSD.string) if t == "typed-literal" else Literal(b["value"])


def read_graph(store: RDFStore, graph_iri: str) -> Graph:
    res = store.query(f"SELECT ?s ?p ?o WHERE {{ GRAPH <{graph_iri}> {{ ?s ?p ?o }} }}")
    g = Graph()
    for b in res["results"]["bindings"]:
        g.add((_term(b["s"]), _term(b["p"]), _term(b["o"])))
    return g


def export_workbook(store: RDFStore, source: str, base: str) -> bytes:
    check_source(source)
    g = read_graph(store, source_graph(source))
    if len(g) == 0:
        from .service import NotFound

        raise NotFound(f"No imported source named '{source}'.")
    return graph_to_workbook(g, base)
