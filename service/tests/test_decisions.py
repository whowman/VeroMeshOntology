"""The decision log: append-only, hash-chained, in memory and in PostgreSQL.

The PostgreSQL cases run when VEROMESH_TEST_DECISION_LOG is a postgresql:// URL of a
database the tests may create tables in (scripts/test.sh decision-log starts one).
"""

from __future__ import annotations

import json
import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from veromesh import decisions as D
from veromesh.api import create_app
from veromesh.config import Settings

PG = os.environ.get("VEROMESH_TEST_DECISION_LOG", "")


def fresh_pg() -> D.PostgresLog:
    """A clean log in its own schema, so runs never see each other's entries."""
    import pg8000.native

    log = D.PostgresLog(PG)
    schema = f"t_{uuid.uuid4().hex[:12]}"
    params = {k: v for k, v in log._params.items() if v is not None}
    pg8000.native.Connection(**params).run(f"CREATE SCHEMA {schema}")
    log._params["startup_params"] = {"search_path": schema}
    return log


BACKENDS = ["memory"] + (["postgresql"] if PG else [])


@pytest.fixture(params=BACKENDS)
def dlog(request) -> D.DecisionLog:
    return D.MemoryLog() if request.param == "memory" else fresh_pg()


def decision(outcome="allow", **kw) -> dict:
    return {
        "kind": "decision",
        "subject": "client-1",
        "agent": "InvoiceAssistant",
        "principal": "DanaExample",
        "action": "ApproveSupplierInvoice",
        "amount": "12500.00",
        "currency": "USD",
        "authority": "AssistantApprovalAuthority",
        "outcome": outcome,
        "reasons": ["within the delegated limit of 25,000 USD"],
        **kw,
    }


def test_entries_chain_and_verify(dlog):
    a = dlog.append(decision())
    b = dlog.append(decision("consult", consult=[{"role": "Controller"}]))
    assert (a["seq"], b["seq"]) == (1, 2)
    assert a["prevHash"] == D.GENESIS and b["prevHash"] == a["hash"]
    assert D.entry_hash(b) == b["hash"]
    assert dlog.verify().as_dict() == {"ok": True, "entries": 2, "lastHash": b["hash"]}


def test_overrides_and_outcomes_refer_to_a_decision(dlog):
    d = dlog.append(decision("consult"))
    o = dlog.append({"kind": "override", "refersTo": d["id"], "subject": "oid-dana", "outcome": "approved"})
    assert dlog.find(refers_to=d["id"]) == [o]
    with pytest.raises(D.InvalidEntry):
        dlog.append({"kind": "outcome", "refersTo": str(uuid.uuid4()), "subject": "x", "outcome": "done"})
    with pytest.raises(D.InvalidEntry):
        dlog.append({"kind": "override", "subject": "x", "outcome": "approved"})


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "decision", "subject": "x", "outcome": "maybe"},
        {"kind": "verdict", "subject": "x", "outcome": "allow"},
        {"kind": "decision", "outcome": "allow"},
        {**decision(), "hash": "forged"},
        {**decision(), "reasons": "a string"},
    ],
)
def test_invalid_entries_are_refused(dlog, bad):
    with pytest.raises(D.InvalidEntry):
        dlog.append(bad)
    assert dlog.verify().entries == 0


def test_amounts_are_hashed_as_strings(dlog):
    e = dlog.append(decision(amount=12500.5))
    assert e["amount"] == "12500.5"
    assert dlog.get(e["id"]) == e


def test_find_filters_and_pages(dlog):
    for i in range(5):
        dlog.append(decision(agent="A" if i % 2 else "B"))
    assert [e["seq"] for e in dlog.find(agent="A")] == [2, 4]
    assert [e["seq"] for e in dlog.find(after_seq=3, limit=1)] == [4]
    assert dlog.get("not-a-uuid") is None


# ---------------------------------------------------------------- tampering is detected


def test_a_changed_entry_breaks_the_chain():
    log = D.MemoryLog()
    for o in ("allow", "consult", "refuse"):
        log.append(decision(o))
    log._entries[1]["outcome"] = "allow"  # someone edits the record
    v = log.verify()
    assert not v.ok and v.broken_at == 2 and "do not match its hash" in v.problem


def test_a_removed_entry_breaks_the_chain():
    log = D.MemoryLog()
    for o in ("allow", "consult", "refuse"):
        log.append(decision(o))
    del log._entries[1]
    v = log.verify()
    assert not v.ok and v.broken_at == 3


def test_a_rewritten_and_rehashed_entry_breaks_the_next_link():
    log = D.MemoryLog()
    for o in ("allow", "consult", "refuse"):
        log.append(decision(o))
    e = log._entries[1]
    e["outcome"] = "allow"
    e["hash"] = D.entry_hash(e)  # a careful forger recomputes this entry's hash
    v = log.verify()
    assert not v.ok and v.broken_at == 3 and "does not follow" in v.problem


# ---------------------------------------------------------------- PostgreSQL only


@pytest.mark.skipif(not PG, reason="VEROMESH_TEST_DECISION_LOG not set")
def test_postgres_refuses_update_delete_and_truncate():
    import pg8000.native

    log = fresh_pg()
    log.append(decision())
    c = pg8000.native.Connection(**{k: v for k, v in log._params.items() if v is not None})
    for sql in (
        "UPDATE veromesh_decision_log SET kind = 'outcome'",
        "DELETE FROM veromesh_decision_log",
        "TRUNCATE veromesh_decision_log",
    ):
        with pytest.raises(Exception, match="append-only"):
            c.run(sql)
    assert log.verify().ok


