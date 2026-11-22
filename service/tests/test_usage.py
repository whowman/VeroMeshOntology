"""The usage report: counts, signed monthly reports chained month to month, settings,
mail adapters and the administrators' API."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_auth import client, h, load_example, token  # noqa: F401

from veromesh import changesets, usage
from veromesh import decisions as D
from veromesh.config import Settings

API = "/api/v1"
BASE = "urn:veromesh:org:"


def cfg(**kw) -> Settings:
    return Settings(**{"rdf_store": "memory", "auth_disabled": True, "install_id": "inst-1", **kw})


def counts(total_people: int, agents: int = 2, **more) -> dict:
    return {
        "identities": {
            "people": total_people,
            "agents": agents,
            "serviceAccounts": 0,
            "total": total_people + agents,
        },
        "peakAt": "2026-10-05T12:00:00+00:00",
        "entities": {"people": total_people, "agents": agents},
        "attestations": 3,
        "changesetsCommitted": 1,
        **more,
    }


def log_on(log: D.MemoryLog, when: str, kind: str = "decision", n: int = 1) -> None:
    """Entries dated ``when`` (the hash chain is not checked by these tests)."""
    for _ in range(n):
        fields = {
            "kind": kind,
            "subject": "s",
            "agent": "A",
            "action": "X",
            "outcome": "allow",
            "reasons": [],
        }
        if kind != "decision":
            fields = {
                "kind": kind,
                "subject": "s",
                "refersTo": log._entries[0]["id"],
                "outcome": "carried-out",
            }
        log.append(fields)
        log._entries[-1]["recordedAt"] = when  # the stored entry (append returns a copy)


@pytest.fixture
def u() -> usage.Usage:
    return usage.Usage(usage.MemoryUsage(), D.MemoryLog(), cfg(licensed_tier="small"))


# ---------------------------------------------------------------- counting


def test_identities_are_the_people_and_agents_in_the_model(service):
    from veromesh.api import create_app

    c = TestClient(create_app(service, cfg()))
    items = c.get(f"{API}/examples/procure-to-pay").json()["items"]
    cs = c.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    c.post(f"{API}/changesets/{cs}/items", json={"items": items})
    assert c.post(f"{API}/changesets/{cs}/commit").status_code == 200
    u = usage.Usage(usage.MemoryUsage(), D.MemoryLog(), cfg(), lambda: (service.store, BASE))
    got = u.take_count()
    people = len(changesets.list_entities(service.store, "people", BASE))
    agents = len(changesets.list_entities(service.store, "agents", BASE))
    assert got["identities"] == {
        "people": people,
        "agents": agents,
        "serviceAccounts": 0,
        "total": people + agents,
    }
    assert people and agents
    assert got["entities"]["people"] == people and got["changesetsCommitted"] >= 1


def test_a_day_keeps_its_highest_count():
    store = usage.MemoryUsage()
    store.put_day("2026-10-05", counts(100))
    store.put_day("2026-10-05", counts(80, attestations=9))
    day = store.days("2026-10-05", "2026-10-05")["2026-10-05"]
    assert day["identities"]["total"] == 102  # the peak stays
    assert day["attestations"] == 9  # the rest is the latest


# ---------------------------------------------------------------- reports


def test_a_month_report_counts_peak_month_end_share_and_everything_shown(u):
    u.store.put_day("2026-10-03", counts(4000))
    u.store.put_day("2026-10-20", counts(4500))
    u.store.put_day("2026-10-31", counts(4200))
    log_on(u.log, "2026-10-20T15:00:00+00:00", n=3)
    log_on(u.log, "2026-10-21T15:00:00+00:00", "outcome")
    log_on(u.log, "2026-11-02T15:00:00+00:00")  # the next month
    r = u.build("2026-10")
    assert r["identities"]["peak"] == {
        "people": 4500,
        "agents": 2,
        "serviceAccounts": 0,
        "total": 4502,
        "date": "2026-10-20",
    }
    assert (
        r["identities"]["monthEnd"]["date"] == "2026-10-31" and r["identities"]["monthEnd"]["total"] == 4202
    )
    assert r["identities"]["shareOfTier"] == "0.900"  # 4,502 of Small's 5,000
    assert r["charged"] == {"identitiesInScope": 4502}
    assert r["shown"]["decisionLog"] == {"decision": 3, "outcome": 1}
    assert r["shown"]["busiestDay"] == {"date": "2026-10-20", "decisions": 3}
    assert len(r["shown"]["days"]) == 31 and r["identities"]["daysCounted"] == 3
    assert r["tier"] == {"name": "Small", "identities": 5000}
    assert r["decisionLogHead"]["seq"] == 4  # the last entry before November


def test_the_month_boundary_follows_the_time_zone_set(u):
    log_on(u.log, "2026-11-01T03:00:00+00:00")  # 31 October, 23:00 in New York
    u.store.put_setting("time-zone", "America/New_York")
    assert u.build("2026-10")["shown"]["decisionLog"] == {"decision": 1}
    u.put_settings({"timeZone": "UTC"})
    assert u.build("2026-10")["shown"]["decisionLog"] == {}
    with pytest.raises(ValueError, match="not a time zone"):
        u.put_settings({"timeZone": "Mars/Olympus"})


def test_reports_are_made_once_a_month_has_ended_signed_and_chained(u):
    u.store.put_day("2026-09-30", counts(10))
    u.store.put_day("2026-10-15", counts(20))
    made = u.make_due(datetime(2026, 11, 2, tzinfo=UTC))
    assert made == ["2026-09", "2026-10"]
    assert u.make_due(datetime(2026, 11, 2, tzinfo=UTC)) == []  # made once
    sep, oct_ = u.store.report("2026-09"), u.store.report("2026-10")
    assert usage.verify(sep) == [] and usage.verify(oct_, previous=sep) == []
    assert oct_["report"]["previousReport"] == sep["hash"] and sep["report"]["previousReport"] is None
    assert u.store.add_report("2026-10", oct_) is False  # never replaced


def test_a_changed_report_or_a_missing_month_shows(u):
    u.store.put_day("2026-08-30", counts(10))
    u.make_due(datetime(2026, 11, 2, tzinfo=UTC))
    aug, sep, oct_ = (u.store.report(m) for m in ("2026-08", "2026-09", "2026-10"))
    changed = json.loads(json.dumps(oct_))
    changed["report"]["charged"]["identitiesInScope"] = 1
    assert any("changed after" in p for p in usage.verify(changed))
    rehashed = {**changed, "hash": usage.report_hash(changed["report"])}
    assert any("signature" in p for p in usage.verify(rehashed))  # a new hash needs the install's key
    assert any("missing" in p or "broken" in p for p in usage.verify(oct_, previous=aug))
    other = usage.Signer(usage.Signer.generate_pem())
    assert any("different key" in p for p in usage.verify(oct_, public_key=other.public_key))
    assert usage.verify(oct_, public_key=oct_["signature"]["publicKey"], previous=sep) == []


def test_the_signing_key_comes_from_the_setting_or_is_made_once():
    pem = usage.Signer.generate_pem()
    given = usage.Usage(usage.MemoryUsage(), D.MemoryLog(), cfg(usage_signing_key=pem))
    assert given.signer().public_key == usage.Signer(pem).public_key
    made = usage.Usage(usage.MemoryUsage(), D.MemoryLog(), cfg())
    assert made.signer().public_key == made.signer().public_key  # kept, not made again
    with pytest.raises(ValueError, match="Ed25519"):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
            .decode()
        )
        usage.Signer(rsa_pem)


def test_no_tier_no_share_and_a_wrong_tier_is_refused():
    u = usage.Usage(usage.MemoryUsage(), D.MemoryLog(), cfg())
    u.store.put_day("2026-10-03", counts(10))
    assert u.build("2026-10")["identities"]["shareOfTier"] is None
    with pytest.raises(ValueError, match="VEROMESH_LICENSED_TIER"):
        usage.Usage(usage.MemoryUsage(), D.MemoryLog(), cfg(licensed_tier="huge")).tier()


# ---------------------------------------------------------------- settings and mail


def test_mail_settings_are_checked_and_merged(u):
    assert (
        u.get_settings()["mail"]["adapter"] == "off"
        and u.get_settings()["mail"]["to"] == usage.DEFAULT_RECIPIENT
    )
    with pytest.raises(ValueError, match="mail.adapter"):
        u.put_settings({"mail": {"adapter": "pigeon"}})
    with pytest.raises(ValueError, match="smtp.host"):
        u.put_settings({"mail": {"adapter": "smtp", "from": "vm@example.com"}})
    s = u.put_settings(
        {"mail": {"adapter": "smtp", "from": "vm@example.com", "smtp": {"host": "mail.example.com"}}}
    )
    assert s["mail"]["smtp"] == {
        "host": "mail.example.com",
        "port": 587,
        "security": "starttls",
        "username": "",
    }
    s = u.put_settings({"mail": {"smtp": {"port": 25, "security": "none"}}})  # only what is given changes
    assert s["mail"]["smtp"]["host"] == "mail.example.com" and s["mail"]["smtp"]["port"] == 25
    assert s["secrets"] == {"smtpPassword": False, "m365ClientSecret": False}


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls, self.login_as = host, port, False, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_as = (user, password)

    def send_message(self, msg):
        FakeSMTP.sent.append((self, msg))
        return {}


def made_report(u) -> str:
    u.store.put_day("2026-10-03", counts(10))
    u.make_due(datetime(2026, 11, 2, tzinfo=UTC))
    return "2026-10"


def test_smtp_sends_the_signed_report_as_an_attachment(u, monkeypatch):
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    u.settings.usage_smtp_password = "s3cret"
    u.put_settings(
        {
            "mail": {
                "adapter": "smtp",
                "from": "vm@acme.example",
                "smtp": {"host": "mail.acme.example", "username": "vm"},
            }
        }
    )
    month = made_report(u)
    assert u.deliver_pending()[0]["ok"]
    server, msg = FakeSMTP.sent[0]
    assert server.tls and server.login_as == ("vm", "s3cret")
    assert msg["To"] == usage.DEFAULT_RECIPIENT and month in msg["Subject"]
    (att,) = list(msg.iter_attachments())
    assert usage.verify(json.loads(att.get_content())) == []
    assert u.deliver_pending() == []  # delivered: not sent again


def test_sendmail_hands_the_message_to_the_local_program(u, tmp_path):
    out = tmp_path / "got.eml"
    fake = tmp_path / "sendmail"
    fake.write_text(f"#!/bin/sh\ncat > {out}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    u.put_settings(
        {"mail": {"adapter": "sendmail", "from": "vm@acme.example", "sendmail": {"path": str(fake)}}}
    )
    month = made_report(u)
    got = u.send(month)
    assert got["ok"], got
    text = out.read_text()
    assert f"To: {usage.DEFAULT_RECIPIENT}" in text and f"veromesh-usage-inst-1-{month}.json" in text


def test_microsoft_365_signs_in_and_sends_as_the_mailbox():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if "oauth2" in str(req.url):
            assert b"client_credentials" in req.content and b"top-secret" in req.content
            return httpx.Response(200, json={"access_token": "tok"})
        assert req.headers["Authorization"] == "Bearer tok"
        body = json.loads(req.content)
        att = body["message"]["attachments"][0]
        assert (
            att["name"] == "r.json"
            and body["message"]["toRecipients"][0]["emailAddress"]["address"] == "to@x.example"
        )
        return httpx.Response(202)

    mail = usage.check_mail(
        {
            "adapter": "m365",
            "to": "to@x.example",
            "m365": {"tenantId": "t", "clientId": "c", "mailbox": "lic@acme.example"},
        }
    )
    s = cfg(usage_m365_client_secret="top-secret")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    detail = usage._send_m365(mail, s, "subject", "body", "r.json", b"{}", client=http)
    assert detail == "sent by Microsoft 365 as lic@acme.example"
    assert str(seen[1].url).endswith("/users/lic@acme.example/sendMail")


def test_a_failed_send_is_recorded_and_tried_again_an_hour_later(u, tmp_path):
    fake = tmp_path / "sendmail"
    fake.write_text("#!/bin/sh\necho 'no route' >&2\nexit 75\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    u.put_settings(
        {"mail": {"adapter": "sendmail", "from": "vm@acme.example", "sendmail": {"path": str(fake)}}}
    )
    made_report(u)
    first = u.deliver_pending()
    assert first and not first[0]["ok"] and "no route" in first[0]["detail"]
    assert u.deliver_pending() == []  # not again within the hour
    later = u.deliver_pending(datetime.now(UTC) + timedelta(hours=2))
    assert len(later) == 1 and len(u.store.deliveries("2026-10")) == 2


# ---------------------------------------------------------------- the API


def test_the_usage_api_is_for_administrators_only(client):  # noqa: F811
    load_example(client)
    for roles in (("VeroMesh.Steward",), ("VeroMesh.Reader",)):
        assert client.get(f"{API}/usage", headers=h(token(roles=roles))).status_code == 403
    admin = h(token(roles=("VeroMesh.Admin",)))
    r = client.get(f"{API}/usage", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["current"]["signed"] is False


def test_the_usage_api_counts_makes_and_serves_reports(service):
    from veromesh.api import create_app

    app = create_app(service, cfg(customer_name="Acme", licensed_tier="medium"))
    c = TestClient(app)
    items = c.get(f"{API}/examples/procure-to-pay").json()["items"]
    cs = c.post(f"{API}/changesets", json={"source": "p2p"}).json()["id"]
    c.post(f"{API}/changesets/{cs}/items", json={"items": items})
    c.post(f"{API}/changesets/{cs}/commit")
    run = c.post(f"{API}/usage/run").json()
    assert run["counted"]["identities"]["total"] > 0 and run["made"] == []  # this month has not ended
    u = app.state.usage
    u.store.put_day("2026-01-10", counts(5))  # an earlier month: its report is due
    assert "2026-01" in c.post(f"{API}/usage/run").json()["made"]
    listed = c.get(f"{API}/usage/reports").json()
    assert listed[0]["month"] == "2026-01"
    doc = c.get(f"{API}/usage/reports/2026-01?download=true")
    assert doc.headers["content-disposition"].startswith("attachment") and usage.verify(doc.json()) == []
    assert doc.json()["report"]["install"]["customer"] == "Acme"
    assert c.get(f"{API}/usage/reports/1999-01").status_code == 404
    assert c.put(f"{API}/usage/settings", json={"timeZone": "Nowhere/Land"}).status_code == 400
    assert (
        c.put(f"{API}/usage/settings", json={"timeZone": "Europe/London"}).json()["timeZone"]
        == "Europe/London"
    )
    assert c.post(f"{API}/usage/reports/2026-01/send").status_code == 400  # no adapter yet
    overview = c.get(f"{API}/usage").json()
    assert (
        overview["tier"] == {"name": "Medium", "identities": 25000}
        and overview["reports"][0]["delivery"] is None
    )


def test_the_command_line_verifies_a_report(u, tmp_path, capsys):
    made_report(u)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(u.store.report("2026-10")))
    assert usage.main(["verify", str(path)]) == 0
    doc = json.loads(path.read_text())
    doc["report"]["month"] = "2026-12"
    path.write_text(json.dumps(doc))
    assert usage.main(["verify", str(path)]) == 1
    assert "changed after" in capsys.readouterr().err


# ---------------------------------------------------------------- PostgreSQL


PG = os.environ.get("VEROMESH_TEST_DECISION_LOG", "")


@pytest.mark.skipif(not PG, reason="needs VEROMESH_TEST_DECISION_LOG (a throwaway PostgreSQL)")
def test_on_postgresql_reports_cannot_be_changed_or_removed():
    store = usage.PostgresUsage(PG)
    month = f"1990-{datetime.now(UTC).microsecond % 12 + 1:02d}"  # unlikely to exist already
    doc = usage.Signer(usage.Signer.generate_pem()).sign({"format": usage.FORMAT, "month": month})
    if store.report(month) is None:
        assert store.add_report(month, doc)
    assert store.add_report(month, doc) is False
    for sql in (
        "UPDATE veromesh_usage_report SET document = '{}' WHERE month = :m",
        "DELETE FROM veromesh_usage_report WHERE month = :m",
    ):
        with pytest.raises(Exception, match="append-only"):
            store._run(sql, m=month)
        store._conn.run("ROLLBACK") if store._conn else None
    store.put_day("1990-01-02", counts(7))
    store.put_day("1990-01-02", counts(3))
    assert store.days("1990-01-02", "1990-01-02")["1990-01-02"]["identities"]["total"] == 9
    with store.exclusive():
        with pytest.raises(usage._Busy):
            with usage.PostgresUsage(PG).exclusive():
                pass


@pytest.mark.skipif(not PG, reason="needs VEROMESH_TEST_DECISION_LOG (a throwaway PostgreSQL)")
def test_on_postgresql_the_decision_log_counts_by_day_and_kind():
    log = D.PostgresLog(PG)
    now = datetime.now(UTC)
    before = log.count_by_day(now - timedelta(hours=1), now + timedelta(hours=1), "UTC")
    log.append(
        {"kind": "decision", "subject": "s", "agent": "A", "action": "X", "outcome": "allow", "reasons": []}
    )
    after = log.count_by_day(now - timedelta(hours=1), now + timedelta(minutes=5), "UTC")
    day = datetime.now(UTC).date().isoformat()
    assert after[day]["decision"] == before.get(day, {}).get("decision", 0) + 1
