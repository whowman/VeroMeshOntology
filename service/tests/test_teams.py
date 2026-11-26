"""The Teams channel (gap G9, part 2): the bot learns who is who when it is installed,
sends the card, and turns Confirm and Object presses into the owner's own attestations.
No Azure: Microsoft's side is a fake (httpx.MockTransport) and the Bot Framework tokens
are signed with a test key."""

from __future__ import annotations

import json
import time
from datetime import date

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from test_auth import KEY, h, load_example, settings, token
from test_meetings import LEE, item

from veromesh import auth, clock
from veromesh import decisions as D
from veromesh import teams as T
from veromesh.api import create_app

API = "/api/v1"
ADMIN = h(token(roles=("VeroMesh.Admin",)))
APP_ID, TENANT, SERVICE = "bot-app", "tenant-1", "https://smba.example/teams/"
EMAILS = {"oid-dana": "Dana@Example.com", "oid-lee": "lee@example.com", "oid-kim": "kim@example.com"}


def bot_token(**over) -> str:
    now = int(time.time())
    claims = {"iss": T.BOT_ISSUER, "aud": APP_ID, "iat": now, "exp": now + 600, "serviceurl": SERVICE, **over}
    return jwt.encode(claims, KEY, algorithm="RS256")


class Microsoft:
    """Microsoft's side: the token endpoint and the Bot Connector."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content.startswith(b"{") else None
        self.calls.append((req.method, str(req.url), body))
        if "oauth2/v2.0/token" in str(req.url):
            return httpx.Response(200, json={"access_token": "bot-token", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer bot-token"
        if "/members/" in str(req.url):
            oid = str(req.url).rsplit("/", 1)[1]
            return httpx.Response(
                200, json={"id": "29:" + oid, "email": EMAILS[oid], "userPrincipalName": EMAILS[oid]}
            )
        if req.method == "POST":
            self.n += 1
            return httpx.Response(201, json={"id": f"act-{self.n}"})
        return httpx.Response(200, json={})

    def sent(self, method="POST") -> list[dict]:
        return [b for m, u, b in self.calls if m == method and "/activities" in u]


@pytest.fixture
def bot(service, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 11, 26))
    log = D.MemoryLog()
    cfg = settings(teams_app_id=APP_ID, teams_tenant_id=TENANT, teams_app_password="secret")
    app = create_app(service, cfg, decision_log=log)
    app.state.verifier = auth.Verifier(cfg, key_for=lambda _t: KEY.public_key())
    client = TestClient(app)
    load_example(client)
    lee = {
        "type": "people",
        "op": "upsert",
        "entity": {"id": "LeeExample", "name": "Lee Example", "email": "lee@example.com"},
    }
    cs = client.post(f"{API}/changesets", json={"source": "hr"}, headers=ADMIN).json()["id"]
    client.post(f"{API}/changesets/{cs}/items", json={"items": [lee]}, headers=ADMIN)
    client.post(f"{API}/changesets/{cs}/commit", headers=ADMIN)
    client.put(f"{API}/notify/settings", json={"channels": ["teams", "email"]}, headers=ADMIN)
    n = app.state.notifier
    ms = Microsoft()
    teams = n.channels["teams"]
    teams.http = httpx.Client(transport=httpx.MockTransport(ms))
    teams._key_for = lambda _t: KEY.public_key()
    return client, log, n, teams, ms


def activity(oid="oid-dana", **more) -> dict:
    return {
        "serviceUrl": SERVICE,
        "from": {"id": "29:" + oid, "aadObjectId": oid},
        "recipient": {"id": "28:bot"},
        "conversation": {"id": "a:conv-" + oid, "conversationType": "personal", "tenantId": TENANT},
        "channelData": {"tenant": {"id": TENANT}},
        **more,
    }


def post(client, act: dict, tok: str | None = None):
    return client.post(
        f"{API}/teams/messages", json=act, headers={"Authorization": f"Bearer {tok or bot_token()}"}
    )


def install(client, oid="oid-dana"):
    r = post(client, activity(oid, type="installationUpdate", action="add"))
    assert r.status_code == 200, r.text


def press(client, verb, meeting_id, attestation=None, oid="oid-dana", note=None):
    data = {
        "meeting": meeting_id,
        **({"attestation": attestation} if attestation else {}),
        **({"note": note} if note else {}),
    }
    act = activity(
        oid,
        type="invoke",
        name="adaptiveCard/action",
        value={"action": {"type": "Action.Execute", "verb": verb, "data": data}},
    )
    r = post(client, act)
    assert r.status_code == 200, r.text
    return r.json()


def record(client, *items) -> dict:
    m = client.post(
        f"{API}/meetings", json={"name": "AP monthly review", "heldOn": "2026-11-26"}, headers=LEE
    ).json()
    r = client.post(f"{API}/meetings/{m['id']}/attestations", json={"items": list(items)}, headers=LEE)
    assert r.status_code == 201, r.text
    return m


def standing(client, m) -> dict:
    return {
        r["id"]: r["standing"]
        for r in client.get(f"{API}/meetings/{m['id']}", headers=LEE).json()["recorded"]
    }


def test_install_matches_the_teams_user_to_a_person(bot):
    client, _, _, teams, ms = bot
    install(client)
    assert teams.people()["DanaExample"]["conversationId"] == "a:conv-oid-dana"
    assert "will send you here" in ms.sent()[0]["text"]
    install(client, "oid-kim")  # nobody in the model
    assert "could not match" in ms.sent()[-1]["text"] and set(teams.people()) == {"DanaExample"}
    post(client, activity(type="installationUpdate", action="remove"))
    assert teams.people() == {}


def test_requests_must_come_from_the_bot_framework_for_this_bot_and_tenant(bot):
    client, *_ = bot
    act = activity(type="installationUpdate", action="add")
    assert post(client, act, bot_token(aud="another-bot")).status_code == 401
    assert post(client, act, bot_token(iss="https://evil.example")).status_code == 401
    assert post(client, act, bot_token(serviceurl="https://elsewhere.example/")).status_code == 401
    other = {**act, "channelData": {"tenant": {"id": "tenant-2"}}}
    assert post(client, other).status_code == 401
    assert client.post(f"{API}/teams/messages", json=act).status_code == 401


def test_the_card_goes_to_the_owner_and_confirm_is_their_attestation(bot):
    client, log, _, _, ms = bot
    install(client)
    m = record(client, item(), item(type="capabilities", id="AccountsPayable"))
    out = client.post(f"{API}/notify/run", headers=ADMIN).json()
    assert [s["channel"] for s in out["sent"]] == ["teams"]
    card = ms.sent()[-1]["attachments"][0]["content"]
    assert card["version"] == "1.4" and "AP monthly review" in card["body"][1]["text"]
    verbs = [
        a["verb"]
        for c in card["body"]
        if c["type"] == "Container"
        for x in c["items"]
        if x["type"] == "ActionSet"
        for a in x["actions"]
    ]
    assert verbs == ["confirm", "object", "confirm", "object"]
    assert card["actions"][0]["verb"] == "confirmAll"
    att = next(
        r["attestation"]
        for r in client.get(f"{API}/meetings/{m['id']}", headers=LEE).json()["recorded"]
        if r["id"] == "OnTimePaymentRate"
    )
    res = press(client, "confirm", m["id"], att)
    assert res["statusCode"] == 200 and res["type"] == "application/vnd.microsoft.card.adaptive"
    assert standing(client, m) == {"OnTimePaymentRate": "confirmed", "AccountsPayable": "open"}
    e = log.find(kind="attestation")[-1]
    assert (e["subject"], e["outcome"], e.get("recordedBy")) == ("DanaExample", "attested", None)
    assert "in Teams" in e["note"]
    # The confirmed fact has no buttons any more.
    texts = json.dumps(res["value"])
    assert "Confirmed by you" in texts and texts.count('"verb": "confirm"') == 1


def test_object_with_a_note_and_confirm_all(bot):
    client, log, *_ = bot
    install(client)
    m = record(client, item(), item(type="capabilities", id="AccountsPayable"))
    client.post(f"{API}/notify/run", headers=ADMIN)
    rows = client.get(f"{API}/meetings/{m['id']}", headers=LEE).json()["recorded"]
    att = next(r["attestation"] for r in rows if r["id"] == "AccountsPayable")
    press(client, "object", m["id"], att, note="We split this capability last month")
    assert log.find(kind="attestation")[-1]["note"].startswith("We split this capability")
    press(client, "confirmAll", m["id"])
    assert standing(client, m) == {"OnTimePaymentRate": "confirmed", "AccountsPayable": "disputed"}


def test_only_the_owner_can_answer(bot):
    client, *_ = bot
    install(client)
    install(client, "oid-lee")
    m = record(client, item())
    att = client.get(f"{API}/meetings/{m['id']}", headers=LEE).json()["recorded"][0]["attestation"]
    res = press(client, "confirm", m["id"], att, oid="oid-lee")
    assert res["statusCode"] == 404
    assert standing(client, m) == {"OnTimePaymentRate": "open"}


def test_without_the_app_installed_the_notice_goes_by_email(bot):
    client, _, n, _, ms = bot
    got = []
    n.channels["email"].send = lambda notice: got.append(notice) or "mailed"
    record(client, item())
    out = client.post(f"{API}/notify/run", headers=ADMIN).json()
    assert [s["channel"] for s in out["sent"]] == ["email"] and got and not ms.sent()


def test_the_card_is_replaced_when_the_window_closes(bot, monkeypatch):
    client, _, _, teams, ms = bot
    install(client)
    record(client, item())
    client.post(f"{API}/notify/run", headers=ADMIN)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 12, 4))
    client.post(f"{API}/notify/run", headers=ADMIN)
    replaced = ms.sent("PUT")
    assert replaced and "this stands" in json.dumps(replaced[-1])
    client.post(f"{API}/notify/run", headers=ADMIN)
    assert len(ms.sent("PUT")) == len(replaced)  # once


def test_without_teams_settings_the_endpoint_is_not_there(service):
    client = TestClient(create_app(service, settings(), decision_log=D.MemoryLog()))
    assert client.post(f"{API}/teams/messages", json=activity(type="message")).status_code == 404
