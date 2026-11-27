"""The MCP gateway (gap G3, option C): an agent's tool calls reach the system only once allowed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_auth import KEY, h, load_example, settings, token
from test_decide import attest_basis
from test_mcp import HEADERS, agent_app

from veromesh import auth, gateway
from veromesh import decisions as D
from veromesh.api import create_app
from veromesh.config import Settings

API = "/api/v1"
ERP = {
    "targets": [
        {
            "name": "erp",
            "url": "https://erp-mcp.example.com/mcp",
            "header": "Authorization",
            "secretEnv": "GATEWAY_ERP_TOKEN",
            "tools": {
                "approve_invoice": {
                    "action": "ApproveSupplierInvoice",
                    "amount": "amount",
                    "currencyValue": "USD",
                    "note": "invoice",
                }
            },
            "passThrough": ["get_invoice"],
        }
    ]
}


class FakeErp:
    """An upstream MCP server: what it offers, and every call it received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = False

    async def list_tools(self) -> list[dict]:
        obj = {"type": "object"}
        return [
            {"name": "approve_invoice", "description": "Approve an invoice.", "inputSchema": obj},
            {"name": "get_invoice", "description": "Read an invoice.", "inputSchema": obj},
            {"name": "delete_vendor", "description": "Not offered through the gateway.", "inputSchema": obj},
        ]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if self.fail:
            raise ConnectionError("ERP unreachable")
        self.calls.append((name, arguments))
        return gateway.text(f"{name} ok: {arguments.get('invoice')}")


@pytest.fixture
def gw(service, monkeypatch):
    """Sign-in off (development): the agent is named in a header."""
    monkeypatch.setenv("GATEWAY_ERP_TOKEN", "Bearer erp-secret")
    erp = FakeErp()
    log = D.MemoryLog()
    app = create_app(
        service, Settings(rdf_store="memory", auth_disabled=True, gateway_job=False), decision_log=log
    )
    with TestClient(app) as client:
        items = client.get(f"{API}/examples/procure-to-pay").json()["items"]
        cs = client.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
        client.post(f"{API}/changesets/{cs}/items", json={"items": items})
        assert client.post(f"{API}/changesets/{cs}/commit").status_code == 200
        attest_basis(client, service)
        assert client.put(f"{API}/gateway/settings", json=ERP).status_code == 200
        app.state.gateway.upstream = lambda target: erp
        yield client, log, erp


AGENT = {"X-VeroMesh-Agent": "InvoiceAssistant"}


