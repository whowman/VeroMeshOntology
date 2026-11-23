"""A started service builds the planner state in the background, so its first edit or
check does not; /health says how far it has got."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from test_auth import load_example, settings

from veromesh import incremental
from veromesh.api import create_app
from veromesh.modelcache import VersionedStore
from veromesh.service import VeromeshService

BASE = settings().instance_base


def restarted(service) -> VeromeshService:
    """The same model, as a freshly started copy of the service sees it: nothing held."""
    inner = service.store.inner if isinstance(service.store, VersionedStore) else service.store
    return VeromeshService(VersionedStore(inner), service.datahub, bindings_graph=service.bindings_graph)


def loaded(service) -> VeromeshService:
    app = create_app(service, settings(auth_disabled=True, oidc_issuer=None, warm_on_start=False))
    with TestClient(app) as client:
        load_example(client)
    return restarted(service)


def planner(client, want: str, timeout: float = 30) -> str:
    end = time.monotonic() + timeout
    while True:
        got = client.get("/health").json()["planner"]
        if got == want or time.monotonic() > end:
            return got
        time.sleep(0.05)


def test_the_planner_state_is_built_at_startup(service):
    fresh = loaded(service)
    assert fresh.store.held(("incremental", BASE)) is None
    built = incremental.STATS["built"]
    app = create_app(fresh, settings(auth_disabled=True, oidc_issuer=None))
    with TestClient(app) as client:
        assert planner(client, "ready") == "ready"
        held = fresh.store.held(("incremental", BASE))
        assert held is not None and held[0] == fresh.store.version()
        assert incremental.STATS["built"] == built + 1
        assert client.get("/health").json()["status"] == "ok"


def test_it_can_be_left_to_the_first_request(service):
    fresh = loaded(service)
    app = create_app(fresh, settings(auth_disabled=True, oidc_issuer=None, warm_on_start=False))
    with TestClient(app) as client:
        assert client.get("/health").json()["planner"] == "not started"
    assert fresh.store.held(("incremental", BASE)) is None


def test_a_failed_build_leaves_it_to_the_first_request(service, monkeypatch):
    fresh = loaded(service)

    def broken(_store, _base):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(incremental, "warm", broken)
    app = create_app(fresh, settings(auth_disabled=True, oidc_issuer=None))
    with TestClient(app) as client:
        assert planner(client, "not warmed") == "not warmed"
        assert client.get("/health").json()["status"] == "ok"
