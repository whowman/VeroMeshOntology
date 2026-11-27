"""Telling consulted owners of decisions waiting for them, by email or a Teams card with
Approve and Reject, and a held gateway call going through when they approve there."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_decide import attest_basis
from test_teams import TENANT, activity, bot, install, post  # noqa: F401 — the bot fixture

from veromesh import decisions as D
from veromesh import notify as N
from veromesh.api import create_app
from veromesh.config import Settings

API = "/api/v1"


class Fake:
    """A channel that records the decision notices it was given."""

    name = "email"

    def __init__(self) -> None:
        self.got: list[N.DecisionNotice] = []

    def send(self, notice):
        return "meeting notice"

    def send_decision(self, notice: N.DecisionNotice) -> str:
        self.got.append(notice)
        return "sent"


@pytest.fixture
def owners(service):
    """Sign-in off, the facts behind the assistant's authority attested."""
    log = D.MemoryLog()
    app = create_app(
        service,
        Settings(rdf_store="memory", auth_disabled=True, notify_job=False, gateway_job=False),
        decision_log=log,
    )
    with TestClient(app) as client:  # runs the lifespan (the gateway's MCP sessions)
        yield from _load(client, service, app, log)


def _load(client, service, app, log):
    items = client.get(f"{API}/examples/procure-to-pay").json()["items"]
    cs = client.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert client.post(f"{API}/changesets/{cs}/commit").status_code == 200
    attest_basis(client, service)
    client.get(f"{API}/notify/settings")  # builds the notifier
    mail = Fake()
    app.state.notifier.channels = {"email": mail}
    yield client, log, mail


def check(client, amount, invoice="INV-1"):
    body = {"agent": "InvoiceAssistant", "action": "ApproveSupplierInvoice", "amount": amount}
    r = client.post(f"{API}/decisions/check", json={**body, "currency": "USD", "note": invoice})
    assert r.status_code == 201, r.text
    return r.json()


def run(client) -> dict:
    r = client.post(f"{API}/notify/run")
    assert r.status_code == 200, r.text
    return r.json()


def test_a_consulted_owner_is_told_once_and_it_is_on_the_chain(owners):
    client, log, mail = owners
    small = check(client, 800)
    d = check(client, 3000)
    assert (small["outcome"], d["outcome"]) == ("allow", "consult")
    out = run(client)
    assert [(s["person"], s["decision"], s["channel"]) for s in out["sent"]] == [
        ("LeeExample", d["id"], "email")
    ]
    assert run(client)["sent"] == []  # once
    n = mail.got[0]
    assert (n.person_name, n.role, n.role_name, n.principal_name) == (
        "Lee Example",
        "Controller",
        "Financial controller",
        "Dana Example",
    )
    assert n.email is None  # the example has no email for Lee: the email channel would pass on
    e = log.find(kind="notice", refers_to=d["id"])[0]
    assert (e["subject"], e["notice"], e["role"], e["channel"]) == (
        "LeeExample",
        "decision",
        "Controller",
        "email",
    )
    assert log.verify().ok


def test_the_email_says_what_the_agent_asks_and_why(owners):
    client, _, mail = owners
    check(client, 3000, invoice="INV-42")
    run(client)
    subject, body = N.decision_text(mail.got[0])
    assert subject.startswith("Approval needed: ") and "3,000 USD" in subject and "Dana Example" in subject
    assert "INV-42" in body and "Financial controller" in body and "1,000 USD per invoice" in body
    assert "dana@example.com" not in body  # no personal details


def test_an_answered_decision_is_not_notified(owners):
    client, _, mail = owners
    d = check(client, 3000)
    r = client.post(
        f"{API}/decisions/{d['id']}/overrides", json={"verdict": "approved", "person": "LeeExample"}
    )
    assert r.status_code == 201
    assert run(client)["sent"] == [] and mail.got == []


def test_decision_notices_can_be_turned_off(owners):
    client, _, mail = owners
    assert client.put(f"{API}/notify/settings", json={"decisions": False}).json()["decisions"] is False
    check(client, 3000)
    assert run(client)["sent"] == [] and mail.got == []
    assert client.put(f"{API}/notify/settings", json={"decisions": "yes"}).status_code == 422


