"""End-to-end regression tests against a running stack (scripts/dev-up.sh).

Run them with ``scripts/test.sh e2e``. They talk to the real services:

    VEROMESH_E2E_API      Veromesh API         default http://localhost:8700
    VEROMESH_E2E_GMS      DataHub GMS          default http://localhost:8080
    VEROMESH_E2E_FUSEKI   Fuseki dataset URL   default http://localhost:3030/veromesh
    VEROMESH_E2E_TOKEN_FILE  DataHub token     default .build/datahub-token
    VEROMESH_E2E_FRONTEND DataHub UI           default http://localhost:9002

Everything they create is named "regtest" and removed afterwards, so they can
run against your development stack without touching your own sources.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("VEROMESH_E2E_API", "http://localhost:8700")
GMS = os.environ.get("VEROMESH_E2E_GMS", "http://localhost:8080")
FUSEKI = os.environ.get("VEROMESH_E2E_FUSEKI", "http://localhost:3030/veromesh").rstrip("/")
FRONTEND = os.environ.get("VEROMESH_E2E_FRONTEND", "http://localhost:9002").rstrip("/")
TOKEN_FILE = Path(os.environ.get("VEROMESH_E2E_TOKEN_FILE", ROOT / ".build" / "datahub-token"))

CORE = "https://veromesh.ai/ontology/core"
RETAIL = "https://example.com/ontology/retail"
RET = RETAIL + "#"
DATASET = "urn:li:dataset:(urn:li:dataPlatform:snowflake,regtest.veromesh.e2e_customers,PROD)"
SOURCE = "regtest-p2p"
PREFIX = "regtest-"


def wait_until(check, seconds: float = 60, every: float = 3):
    deadline = time.time() + seconds
    while True:
        value = check()
        if value or time.time() > deadline:
            return value
        time.sleep(every)


@pytest.fixture(scope="session")
def api() -> httpx.Client:
    with httpx.Client(base_url=API, timeout=120) as c:
        yield c


@pytest.fixture(scope="session")
def gms() -> httpx.Client:
    token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=GMS, timeout=60, headers=headers) as c:
        yield c


def sparql(query: str) -> dict:
    r = httpx.post(
        f"{FUSEKI}/query",
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def entity(gms: httpx.Client, urn: str) -> dict:
    r = gms.get("/entitiesV2/" + urllib.parse.quote(urn, safe=""))
    return r.json() if r.status_code == 200 else {}


# ---------------------------------------------------------------------------- health


def test_services_are_up(api, gms):
    assert api.get("/health").json()["status"] == "ok"
    assert gms.get("/health").status_code == 200
    assert httpx.get(FUSEKI.rsplit("/", 1)[0] + "/$/ping", timeout=10).status_code == 200
    assert "veromesh-ontology-model" in gms.get("/config").text, (
        "DataHub has not loaded the Veromesh model plugin"
    )
    assert gms.post("/api/graphql", json={"query": "{ me { corpUser { urn } } }"}).status_code == 200, (
        "the DataHub token in .build/datahub-token is not accepted"
    )


# ---------------------------------------------------------------------------- branding


def test_datahub_ui_uses_the_veromesh_theme(api, gms):
    q = "{ appConfig { visualConfig { logoUrl faviconUrl appTitle theme { themeId } } } }"
    visual = gms.post("/api/graphql", json={"query": q}).json()["data"]["appConfig"]["visualConfig"]
    if not (visual.get("theme") or {}).get("themeId"):
        pytest.skip("stack started with SKIP_BRANDING=1")
    assert visual["theme"]["themeId"] == "veromesh.theme.json"
    assert visual["appTitle"] == "VeroMesh"
    logos = ROOT / "branding" / "logos"
    with httpx.Client(base_url=FRONTEND, timeout=30) as ui:
        theme = ui.get("/assets/conf/theme/veromesh.theme.json")
        assert theme.status_code == 200, "the frontend does not serve the VeroMesh theme"
        assert theme.json()["styles"]["primary-color"] == "#23395D"
        for url in (visual["logoUrl"], theme.json()["assets"]["logoUrl"]):
            r = ui.get("/" + url.lstrip("/"))
            assert r.status_code == 200 and r.content == (logos / url.rsplit("/", 1)[-1]).read_bytes(), url
        assert ui.get("/assets/icons/favicon.ico").content == (logos / "favicon.ico").read_bytes()
    # The API's own docs carry the brand too
    assert "/brand/logos/veromesh-landscape.svg" in api.get("/docs").text
    assert api.get("/favicon.ico").content == (logos / "favicon.ico").read_bytes()


# ---------------------------------------------------------------------------- ontologies and glossary


def test_ontologies_load_and_publish_to_the_glossary(api, gms):
    for graph, path in ((CORE, "ontology/veromesh-core.ttl"), (RETAIL, "ontology/examples/retail.ttl")):
        r = api.put(
            "/ontologies",
            params={"graph": graph},
            content=(ROOT / path).read_bytes(),
            headers={"Content-Type": "text/turtle"},
        )
        assert r.status_code == 204, r.text
    assert {CORE, RETAIL} <= set(api.get("/ontologies").json())
    classes = {c["iri"]: c for c in api.get("/classes", params={"graph": RETAIL}).json()}
    assert RET + "Customer" in classes and RET + "Party" in classes[RET + "Customer"]["parents"]

    r = api.post("/glossary/publish", json={"graph": RETAIL, "name": "Retail (example)"})
    assert r.status_code == 200, r.text

    from veromesh.datahub import term_urn_for

    term = term_urn_for(RET + "Customer")
    found = wait_until(lambda: "glossaryTermInfo" in entity(gms, term).get("aspects", {}))
    assert found, f"{term} not found in DataHub after publishing"


# ---------------------------------------------------------------------------- bindings


def _unbind_all(api):
    for c in api.get("/bindings", params={"datahub_urn": DATASET}).json():
        api.request("DELETE", "/bindings", json={"datahub_urn": DATASET, "class_iri": c})


def _bound_classes(gms) -> list[str]:
    aspect = entity(gms, DATASET).get("aspects", {}).get("veromeshOntologyBindings", {})
    return sorted(b["classIri"] for b in aspect.get("value", {}).get("bindings", []))


def test_bindings_reach_fuseki_and_datahub(api, gms):
    _unbind_all(api)
    try:
        for cls in ("Customer", "LoyaltyMember"):
            r = api.post("/bindings", json={"datahub_urn": DATASET, "class_iri": RET + cls})
            assert r.status_code == 201, r.text

        # Fuseki: queryable, and subclass-aware
        assert DATASET in api.get("/classes/assets", params={"class_iri": RET + "Party"}).json()
        ask = f"ASK {{ GRAPH ?g {{ ?b <https://veromesh.ai/ontology/core#represents> <{RET}Customer> }} }}"
        assert sparql(ask)["boolean"] is True

        # DataHub: both bindings in the custom aspect, both glossary terms on the dataset
        want = [RET + "Customer", RET + "LoyaltyMember"]
        assert wait_until(lambda: _bound_classes(gms) == want), _bound_classes(gms)
        terms = entity(gms, DATASET)["aspects"]["glossaryTerms"]["value"]["terms"]
        assert len(terms) == 2, terms
    finally:
        _unbind_all(api)
    assert DATASET not in api.get("/classes/assets", params={"class_iri": RET + "Party"}).json()
    assert wait_until(lambda: _bound_classes(gms) == []), _bound_classes(gms)


# ---------------------------------------------------------------------------- organizational model


def _prefixed_example(api) -> tuple[list[dict], dict]:
    """The worked example with every ID and reference prefixed, so it cannot collide with real sources."""
    schema = api.get("/api/v1/schema").json()
    refs = {t["type"]: {f["key"] for f in t["fields"] if f["kind"] in ("ref", "id")} for t in schema}
    items = api.get("/api/v1/examples/procure-to-pay").json()["items"]
    out: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        e = {}
        for k, v in it["entity"].items():
            if k in refs[it["type"]]:
                v = [PREFIX + x for x in v] if isinstance(v, list) else PREFIX + v
            e[k] = v
        out[it["type"]].append(e)
    return schema, out


@pytest.fixture
def clean_source(api):
    yield SOURCE
    # Empty the source: a replace changeset with no items.
    cs = api.post("/api/v1/changesets", json={"source": SOURCE, "kind": "api"}).json()["id"]
    api.post(f"/api/v1/changesets/{cs}/commit")


def test_org_model_through_the_spreadsheet_connector(api, tmp_path, capsys, clean_source):
    from veromesh_connectors.cli import main
    from veromesh_connectors.plugins import build_workbook

    schema, entities = _prefixed_example(api)
    book = tmp_path / "regtest.xlsx"
    book.write_bytes(build_workbook(schema, entities))

    assert main(["--api", API, "spreadsheet", "import", str(book), "--source", SOURCE]) == 0
    out = capsys.readouterr().out
    assert "0 errors, 0 violations" in out and "Committed changeset" in out, out

    # Through the API, and directly in the store
    authority = api.get(f"/api/v1/authorities/{PREFIX}AssistantApprovalAuthority").json()
    assert authority["holder"] == f"{PREFIX}InvoiceAssistant", authority
    graph = f"https://veromesh.ai/graph/source/{SOURCE}"
    assert sparql(f"ASK {{ GRAPH <{graph}> {{ ?a a <https://veromesh.ai/ontology/org#AIAgent> }} }}")[
        "boolean"
    ]
    source = next(s for s in api.get("/api/v1/sources").json() if s["source"] == SOURCE)
    assert source["facts"] > 0

    # Re-running unchanged input is a no-op
    assert main(["--api", API, "spreadsheet", "import", str(book), "--source", SOURCE]) == 0
    assert "already up to date" in capsys.readouterr().out
    assert (
        next(s for s in api.get("/api/v1/sources").json() if s["source"] == SOURCE)["version"]
        == source["version"]
    )

    # Export and re-import round-trips cleanly
    exported = tmp_path / "exported.xlsx"
    assert main(["--api", API, "spreadsheet", "export", "--source", SOURCE, "-o", str(exported)]) == 0
    assert main(["--api", API, "spreadsheet", "import", str(exported), "--source", SOURCE, "--dry-run"]) == 0
    assert "0 errors, 0 violations" in capsys.readouterr().out

    # A write that would break the model is refused, and nothing changes
    r = api.delete(f"/api/v1/roles/{PREFIX}Controller", params={"source": SOURCE})
    assert r.status_code == 422, r.text
    assert api.get(f"/api/v1/roles/{PREFIX}Controller").status_code == 200

    # The run history shows the connector's changeset
    runs = api.get("/api/v1/changesets", params={"source": SOURCE}).json()
    assert any(r["status"] == "committed" and r.get("connector") == "spreadsheet" for r in runs)
