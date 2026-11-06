"""Contract snapshots: what other components and customers depend on.

Each test renders one contract as stable JSON and compares it with the
snapshot in golden/. A difference fails with a diff. If the change is
intended, accept it and commit the new snapshot with the code:

    scripts/test.sh contracts --update-golden

Covered: the model API schema, the HTTP routes and their request/response
shapes, the ontology and validation-rule inventory, the validation result of
the worked example, and the connector command line.
"""

from __future__ import annotations

import difflib
import json
import os
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef
from veromesh.api import create_app
from veromesh.config import Settings
from veromesh.rdf_store import MemoryStore
from veromesh.service import VeromeshService

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).parent / "golden"
SH = Namespace("http://www.w3.org/ns/shacl#")


def check(name: str, data) -> None:
    path = GOLDEN / f"{name}.json"
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("VEROMESH_UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(text)
        return
    if not path.exists():
        pytest.fail(
            f"No snapshot golden/{name}.json yet. Create it: scripts/test.sh contracts --update-golden"
        )
    old = path.read_text()
    if old != text:
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(True), text.splitlines(True), f"golden/{name}.json", "now", n=2
            )
        )
        pytest.fail(
            f"Contract '{name}' changed. If this is intended, run "
            f"'scripts/test.sh contracts --update-golden' and commit the snapshot.\n{diff[:8000]}",
            pytrace=False,
        )


class NoDataHub:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected DataHub call: {name}")


@pytest.fixture(scope="module")
def client() -> TestClient:
    svc = VeromeshService(MemoryStore(), NoDataHub(), bindings_graph="https://veromesh.ai/graph/bindings")
    return TestClient(create_app(svc, Settings(rdf_store="memory")))


# ---------------------------------------------------------------------------- API


def test_model_schema(client):
    """Entity types, fields, kinds and reference targets: the workbook and every connector depend on it."""
    check("api-schema", client.get("/api/v1/schema").json())