def test_a_held_gateway_call_is_named_in_the_notice(owners, monkeypatch):
    from test_gateway import ERP, FakeErp

    client, log, mail = owners
    monkeypatch.setenv("GATEWAY_ERP_TOKEN", "Bearer x")
    assert client.put(f"{API}/gateway/settings", json=ERP).status_code == 200
    erp = FakeErp()
    client.app.state.gateway.upstream = lambda target: erp
    r = client.post(
        "/gateway/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "X-VeroMesh-Agent": "InvoiceAssistant",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "erp__approve_invoice", "arguments": {"amount": 3000, "invoice": "INV-9"}},
        },
    )
    assert "Held by VeroMesh" in r.json()["result"]["content"][0]["text"]
    run(client)
    n = mail.got[0]
    assert n.held_call == "erp.approve_invoice"
    assert "held by VeroMesh: it goes to the system only if you approve" in N.decision_text(n)[1]


# ---------------------------------------------------------------- Teams


def consult_entry(log) -> dict:
    """A decision consulting the Controller (Lee), as the check writes it."""
    return log.append(
        {
            "kind": "decision",
            "outcome": "consult",
            "subject": "invoice-assistant",
            "agent": "InvoiceAssistant",
            "principal": "DanaExample",
            "action": "ApproveSupplierInvoice",
            "amount": "3000",
            "currency": "USD",
            "note": "INV-7",
            "reasons": ["Consult Financial controller."],
            "consult": [
                {
                    "role": "Controller",
                    "roleName": "Financial controller",
                    "people": [{"id": "LeeExample", "name": "Lee Example"}],
                    "why": ["3,000 USD against a threshold of 1,000 USD per invoice"],
                }
            ],
        }
    )


def press(client, verb, decision_id, oid="oid-lee", note=None):
    data = {"decision": decision_id, "role": "Controller", **({"note": note} if note else {})}
    act = activity(
        oid,
        type="invoke",
        name="adaptiveCard/action",
        value={"action": {"type": "Action.Execute", "verb": verb, "data": data}},
    )
    r = post(client, act)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_card_has_approve_and_reject_and_approve_answers_as_the_owner(bot):  # noqa: F811
    client, log, n, teams, ms = bot
    install(client, "oid-lee")
    d = consult_entry(log)
    released = []
    teams.after_answer = released.append
    out = n.run()
    assert [(s["person"], s["channel"]) for s in out["sent"]] == [("LeeExample", "teams")]
    card = ms.sent()[-1]["attachments"][0]["content"]
    assert [a["verb"] for a in card["actions"] if a["type"] == "Action.Execute"] == ["approve", "reject"]
    facts = {f["title"]: f["value"] for f in card["body"][1]["facts"]}
    assert facts["Amount"] == "3,000 USD" and facts["About"] == "INV-7"
    res = press(client, "approve", d["id"], note="PO matches")
    assert res["statusCode"] == 200 and "Approved by Lee Example" in str(res["value"])
    assert "approve" not in [a.get("verb") for a in res["value"].get("actions", [])]
    o = log.find(kind="override", refers_to=d["id"])[0]
    assert (o["outcome"], o["basis"]["by"], o["basis"]["role"]) == ("approved", "LeeExample", "Controller")
    assert "PO matches" in o["note"] and "in Teams" in o["note"] and released == [d["id"]]
    assert press(client, "approve", d["id"])["statusCode"] == 200  # pressed again: just the card
    assert len(log.find(kind="override", refers_to=d["id"])) == 1


def test_someone_not_consulted_cannot_answer_in_teams(bot):  # noqa: F811
    client, log, n, _, _ = bot
    install(client, "oid-lee")
    install(client, "oid-dana")
    d = consult_entry(log)
    n.run()
    res = press(client, "reject", d["id"], oid="oid-dana")
    assert res["statusCode"] == 403 and log.find(kind="override", refers_to=d["id"]) == []


def test_a_card_answered_elsewhere_is_replaced(bot):  # noqa: F811
    client, log, n, teams, ms = bot
    install(client, "oid-lee")
    d = consult_entry(log)
    n.run()
    log.append(
        {
            "kind": "override",
            "refersTo": d["id"],
            "subject": "lee",
            "outcome": "rejected",
            "basis": {"by": "LeeExample", "role": "Controller"},
        }
    )
    n.run()
    put = ms.sent("PUT")[-1]["attachments"][0]["content"]
    assert (
        "Rejected by Lee Example" in str(put)
        and "actions" not in put
        or all(a["type"] != "Action.Execute" for a in put.get("actions", []))
    )
