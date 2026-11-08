"""Load the worked example into a VeroMesh API and check it is there.

    python tests/k8s/smoke.py <api-url> load    # commit the example as source k8s-smoke
    python tests/k8s/smoke.py <api-url> check   # the committed model is still there
    python tests/k8s/smoke.py <api-url> note    # one more small write (a new changeset)
    python tests/k8s/smoke.py <fuseki-dataset-url> count   # quads in one Fuseki

Standard library only, so it runs anywhere Python does.
"""

from __future__ import annotations

import json
import sys
import urllib.request

SOURCE = "k8s-smoke"


def call(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read()
        return json.loads(text) if text else None


def load(base: str) -> None:
    items = call(base, "GET", "/api/v1/examples/procure-to-pay")["items"]
    cs = call(base, "POST", "/api/v1/changesets", {"source": SOURCE})["id"]
    call(base, "POST", f"/api/v1/changesets/{cs}/items", {"items": items})
    report = call(base, "POST", f"/api/v1/changesets/{cs}/validate")
    assert report["valid"], report
    committed = call(base, "POST", f"/api/v1/changesets/{cs}/commit")
    print(f"committed {len(items)} entities, {committed.get('facts')} facts")
    check(base)


def check(base: str) -> None:
    source = next((s for s in call(base, "GET", "/api/v1/sources") if s["source"] == SOURCE), None)
    assert source and source["facts"] > 0, f"source {SOURCE} missing or empty: {source}"
    authority = call(base, "GET", "/api/v1/authorities/AssistantApprovalAuthority")
    assert authority["holder"] == "InvoiceAssistant", authority
    prov = call(base, "GET", "/api/v1/authorities/AssistantApprovalAuthority/provenance")
    assert prov["fields"], prov
    print(f"model present: {source['facts']} facts from {SOURCE}")


def note(base: str) -> None:
    """A second, small write: open a changeset (recorded in the knowledge graph)."""
    cs = call(base, "POST", "/api/v1/changesets", {"source": SOURCE + "-2"})["id"]
    print(f"opened changeset {cs}")


def count(dataset: str) -> None:
    """Number of quads in a Fuseki dataset (writer or replica), straight from its query endpoint."""
    q = "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"
    req = urllib.request.Request(
        dataset + "/query",
        data=q.encode(),
        method="POST",
        headers={"Content-Type": "application/sparql-query", "Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        print(json.load(r)["results"]["bindings"][0]["n"]["value"])


if __name__ == "__main__":
    base, what = sys.argv[1].rstrip("/"), sys.argv[2]
    {"load": load, "check": check, "note": note, "count": count}[what](base)