@pytest.mark.skipif(not PG, reason="VEROMESH_TEST_DECISION_LOG not set")
def test_postgres_concurrent_writers_form_one_chain():
    schema_log = fresh_pg()
    schema_log.append(decision())  # creates the table in this schema
    writers = []
    for _ in range(4):
        w = D.PostgresLog(PG)
        w._params["startup_params"] = schema_log._params["startup_params"]
        writers.append(w)

    def write(w):
        for _ in range(10):
            w.append(decision())

    threads = [threading.Thread(target=write, args=(w,)) for w in writers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    v = schema_log.verify()
    assert v.ok and v.entries == 41


@pytest.mark.skipif(not PG, reason="VEROMESH_TEST_DECISION_LOG not set")
def test_postgres_survives_a_new_connection():
    log = fresh_pg()
    e = log.append(decision())
    again = D.PostgresLog(PG)
    again._params["startup_params"] = log._params["startup_params"]
    assert again.get(e["id"]) == e and again.verify().ok


# ---------------------------------------------------------------- the API


@pytest.fixture
def api(service):
    log = D.MemoryLog()
    return TestClient(
        create_app(service, Settings(rdf_store="memory", auth_disabled=True), decision_log=log)
    ), log


def test_api_lists_gets_and_verifies(api):
    client, log = api
    d = log.append(decision("consult"))
    o = log.append({"kind": "override", "refersTo": d["id"], "subject": "oid-dana", "outcome": "approved"})
    assert [
        e["id"] for e in client.get("/api/v1/decisions", params={"agent": "InvoiceAssistant"}).json()
    ] == [d["id"]]
    one = client.get(f"/api/v1/decisions/{d['id']}").json()
    assert one["outcome"] == "consult" and [e["id"] for e in one["followedBy"]] == [o["id"]]
    assert client.get("/api/v1/decisions/verify").json()["ok"] is True
    assert client.get(f"/api/v1/decisions/{uuid.uuid4()}").status_code == 404


def test_api_has_no_way_to_change_the_log(api):
    client, log = api
    d = log.append(decision())
    for method in ("put", "patch", "delete"):
        r = getattr(client, method)(f"/api/v1/decisions/{d['id']}", params={"source": "x"})
        assert 400 <= r.status_code < 500, r.text
    assert log.verify().ok and json.loads(json.dumps(log.get(d["id"]))) == d


def test_where_a_day_starts(dlog):
    from datetime import UTC, datetime, timedelta

    assert dlog.last_seq_before(datetime.now(UTC)) == 0
    first = dlog.append(decision())
    second = dlog.append(decision())
    assert dlog.last_seq_before(datetime(2000, 1, 1, tzinfo=UTC)) == 0
    assert dlog.last_seq_before(datetime.now(UTC) + timedelta(seconds=5)) == second["seq"]
    assert first["seq"] < second["seq"]


def test_the_daily_tally_on_each_log(dlog):
    from veromesh import clock, followup

    for outcome in ("allow", "allow", "refuse"):
        dlog.append(decision(outcome))
    today = clock.today()
    assert followup.done_today(dlog, "ApproveSupplierInvoice", today) == 2
    d = dlog.append(decision())
    dlog.append({"kind": "outcome", "refersTo": d["id"], "subject": "client-1", "outcome": "not_done"})
    assert followup.done_today(dlog, "ApproveSupplierInvoice", today) == 2
    assert followup.done_today_by_reading(dlog, "ApproveSupplierInvoice", today) == 2


def test_many_writers_at_once_each_get_their_own_answer(dlog):
    """Appends that arrive together (one group commit on PostgreSQL) each come back
    with their own entry, or their own refusal, and the chain still verifies."""
    import threading

    first = dlog.append(decision())
    results: dict[int, object] = {}

    def write(k: int) -> None:
        try:
            if k % 5 == 0:  # refers to nothing: refused, alone
                fields = {
                    "kind": "outcome",
                    "refersTo": str(uuid.uuid4()),
                    "subject": "client-1",
                    "outcome": "done",
                }
            elif k % 5 == 1:  # refers to a committed entry: fine
                fields = {
                    "kind": "outcome",
                    "refersTo": first["id"],
                    "subject": "client-1",
                    "outcome": "done",
                }
            else:
                fields = decision(note=f"writer {k}")
            results[k] = dlog.append(fields)
        except D.InvalidEntry as exc:
            results[k] = exc

    threads = [threading.Thread(target=write, args=(k,)) for k in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(isinstance(results[k], D.InvalidEntry) for k in range(0, 40, 5))
    written = [r for k, r in results.items() if k % 5]
    assert all(isinstance(r, dict) for r in written)
    assert len({r["seq"] for r in written}) == len(written)
    assert all(r.get("note") == f"writer {k}" for k, r in results.items() if k % 5 > 1)
    assert dlog.verify().ok


def test_any_text_is_stored_exactly(dlog):
    """Values are written as quoted SQL literals: text that looks like quoting or SQL
    comes back exactly as given, and the chain still verifies."""
    awkward = "it's $$ a $v0000$ test'); DROP TABLE veromesh_decision_log; --\n\\x00 é 🤖"
    e = dlog.append(decision(agent=awkward, principal=awkward, action=awkward, reasons=[awkward]))
    assert dlog.get(e["id"])["agent"] == awkward
    assert dlog.get(e["id"])["reasons"] == [awkward]
    assert dlog.find(agent=awkward)[0]["id"] == e["id"]
    assert dlog.verify().ok
