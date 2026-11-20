"""The running daily tally counts exactly what reading the whole log counts."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

from veromesh import clock, decisions, followup

ACTIONS = ("Approve", "Pay")
ROLES = ("Controller", "ApManager")


def decision(log, rng) -> dict:
    outcome = rng.choice(("allow", "consult", "refuse"))
    consult = (
        [
            {"role": r, "roleName": r, "people": [], "why": ["material"]}
            for r in rng.sample(ROLES, rng.randint(1, 2))
        ]
        if outcome == "consult"
        else []
    )
    return log.append(
        {
            "kind": "decision",
            "subject": "agent-client",
            "agent": "Bot",
            "principal": "Dana",
            "action": rng.choice(ACTIONS),
            "outcome": outcome,
            "reasons": ["test"],
            "consult": consult,
            "basis": {},
        }
    )


def follow_up(log, rng, decided: list[dict]) -> None:
    d = rng.choice(decided)
    if rng.random() < 0.5:
        log.append(
            {
                "kind": "override",
                "refersTo": d["id"],
                "subject": "person",
                "outcome": rng.choice(followup.VERDICTS),
                "basis": {"role": rng.choice(ROLES), "by": "Lee"},
            }
        )
    else:
        log.append(
            {
                "kind": "outcome",
                "refersTo": d["id"],
                "subject": "agent-client",
                "outcome": rng.choice(followup.OUTCOMES),
            }
        )


def test_the_tally_matches_reading_the_log():
    rng = random.Random(7)
    log = decisions.make_log("memory")
    today = clock.today()
    decided: list[dict] = []
    for step in range(400):
        if not decided or rng.random() < 0.55:
            decided.append(decision(log, rng))
        else:
            follow_up(log, rng, decided)
        if step % 7 == 0 or step > 380:
            for action in ACTIONS:
                assert followup.done_today(log, action, today) == followup.done_today_by_reading(
                    log, action, today
                ), (step, action)


def test_a_new_day_starts_from_its_first_entry(monkeypatch):
    log = decisions.make_log("memory")
    rng = random.Random(3)
    for _ in range(20):
        decision(log, rng)
    yesterday = clock.today()
    tomorrow = yesterday + timedelta(days=1)
    assert followup.done_today(log, "Approve", tomorrow) == 0  # nothing recorded tomorrow yet
    # Entries recorded "tomorrow" (moved forward in time), then counted on tomorrow.
    real = datetime.now
    monkeypatch.setattr(
        decisions,
        "datetime",
        type("D", (), {"now": staticmethod(lambda tz=None: real(tz) + timedelta(days=1))}),
    )
    for _ in range(15):
        decision(log, rng)
    for action in ACTIONS:
        assert followup.done_today(log, action, tomorrow) == followup.done_today_by_reading(
            log, action, tomorrow
        )


def test_the_log_finds_where_a_day_starts():
    log = decisions.make_log("memory")
    a = log.append(
        {
            "kind": "decision",
            "subject": "s",
            "agent": "Bot",
            "action": "Pay",
            "outcome": "allow",
            "reasons": [],
            "basis": {},
        }
    )
    assert log.last_seq_before(datetime(2000, 1, 1, tzinfo=UTC)) == 0
    assert log.last_seq_before(datetime.now(UTC) + timedelta(seconds=1)) == a["seq"]
    assert isinstance(clock.start_of_day(date(2026, 11, 20)), datetime)
