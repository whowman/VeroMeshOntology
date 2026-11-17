"""VeroMesh as an MCP server: /mcp, behind the same sign-in, with the same answers."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_auth import ASSISTANT, KEY, h, load_example, settings, token

from veromesh import auth
from veromesh import decisions as D
from veromesh.api import create_app

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def mcp(service):
    log = D.MemoryLog()
    app = create_app(service, settings(), decision_log=log)
    app.state.verifier = auth.Verifier(settings(), key_for=lambda _t: KEY.public_key())
    with TestClient(app) as client:  # runs the lifespan: the MCP session manager
        load_example(client)
        yield client, log


def rpc(client, hdr, method, params=None):
    r = client.post(
        "/mcp",
        headers={**HEADERS, **hdr},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    return r


def call(client, hdr, tool, **arguments):
    r = rpc(client, hdr, "tools/call", {"name": tool, "arguments": arguments})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"]
    return (json.loads(text) if not result.get("isError") else text), bool(result.get("isError"))


def agent_app() -> dict:
    """The invoice assistant signed in as itself (client credentials)."""
    return h(token(roles=("VeroMesh.Agent",), idtyp="app", scp=None, name=None, azp=ASSISTANT))


def through_agent(email: str) -> dict:
    """A person signed in through the invoice assistant's app registration (delegated, e.g. Copilot Studio)."""
    return h(token(roles=("VeroMesh.Reader",), email=email, azp=ASSISTANT))


def test_mcp_needs_sign_in(mcp):
    client, _ = mcp
    r = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    r = rpc(client, h(token(roles=("SomeOtherApp.User",))), "tools/list")
    assert r.status_code == 403


def test_tools_and_instructions(mcp):
    client, _ = mcp
    init = rpc(
        client,
        agent_app(),
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    ).json()["result"]
    assert "Before any consequential action" in init["instructions"]
    tools = {t["name"] for t in rpc(client, agent_app(), "tools/list").json()["result"]["tools"]}
    assert tools == {
        "who_am_i",
        "check_action",
        "decision_status",
        "report_outcome",
        "impact_of_action",
        "who_owns",
        "get_entity",
    }


def test_an_agent_signed_in_as_itself(mcp):
    client, log = mcp
    me, err = call(client, agent_app(), "who_am_i")
    assert not err and me["agent"]["id"] == "InvoiceAssistant" and me["actsFor"]["id"] == "DanaExample"
    d, err = call(
        client, agent_app(), "check_action", action="ApproveSupplierInvoice", amount="800", currency="USD"
    )
    assert not err and d["outcome"] in ("allow", "consult", "refuse") and d["subject"] == ASSISTANT
    assert log.get(d["id"])["agent"] == "InvoiceAssistant" and log.verify().ok


def test_a_person_through_their_agent(mcp):
    client, _ = mcp
    d, err = call(client, through_agent("dana@example.com"), "check_action", action="ApproveSupplierInvoice")
    assert not err and d["agent"] == "InvoiceAssistant" and d["principal"] == "DanaExample"
    me, _ = call(client, through_agent("dana@example.com"), "who_am_i")
    assert me["person"]["id"] == "DanaExample" and me["agent"]["id"] == "InvoiceAssistant"


def test_someone_else_cannot_use_a_persons_agent(mcp):
    client, log = mcp
    msg, err = call(client, through_agent("lee@example.com"), "check_action", action="ApproveSupplierInvoice")
    assert err and "acts for DanaExample" in msg
    assert log.verify().entries == 0


def test_a_person_without_an_agent_cannot_ask(mcp):
    client, _ = mcp
    msg, err = call(
        client, h(token(roles=("VeroMesh.Reader",), email="dana@example.com")), "check_action", action="X"
    )
    assert err and "asked by agents" in msg


def test_lookups_are_need_to_know(mcp):
    client, _ = mcp
    impact, _ = call(client, agent_app(), "impact_of_action", action="ApproveSupplierInvoice")
    by_kpi = {a["id"]: a for a in impact["affects"]}
    assert by_kpi["DuplicatePaymentRate"]["materialityThreshold"] == "1000"
    assert by_kpi["DuplicatePaymentRate"]["owners"][0]["people"] == [
        {"id": "LeeExample", "name": "Lee Example"}
    ]
    owns, _ = call(client, agent_app(), "who_owns", type="kpis", id="OnTimePaymentRate")
    assert owns["owners"][0]["role"] == "ApManager"
    person, _ = call(client, agent_app(), "get_entity", type="people", id="DanaExample")
    assert "email" not in person and "email" in person["withheld"]
    msg, err = call(client, agent_app(), "get_entity", type="nonsense", id="x")
    assert err


def test_with_sign_in_off_the_agent_is_named(service):
    """Local development: sign-in off, so the check names the agent, as the REST route does."""
    from veromesh.config import Settings

    log = D.MemoryLog()
    app = create_app(service, Settings(rdf_store="memory", auth_disabled=True), decision_log=log)
    with TestClient(app) as client:
        load_example(client)
        msg, err = call(client, {}, "check_action", action="ApproveSupplierInvoice")
        assert err and "Give the agent ID" in msg
        d, err = call(client, {}, "check_action", action="ApproveSupplierInvoice", agent="InvoiceAssistant")
        assert not err and d["agent"] == "InvoiceAssistant" and log.verify().entries == 1


def test_a_signed_in_agent_cannot_name_another(mcp):
    client, log = mcp
    msg, err = call(
        client, agent_app(), "check_action", action="ApproveSupplierInvoice", agent="SomeOtherAgent"
    )
    assert err and "not SomeOtherAgent" in msg
    assert log.verify().entries == 0
