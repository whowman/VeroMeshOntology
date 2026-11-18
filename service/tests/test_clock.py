"""The install's calendar: VEROMESH_TIME_ZONE decides which day a timestamp falls on."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from veromesh import clock, followup
from veromesh import provenance as prov

# 20:00 in New York on 17 November is 01:00 UTC on the 18th.
EVENING_NY = "2026-11-18T01:00:00+00:00"


@pytest.fixture(autouse=True)
def back_to_utc():
    yield
    clock.configure("UTC")


def test_default_is_utc():
    clock.configure(None)
    assert clock.zone_name() == "UTC"
    assert clock.local_date(EVENING_NY) == "2026-11-18"


def test_a_timestamp_falls_on_the_local_day():
    clock.configure("America/New_York")
    assert clock.local_date(EVENING_NY) == "2026-11-17"
    assert clock.local_date("2026-11-18T05:00:00Z") == "2026-11-18"
    assert clock.today() == datetime.now(clock._zone).date()


def test_end_of_day_is_local_midnight():
    clock.configure("America/New_York")
    assert clock.end_of_day(date(2026, 11, 17)) == datetime(2026, 11, 18, 4, 59, 59, tzinfo=UTC)
    clock.configure("UTC")
    assert clock.end_of_day(date(2026, 11, 17)) == datetime(2026, 11, 17, 23, 59, 59, tzinfo=UTC)


def test_an_unknown_zone_is_refused():
    with pytest.raises(ValueError, match="not a time zone"):
        clock.configure("Mars/Olympus")


def _entries():
    def d(seq, at, eid):
        return {
            "seq": seq,
            "id": eid,
            "kind": "decision",
            "action": "ApproveSupplierInvoice",
            "outcome": "allow",
            "recordedAt": at,
        }

    return [
        d(1, "2026-11-17T15:00:00+00:00", "a"),  # 10:00 New York, 17th
        d(2, EVENING_NY, "b"),  # 20:00 New York, 17th; already the 18th in UTC
        d(3, "2026-11-18T14:00:00+00:00", "c"),  # 09:00 New York, 18th
    ]


@pytest.fixture
def fake_log(monkeypatch):
    rows = _entries()

    def all_entries(log, *, action=None, kind=None, agent=None, after_seq=0):
        return [
            e
            for e in rows
            if e["seq"] > after_seq
            and (action is None or e.get("action") == action)
            and (kind is None or e["kind"] == kind)
        ]

    monkeypatch.setattr(followup, "all_entries", all_entries)
    return None


def test_per_day_count_follows_the_local_day(fake_log):
    clock.configure("America/New_York")
    assert followup.done_today(None, "ApproveSupplierInvoice", date(2026, 11, 17)) == 2
    assert followup.done_today(None, "ApproveSupplierInvoice", date(2026, 11, 18)) == 1
    clock.configure("UTC")
    assert followup.done_today(None, "ApproveSupplierInvoice", date(2026, 11, 17)) == 1
    assert followup.done_today(None, "ApproveSupplierInvoice", date(2026, 11, 18)) == 2


def test_measures_period_follows_the_local_day(fake_log):
    clock.configure("America/New_York")
    m = followup.measures(None, since="2026-11-17", until="2026-11-17")
    assert m["decisions"] == 2 and m["period"]["timeZone"] == "America/New_York"
    clock.configure("UTC")
    assert followup.measures(None, since="2026-11-17", until="2026-11-17")["decisions"] == 1


def test_as_of_a_date_means_the_end_of_the_local_day():
    clock.configure("America/New_York")
    assert prov._as_of_end("2026-11-17") == datetime(2026, 11, 18, 4, 59, 59, tzinfo=UTC)
