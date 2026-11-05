"""Connectors are tested against the real VeroMesh API, in-process.

The store is in-memory, or the one named by VEROMESH_TEST_STORE (e.g.
fuseki:<dataset URL>, set by scripts/test.sh integration), emptied per test.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from veromesh.api import create_app
from veromesh.config import Settings
from veromesh.rdf_store import make_store
from veromesh.service import VeromeshService


class NoDataHub:
    """The model API never calls DataHub."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected DataHub call: {name}")


@pytest.fixture
def http() -> TestClient:
    spec = os.environ.get("VEROMESH_TEST_STORE", "memory")
    store = make_store(spec)
    if spec != "memory":
        store.update("DROP ALL")
    svc = VeromeshService(store, NoDataHub(), bindings_graph="https://veromesh.ai/graph/bindings")
    return TestClient(create_app(svc, Settings(rdf_store="memory")))
