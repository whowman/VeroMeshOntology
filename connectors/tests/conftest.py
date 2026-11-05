"""The loader is tested against the real VeroMesh API, in-process, on an in-memory store."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from veromesh.api import create_app
from veromesh.config import Settings
from veromesh.rdf_store import MemoryStore
from veromesh.service import VeromeshService


class NoDataHub:
    """The model API never calls DataHub."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected DataHub call: {name}")


@pytest.fixture
def http() -> TestClient:
    svc = VeromeshService(MemoryStore(), NoDataHub(), bindings_graph="https://veromesh.ai/graph/bindings")
    return TestClient(create_app(svc, Settings(rdf_store="memory")))