def _shape(schema: dict, components: dict, depth: int = 0):
    """Reduce a JSON schema to names, types and required-ness."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return name if depth else _shape(components[name], components, depth + 1)
    if "anyOf" in schema:
        return {"anyOf": [_shape(s, components, depth) for s in schema["anyOf"]]}
    if schema.get("type") == "array":
        return {"array": _shape(schema.get("items", {}), components, depth)}
    if "properties" in schema:
        req = set(schema.get("required", []))
        return {
            k + ("" if k in req else "?"): _shape(v, components, depth + 1)
            for k, v in schema["properties"].items()
        }
    return schema.get("type", "any")


def test_http_routes(client):
    """Every route, its parameters and body shapes. Removing or renaming anything fails here."""
    spec = client.get("/openapi.json").json()
    comps = spec.get("components", {}).get("schemas", {})
    routes = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            entry = {
                "params": sorted(
                    p["name"] + ("" if p.get("required") else "?") + f" ({p['in']})"
                    for p in op.get("parameters", [])
                ),
                "status": sorted(op.get("responses", {})),
            }
            body = op.get("requestBody", {}).get("content", {})
            if body:
                ctype, media = next(iter(body.items()))
                entry["body"] = {ctype: _shape(media.get("schema", {}), comps)}
            routes[f"{method.upper()} {path}"] = entry
    check("api-routes", routes)


def test_worked_example_validation(client):
    """The procure-to-pay example loads with the same findings and entity counts as before."""
    items = client.get("/api/v1/examples/procure-to-pay").json()["items"]
    cs = client.post("/api/v1/changesets", json={"source": "contract"}).json()["id"]
    client.post(f"/api/v1/changesets/{cs}/items", json={"items": items})
    report = client.post(f"/api/v1/changesets/{cs}/validate").json()
    findings = sorted(
        f"{kind[:-1]}: {f.get('type')} {f.get('id')} {f.get('field') or ''} | {f['message']}"
        for kind in ("errors", "violations", "warnings")
        for f in report.get(kind, [])
    )
    committed = client.post(f"/api/v1/changesets/{cs}/commit").json()
    check(
        "example-procure-to-pay",
        {
            "valid": report["valid"],
            "summary": report["summary"],
            "findings": findings,
            "entities": dict(sorted(Counter(i["type"] for i in items).items())),
            "facts": committed.get("facts"),
        },
    )


# ---------------------------------------------------------------------------- ontology


def _curie(g: Graph, node) -> str:
    if isinstance(node, URIRef):
        try:
            return g.namespace_manager.normalizeUri(node)
        except Exception:
            return str(node)
    union = g.value(node, OWL.unionOf) if node is not None else None
    if union is not None:
        return "(" + " or ".join(sorted(_curie(g, n) for n in g.items(union))) + ")"
    return "[]" if node is not None else ""


def _path(g: Graph, node) -> str:
    """A SHACL property path as text, e.g. '^vmo:fills' or 'vmo:a / vmo:b' (blank nodes have no stable IDs)."""
    if isinstance(node, URIRef):
        return _curie(g, node)
    if (node, RDF.first, None) in g:
        return " / ".join(_path(g, n) for n in g.items(node))
    for pred, fmt in (
        (SH.inversePath, "^{}"),
        (SH.zeroOrMorePath, "({})*"),
        (SH.oneOrMorePath, "({})+"),
        (SH.zeroOrOnePath, "({})?"),
    ):
        inner = g.value(node, pred)
        if inner is not None:
            return fmt.format(_path(g, inner))
    alt = g.value(node, SH.alternativePath)
    if alt is not None:
        return "(" + " | ".join(_path(g, n) for n in g.items(alt)) + ")"
    return "[]"


def test_ontology_inventory():
    """Classes, properties and validation rules of the core vocabulary and the organizational meta-model."""
    out = {}
    for rel in ("ontology/veromesh-core.ttl", "ontology/veromesh-org.ttl", "ontology/veromesh-prov.ttl"):
        g = Graph().parse(ROOT / rel)
        classes = {
            _curie(g, c): sorted(_curie(g, p) for p in g.objects(c, RDFS.subClassOf) if isinstance(p, URIRef))
            for c in g.subjects(RDF.type, OWL.Class)
            if isinstance(c, URIRef)
        }
        props = {}
        for kind in (OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty):
            for p in g.subjects(RDF.type, kind):
                props[_curie(g, p)] = {
                    "kind": _curie(g, kind),
                    "domain": sorted(_curie(g, d) for d in g.objects(p, RDFS.domain)),
                    "range": sorted(_curie(g, r) for r in g.objects(p, RDFS.range)),
                }
        out[rel] = {"classes": dict(sorted(classes.items())), "properties": dict(sorted(props.items()))}

    for rel in ("ontology/shapes/veromesh-org-shapes.ttl", "ontology/shapes/veromesh-prov-shapes.ttl"):
        g = Graph().parse(ROOT / rel)
        shapes = {}
        for s in set(g.subjects(RDF.type, SH.NodeShape)):
            rules = []
            for ps in g.objects(s, SH.property):
                rule = {
                    k: _curie(g, v) if isinstance(v, URIRef) else str(v)
                    for k, v in (
                        ("path", Literal(_path(g, g.value(ps, SH.path)))),
                        ("minCount", g.value(ps, SH.minCount)),
                        ("maxCount", g.value(ps, SH.maxCount)),
                        ("datatype", g.value(ps, SH.datatype)),
                        ("class", g.value(ps, SH["class"])),
                        ("severity", g.value(ps, SH.severity)),
                    )
                    if v is not None
                }
                rules.append(rule)
            sparql = len(list(g.objects(s, SH.sparql)))
            shapes[_curie(g, s)] = {
                "target": sorted(_curie(g, t) for t in g.objects(s, SH.targetClass)),
                "closed": str(g.value(s, SH.closed) or "false"),
                "severity": _curie(g, g.value(s, SH.severity)) if g.value(s, SH.severity) else "",
                "properties": sorted(rules, key=lambda r: json.dumps(r, sort_keys=True)),
                "sparqlRules": sparql,
            }
        out[rel] = dict(sorted(shapes.items()))
    check("ontology-inventory", out)


# ---------------------------------------------------------------------------- connectors


def test_connector_cli():
    """Commands and options of veromesh-connect: scripts and docs depend on them."""
    import argparse

    from veromesh_connectors.cli import parser

    def walk(p: argparse.ArgumentParser) -> dict:
        node: dict = {"options": [], "args": []}
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                node["commands"] = {name: walk(sp) for name, sp in sorted(a.choices.items())}
            elif isinstance(a, (argparse._HelpAction, argparse._VersionAction)):
                continue
            elif a.option_strings:
                node["options"].append(
                    "/".join(a.option_strings) + (f" {list(a.choices)}" if a.choices else "")
                )
            else:
                node["args"].append(a.dest)
        return node

    check("connectors-cli", walk(parser()))
