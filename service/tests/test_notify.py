"""Notices to owners of attestations recorded for them in meetings (gap G9, part 2): sent
once by the first channel that reaches them, on the chain, with one reminder."""

from __future__ import annotations

from datetime import date

import pytest
from test_auth import h, token
from test_meetings import LEE, item, meeting, mtg  # noqa: F401 — the fixture

from veromesh import clock
from veromesh import notify as N

API = "/api/v1"
ADMIN = h(token(roles=("VeroMesh.Admin",)))


class Fake:
    """A channel that records what it was given, or fails as told."""

    def __init__(self, name: str, fail: Exception | None = None) -> None:
        self.name, self.fail, self.got = name, fail, []

    def send(self, notice: N.Notice) -> str:
        if self.fail:
            raise self.fail
        self.got.append(notice)
        return f"sent by {self.name}"


@pytest.fixture
def notified(mtg, monkeypatch):  # noqa: F811
    client, log = mtg
    monkeypatch.setattr(clock, "today", lambda: date(2026, 11, 26))
    client.get(f"{API}/notify/settings", headers=ADMIN)  # builds the notifier
    n = client.app.state.notifier
    mail = Fake("email")
    n.channels = {"email": mail}
    m = meeting(client)
    r = client.post(f"{API}/meetings/{m['id']}/attestations", json={"items": [item()]}, headers=LEE)
    assert r.status_code == 201, r.text
    return client, log, n, mail, m


def run(client) -> dict:
    r = client.post(f"{API}/notify/run", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_owner_is_told_once_and_it_is_on_the_chain(notified):
    client, log, _, mail, m = notified
    out = run(client)
    assert [(s["person"], s["notice"], s["channel"]) for s in out["sent"]] == [
        ("DanaExample", "recorded", "email")
    ]
    assert run(client)["sent"] == []  # once
    n = mail.got[0]
    assert (n.person_name, n.email, n.recorded_by_name, n.objection_until) == (
        "Dana Example",
        "dana@example.com",
        "Lee Example",
        "2026-12-03",
    )
    assert n.items[0].name == "On-time payment rate" and n.items[0].role_name == "Accounts payable manager"
    e = log.find(kind="notice")[0]
    assert (e["subject"], e["meeting"], e["channel"], e["notice"]) == (
        "DanaExample",
        m["id"],
        "email",
        "recorded",
    )
    assert e["attestations"] == n.attestations and log.verify().ok
    got = client.get(f"{API}/meetings/{m['id']}", headers=LEE).json()["recorded"]
    assert got[0]["notified"][0]["channel"] == "email"


def test_the_text_shows_values_or_names_and_never_personal_details(notified):
    client, _, n, mail, _ = notified
    run(client)
    subject, body = N.text(mail.got[0])
    assert "AP monthly review" in subject and "object by 3 December 2026" in body
    assert "On-time payment rate (KPI), as Accounts payable manager" in body
    assert "Name: On-time payment rate" in body
    client.put(f"{API}/notify/settings", json={"show": "names"}, headers=ADMIN)
    names = n.build(
        "recorded",
        mail.got[0].meeting["id"],
        "DanaExample",
        client.get(f"{API}/meetings/recorded?person=DanaExample", headers=LEE).json(),
    )
    assert all(value is None for it in names.items for _, value in it.fields)


def test_personal_fields_are_left_out(notified):
    _, _, n, _, m = notified
    rows = [
        {
            "type": "people",
            "id": "DanaExample",
            "name": "Dana Example",
            "role": "ApManager",
            "attestation": "a1",
            "fields": ["email", "department"],
            "recordedBy": "LeeExample",
            "objectionUntil": "2026-12-03",
            "at": "2026-11-26T10:00:00Z",
        }
    ]
    it = n.build("recorded", m["id"], "DanaExample", rows).items[0]
    assert [label for label, _ in it.fields] == ["Department"] and it.hidden == ["Email"]


def test_the_next_channel_is_used_when_the_first_cannot_reach_the_person(notified):
    client, log, n, mail, _ = notified
    n.channels = {"teams": Fake("teams", N.Unreachable("app not installed")), "email": mail}
    client.put(f"{API}/notify/settings", json={"channels": ["teams", "email"]}, headers=ADMIN)
    assert [s["channel"] for s in run(client)["sent"]] == ["email"]


def test_a_failure_is_shown_and_retried(notified):
    client, log, n, mail, m = notified
    n.channels = {"email": Fake("email", RuntimeError("smtp down"))}
    out = run(client)
    assert out["sent"] == [] and "smtp down" in out["notSent"][0]["problem"]
    assert not log.find(kind="notice")
    got = client.get(f"{API}/meetings/{m['id']}", headers=LEE).json()["recorded"][0]
    assert got["notified"] == [] and "smtp down" in got["notifyProblem"]
    n.channels = {"email": mail}
    assert len(run(client)["sent"]) == 1


def test_one_reminder_on_the_third_business_day_while_still_open(notified, monkeypatch):
    client, log, _, mail, _ = notified
    run(client)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 11, 30))  # Monday: business day 2
    assert run(client)["sent"] == []
    monkeypatch.setattr(clock, "today", lambda: date(2026, 12, 1))  # business day 3
    assert [s["notice"] for s in run(client)["sent"]] == ["reminder"]
    assert run(client)["sent"] == []
    assert "Reminder" in N.text(mail.got[-1])[0]


def test_no_reminder_once_the_owner_answered(notified, monkeypatch):
    client, _, _, _, _ = notified
    run(client)
    dana = h(token(roles=("VeroMesh.Reader",), email="dana@example.com"))
    r = client.post(
        f"{API}/kpis/OnTimePaymentRate/attestations",
        json={"verdict": "attested", "role": "ApManager"},
        headers=dana,
    )
    assert r.status_code == 201, r.text
    monkeypatch.setattr(clock, "today", lambda: date(2026, 12, 1))
    assert run(client)["sent"] == []


def test_settings_are_checked_and_logged(notified):
    client, log, *_ = notified
    for bad in ({"channels": ["fax"]}, {"show": "all"}, {"reminderBusinessDays": 5}, {"colour": "teal"}):
        assert client.put(f"{API}/notify/settings", json=bad, headers=ADMIN).status_code == 422, bad
    r = client.put(f"{API}/notify/settings", json={"reminderBusinessDays": 0}, headers=ADMIN)
    assert r.status_code == 200 and r.json() == {
        "channels": ["email"],
        "show": "values",
        "reminderBusinessDays": 0,
    }
    assert log.find(kind="configuration")[-1]["setting"] == "notify"
    assert client.get(f"{API}/notify/settings", headers=LEE).status_code == 403


def test_email_needs_an_address_and_a_mail_adapter():
    sent = []
    notice = N.Notice(
        "recorded",
        "P",
        "P",
        None,
        {"id": "m", "name": "M", "heldOn": "2026-11-26"},
        "L",
        "L",
        "2026-12-03",
        [],
        None,
    )
    off = N.EmailChannel(lambda: {"adapter": "off"}, None, send=lambda *a: sent.append(a))
    with pytest.raises(N.Unreachable):
        off.send(notice)
    notice.email = "p@example.com"
    with pytest.raises(N.Unreachable):
        off.send(notice)
    on = N.EmailChannel(
        lambda: {"adapter": "smtp", "to": "lic@veromesh.ai"}, None, send=lambda *a: sent.append(a) or "ok"
    )
    assert on.send(notice) == "ok" and sent[0][0]["to"] == "p@example.com"