def rpc(client, method, params=None, hdr=AGENT):
    r = client.post(
        "/gateway/mcp",
        headers={**HEADERS, **hdr},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def call(client, tool, hdr=AGENT, **arguments):
    res = rpc(client, "tools/call", {"name": tool, "arguments": arguments}, hdr)
    return " ".join(c.get("text", "") for c in res["content"]), bool(res.get("isError"))


def decision_of(log):
    return log.find(kind="decision")[-1]


def test_only_mapped_and_read_only_tools_are_offered(gw):
    client, _, _ = gw
    tools = {t["name"]: t for t in rpc(client, "tools/list")["tools"]}
    assert set(tools) == {"erp__approve_invoice", "erp__get_invoice", "gateway_status"}
    assert "checked by VeroMesh" in tools["erp__approve_invoice"]["description"]
    msg, err = call(client, "erp__delete_vendor")
    assert err and "no gateway tool" in msg


def test_an_allowed_call_goes_through_and_is_logged(gw):
    client, log, erp = gw
    msg, err = call(client, "erp__approve_invoice", amount=800, invoice="INV-1")
    assert not err and "approve_invoice ok: INV-1" in msg and "allowed" in msg
    assert erp.calls == [("approve_invoice", {"amount": 800, "invoice": "INV-1"})]
    d = decision_of(log)
    assert d["outcome"] == "allow" and d["amount"] == "800" and d["currency"] == "USD"
    assert "erp.approve_invoice: INV-1" in d["note"]
    out = log.find(kind="outcome", refers_to=d["id"])
    assert out[0]["outcome"] == "done" and "INV-1" in out[0]["note"] and log.verify().ok


def test_a_refused_call_never_reaches_the_system(gw):
    client, log, erp = gw
    msg, err = call(client, "erp__approve_invoice", amount=300_000, invoice="INV-2")
    assert err and "Refused by VeroMesh" in msg and "did not run" in msg
    assert erp.calls == [] and decision_of(log)["outcome"] == "refuse"


def test_a_held_call_waits_for_the_owner_then_goes_through(gw):
    client, log, erp = gw
    msg, err = call(client, "erp__approve_invoice", amount=3000, invoice="INV-3")
    assert not err and "Held by VeroMesh: Financial controller must approve" in msg
    d = decision_of(log)
    assert d["outcome"] == "consult" and d["id"] in msg and erp.calls == []
    msg, _ = call(client, "gateway_status", reference=d["id"])
    assert "Still held" in msg and "Controller" in msg and erp.calls == []
    r = client.post(
        f"{API}/decisions/{d['id']}/overrides", json={"verdict": "approved", "person": "LeeExample"}
    )
    assert r.status_code == 201 and r.json()["status"]["done"] == "done"  # sent with the answer
    assert erp.calls == [("approve_invoice", {"amount": 3000, "invoice": "INV-3"})]
    msg, err = call(client, "gateway_status", reference=d["id"])
    assert not err and "Done" in msg and "INV-3" in msg
    assert len(erp.calls) == 1 and client.post(f"{API}/gateway/run").json() == {"released": 0}
    assert log.verify().ok


def test_a_rejected_call_is_discarded(gw):
    client, log, erp = gw
    call(client, "erp__approve_invoice", amount=3000, invoice="INV-4")
    d = decision_of(log)
    client.post(f"{API}/decisions/{d['id']}/overrides", json={"verdict": "rejected", "person": "LeeExample"})
    msg, err = call(client, "gateway_status", reference=d["id"])
    assert err and "Rejected" in msg and erp.calls == []
    assert client.post(f"{API}/gateway/run").json() == {"released": 0}


def test_an_approval_the_service_missed_is_sent_by_the_sweep(gw):
    client, log, erp = gw
    call(client, "erp__approve_invoice", amount=3000, invoice="INV-5")
    d = decision_of(log)
    # Answered elsewhere (in Teams, or by another copy of the service): straight onto the log.
    log.append(
        {
            "kind": "override",
            "refersTo": d["id"],
            "subject": "lee",
            "outcome": "approved",
            "basis": {"role": "Controller", "person": "LeeExample"},
        }
    )
    assert client.post(f"{API}/gateway/run").json() == {"released": 1}
    assert erp.calls == [("approve_invoice", {"amount": 3000, "invoice": "INV-5"})]
    assert client.post(f"{API}/gateway/run").json() == {"released": 0}  # sent once


def test_a_failed_call_is_logged_as_not_done(gw):
    client, log, erp = gw
    erp.fail = True
    msg, err = call(client, "erp__approve_invoice", amount=800, invoice="INV-6")
    assert err and "ERP unreachable" in msg
    d = decision_of(log)
    assert log.find(kind="outcome", refers_to=d["id"])[0]["outcome"] == "not_done"


def test_read_only_tools_pass_through_unchecked(gw):
    client, log, erp = gw
    before = len(log.find(kind="decision"))
    msg, err = call(client, "erp__get_invoice", invoice="INV-1")
    assert not err and "get_invoice ok" in msg and len(log.find(kind="decision")) == before


def test_settings_are_checked_and_logged(gw):
    client, log, _ = gw
    bad = {"targets": [{**ERP["targets"][0], "passThrough": ["approve_invoice"]}]}
    assert client.put(f"{API}/gateway/settings", json=bad).status_code == 422
    bad = {"targets": [{"name": "erp", "url": "ftp://x"}]}
    assert client.put(f"{API}/gateway/settings", json=bad).status_code == 422
    bad = {"targets": [{"name": "erp", "url": "https://x", "header": "Authorization"}]}
    assert "secretEnv" in client.put(f"{API}/gateway/settings", json=bad).json()["detail"]
    assert client.get(f"{API}/gateway/settings").json()["targets"][0]["name"] == "erp"
    assert log.find(kind="configuration")[-1]["setting"] == "gateway"


def test_the_secret_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("GATEWAY_ERP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GATEWAY_ERP_TOKEN is not set"):
        gateway.upstream_for(ERP["targets"][0])
    monkeypatch.setenv("GATEWAY_ERP_TOKEN", "Bearer s")
    up = gateway.upstream_for(ERP["targets"][0])
    assert up.headers == {"Authorization": "Bearer s"} and up.url.endswith("/mcp")


# ---------------------------------------------------------------- signed in


@pytest.fixture
def signed_in(service, monkeypatch):

    admin = h(token(roles=("VeroMesh.Admin",)))

    erp = FakeErp()
    log = D.MemoryLog()
    app = create_app(service, settings(gateway_job=False), decision_log=log)
    app.state.verifier = auth.Verifier(settings(), key_for=lambda _t: KEY.public_key())
    with TestClient(app) as client:
        load_example(client)
        assert client.put(f"{API}/gateway/settings", json=ERP, headers=admin).status_code == 200
        app.state.gateway.upstream = lambda target: erp
        yield client, log, erp


def test_the_agent_is_its_sign_in(signed_in):
    client, log, erp = signed_in
    r = client.post("/gateway/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    msg, err = call(client, "erp__approve_invoice", hdr=agent_app(), amount=800, invoice="INV-7")
    d = decision_of(log)
    assert d["agent"] == "InvoiceAssistant" and "through the gateway" in d["note"]
    # Not attested in this install, so refused: the call never left VeroMesh.
    assert (d["outcome"] == "refuse") == err and (erp.calls == []) == err


# ---------------------------------------------------------------- a real MCP server upstream


def test_mcp_upstream_over_streamable_http():
    """McpUpstream against a real MCP server (FastMCP over Streamable HTTP, on a local port)."""
    import asyncio
    import socket
    import threading
    import time

    import uvicorn
    from mcp.server.fastmcp import FastMCP

    erp = FastMCP("erp", stateless_http=True, json_response=True)

    @erp.tool()
    def approve_invoice(invoice: str, amount: float) -> str:
        """Approve an invoice."""
        return f"approved {invoice} for {amount}"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(erp.streamable_http_app(), port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    try:
        up = gateway.McpUpstream(f"http://127.0.0.1:{port}/mcp", {"Authorization": "Bearer x"})
        tools = asyncio.run(up.list_tools())
        assert [t["name"] for t in tools] == ["approve_invoice"] and tools[0]["inputSchema"]["properties"]
        res = asyncio.run(up.call_tool("approve_invoice", {"invoice": "INV-9", "amount": 12}))
        assert not res["isError"] and "approved INV-9 for 12" in res["content"][0]["text"]
    finally:
        server.should_exit = True
        t.join(5)


def test_the_administration_page_lists_each_tool_and_how_it_is_offered(gw):
    client, _, _ = gw
    out = client.get(f"{API}/gateway/targets/erp/tools").json()
    by = {t["name"]: t for t in out["tools"]}
    assert out["problem"] is None
    assert (
        by["approve_invoice"]["offered"],
        by["get_invoice"]["offered"],
        by["delete_vendor"]["offered"],
    ) == (
        "checked",
        "read",
        None,
    )
    assert by["approve_invoice"]["mapping"]["action"] == "ApproveSupplierInvoice"
    assert client.get(f"{API}/gateway/targets/nope/tools").status_code == 404
    page = client.get("/admin/").text
    assert 'data-tab="gateway"' in page and "gw-save" in page


def test_an_administrator_names_the_agent_when_testing(signed_in):
    from test_auth import h, token

    client, log, _ = signed_in
    admin = h(token(roles=("VeroMesh.Admin",)))
    r = client.post(
        "/gateway/mcp", headers={**HEADERS, **admin}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert r.status_code == 422  # say which agent
    call(
        client,
        "erp__approve_invoice",
        hdr={**admin, "X-VeroMesh-Agent": "InvoiceAssistant"},
        amount=800,
        invoice="T-1",
    )
    assert decision_of(log)["agent"] == "InvoiceAssistant"
