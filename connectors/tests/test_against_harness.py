"""The Workday and Entra ID connectors against the VeroMesh test harness (a simulated company).

Skipped unless the harness is installed:

    pip install "veromesh-test-harness @ git+https://github.com/whowman/VeroMeshTestHarness"

The harness serves mocks of Workday's and Microsoft Graph's public APIs over a
company whose truth it knows, with each system's quirks recorded as defects.
"""

from __future__ import annotations

import pytest

harness_truth = pytest.importorskip("harness.truth")
harness_app = pytest.importorskip("harness.app")

from fastapi.testclient import TestClient  # noqa: E402

from veromesh_connectors.cli import main  # noqa: E402

API = ["--api", "http://testserver"]
WORKDAY = ["workday", "import", "--url", "http://testserver/workday", "--tenant", "kvh"]
GRAPH = ["--tenant", "kvh", "--graph", "http://testserver/entra", "--login", "http://testserver/entra"]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    for k, v in {
        "WORKDAY_CLIENT_ID": "veromesh-connector",
        "WORKDAY_CLIENT_SECRET": "harness-secret",
        "WORKDAY_REFRESH_TOKEN": "harness-workday-refresh",
        "ENTRA_CLIENT_ID": "veromesh-connector",
        "ENTRA_CLIENT_SECRET": "harness-secret",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def company():
    truth = harness_truth.generate("healthcare", 300, 7)
    return truth, TestClient(harness_app.create_app(truth))


def load(http, h) -> None:
    assert main([*API, *WORKDAY], http=http, source_http=h) == 0
    assert (
        main([*API, "entra", "import", *GRAPH, "--role-group-prefix", "SG-"], http=http, source_http=h) == 0
    )


def test_everyone_working_is_loaded_and_agents_act_for_their_principals(http, company):
    truth, h = company
    load(http, h)
    people = {p["id"] for p in http.get("/api/v1/people").json()}
    employees = {p.employee_id for p in truth.active_people() if p.worker_type == "employee"}
    assert employees <= people
    agents = {a["id"]: a for a in http.get("/api/v1/agents").json()}
    for a in truth.agents.values():
        assert agents[a.id]["actsFor"] == truth.people[a.acts_for].employee_id


def test_a_month_of_changes_through_both_feeds(http, company, tmp_path):
    truth, h = company
    load(http, h)
    state = tmp_path / "entra.json"
    entra = [*API, "entra", "changes", *GRAPH, "--role-group-prefix", "SG-", "--state", str(state)]
    assert main(entra, http=http, source_http=h) == 0  # records the position
    start = truth.today
    h.post("/harness/clock", json={"days": 30})
    changes = [*API, "workday", "changes", "--url", "http://testserver/workday", "--tenant", "kvh"]
    assert main([*changes, "--since", start.isoformat()], http=http, source_http=h) == 0
    assert main(entra, http=http, source_http=h) == 0
    people = {p["id"] for p in http.get("/api/v1/people").json()}
    left = {p.employee_id for p in truth.people.values() if p.left and start < p.left <= truth.today}
    hired = {p.employee_id for p in truth.people.values() if start < p.hired <= truth.today}
    assert left and not (left & people)
    assert hired and hired <= people


def test_misbehaving_systems_give_the_same_result(http, company):
    truth, h = company
    h.put("/harness/chaos", json={"rate": 0.3, "seed": 1, "slow_ms": 20})
    load(http, h)
    seen = h.get("/harness/chaos").json()["seen"]
    assert seen["throttle"] + seen["server-error"] + seen["unavailable"] + seen["token-expired"] > 0
    calm = harness_truth.generate("healthcare", 300, 7)
    assert {p["id"] for p in http.get("/api/v1/people").json()} >= {
        p.employee_id for p in calm.active_people() if p.worker_type == "employee"
    }
