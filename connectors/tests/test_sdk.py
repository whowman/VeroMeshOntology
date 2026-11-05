"""SDK core: transport retries, session auth, idempotent resume, version guard, event mode."""

from __future__ import annotations

import httpx
import pytest

from veromesh_connectors.core import (
    ApiSession,
    ClientCredentials,
    Event,
    EventConnector,
    Item,
    Record,
    Runner,
    RunOptions,
    StaticToken,
    Transport,
    TransportError,
)
from veromesh_connectors.core.contract import BatchConnector


class Roles(BatchConnector):
    name = "test-roles"

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def read(self, schema):
        return [Record(ref=f"row {n}", data={"id": r, "name": r}) for n, r in enumerate(self.rows, 1)]

    def map(self, record, schema):
        return [Item(type="roles", entity=dict(record.data), ref=record.ref)]


def session(http) -> ApiSession:
    return ApiSession(Transport("http://testserver", http=http, sleep=lambda s: None), StaticToken(None))


def test_transport_retries_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=req)
        if calls["n"] == 2:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    slept = []
    t = Transport(
        "http://x",
        http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)),
        sleep=slept.append,
    )
    assert t.request("GET", "/y").json() == {"ok": True}
    assert calls["n"] == 3 and slept[1] == 1.0


def test_transport_gives_up():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    t = Transport(
        "http://x",
        retries=2,
        http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    with pytest.raises(TransportError):
        t.request("GET", "/y")


def test_client_credentials_refresh_on_401():
    tokens = iter(["t1", "t2"])
    seen = []

    def handler(req):
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        seen.append(req.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={"detail": "x"} if len(seen) == 1 else [])

    t = Transport("http://x", http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)))
    api = ApiSession(t, ClientCredentials(t, "/token", "id", "secret"))
    assert api.sources() == []
    assert seen == ["Bearer t1", "Bearer t2"]


def test_rerun_of_unchanged_input_is_a_no_op_and_crash_resumes(http):
    api = session(http)
    out = []
    runner = Runner(api, out.append)
    assert runner.run(Roles(["A", "B"]), RunOptions(source="r")).ok
    version = api.source_version("r")

    again = runner.run(Roles(["A", "B"]), RunOptions(source="r"))
    assert again.ok and api.source_version("r") == version
    assert any("already up to date" in line for line in out)

    # Someone else edits the source; the same input must now be applied again.
    http.put("/api/v1/roles/Z", params={"source": "r"}, json={"name": "Z"}).raise_for_status()
    assert runner.run(Roles(["A", "B"]), RunOptions(source="r")).ok
    assert {r["id"] for r in http.get("/api/v1/roles", params={"source": "r"}).json()} == {"A", "B"}

    # A crashed run: changeset opened with the run's key, items sent, never committed.
    conn = Roles(["A", "B", "C"])
    schema = api.schema()
    items, _ = runner._map_all(conn, conn.read(schema), schema)
    key = f"{runner._content_key(conn, RunOptions(source='r'), items)}:v:{api.source_version('r')}"
    crashed = api.open_changeset(
        source="r", mode="replace", kind="connector", connector=conn.name, idempotency_key=key
    )
    api.add_items(crashed["id"], [i.wire() for i in items])
    resumed = runner.run(Roles(["A", "B", "C"]), RunOptions(source="r"))
    assert resumed.ok and resumed.changeset == crashed["id"]
    assert any("Resuming changeset" in line for line in out)


def test_concurrent_edit_is_refused_not_overwritten(http, monkeypatch):
    api = session(http)
    runner = Runner(api, lambda m: None)
    assert runner.run(Roles(["A"]), RunOptions(source="r")).ok
    real_validate = api.validate

    def validate_then_someone_else_edits(cs):
        report = real_validate(cs)
        http.put("/api/v1/roles/Z", params={"source": "r"}, json={"name": "Z"})
        return report

    monkeypatch.setattr(api, "validate", validate_then_someone_else_edits)
    result = runner.run(Roles(["A", "B"]), RunOptions(source="r"))
    assert result.ok is False
    assert {r["id"] for r in http.get("/api/v1/roles", params={"source": "r"}).json()} == {"A", "Z"}


def test_event_connector_applies_each_event_once(http):
    class Feed(EventConnector):
        name = "test-feed"

        def events(self, schema):
            yield Event(id="e1", records=[Record("msg e1", {"id": "A", "name": "A"})])
            yield Event(id="e2", records=[Record("msg e2", {"id": "B", "name": "B"})])
            yield Event(id="e1", records=[Record("msg e1 again", {"id": "A", "name": "A"})])  # redelivered
            yield Event(id="e3", deletes=[("roles", "B")])

        def map(self, record, schema):
            return [Item(type="roles", entity=dict(record.data), ref=record.ref)]

    out = []
    results = Runner(session(http), out.append).run_events(Feed(), RunOptions(source="feed"))
    assert all(r.ok for r in results)
    assert any("Already applied" in line for line in out)
    assert [r["id"] for r in http.get("/api/v1/roles", params={"source": "feed"}).json()] == ["A"]
