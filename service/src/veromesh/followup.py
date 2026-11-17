"""What happens after a decision: people's answers and overrides, the agent's outcome, and measures.

All of it is appended to the decision log, never changed, each entry referring to its decision:

* **override** (a person): ``approved`` or ``rejected``.
    - On *consult*: someone filling one of the consulted roles answers. The decision is
      approved once every consulted role has approved; rejected as soon as one rejects.
    - On *refuse*: the principal (or an administrator) may approve, taking responsibility
      for an action above the agent's authority; the principal can also extend the delegation.
    - On *allow*: the principal, an informed owner or an administrator may reject (stop it).
* **outcome** (the agent): ``done`` or ``not_done``: what it actually did. Allowed once the
  decision stands as allowed or approved.

Measures (working definitions, to confirm against the white paper):

* **override rate**: decisions where a person overruled the check (an allow rejected, or a
  refusal approved) over all decisions.
* **consultation precision**: of the consultations answered, the share the owner rejected.
  Low means owners are asked about things they always approve (thresholds may be too low);
  high means consulting them matters.
* **delegation extended**: the share of decisions the agent could take within its own
  delegation (allowed outright), with escalations above the limit that the principal
  approved: each is a candidate for extending the delegation.
"""

from __future__ import annotations

from datetime import date

from .decisions import DecisionLog, InvalidEntry

VERDICTS = ("approved", "rejected")
OUTCOMES = ("done", "not_done")


def followups(log: DecisionLog, decision_id: str) -> list[dict]:
    out, after = [], 0
    while True:
        page = log.find(refers_to=decision_id, after_seq=after, limit=1000)
        if not page:
            return out
        out += page
        after = page[-1]["seq"]


def status_of(decision: dict, later: list[dict]) -> dict:
    """Where a decision stands: pending | allowed | approved | rejected | refused, and what was done."""
    overrides = [e for e in later if e["kind"] == "override"]
    outcomes = [e for e in later if e["kind"] == "outcome"]
    rejected = [o for o in overrides if o["outcome"] == "rejected"]
    approved = [o for o in overrides if o["outcome"] == "approved"]
    kind = decision["outcome"]
    if rejected:
        state = "rejected"
    elif kind == "allow":
        state = "allowed"
    elif kind == "consult":
        needed = {c["role"] for c in decision.get("consult", [])}
        answered = {(o.get("basis") or {}).get("role") for o in approved}
        state = "approved" if needed and needed <= answered else "pending"
        pending_roles = sorted(needed - answered)
    else:  # refuse
        state = "approved" if approved else "refused"
    out = {
        "decision": decision["id"],
        "outcome": kind,
        "status": state,
        "mayAct": state in ("allowed", "approved"),
        "overrides": [
            {
                "verdict": o["outcome"],
                "by": (o.get("basis") or {}).get("by"),
                "role": (o.get("basis") or {}).get("role"),
                "note": o.get("note"),
                "at": o["recordedAt"],
            }
            for o in overrides
        ],
        "done": outcomes[-1]["outcome"] if outcomes else None,
    }
    if kind == "consult" and state == "pending":
        out["waitingFor"] = pending_roles
    return out


def override(
    log: DecisionLog,
    decision: dict,
    *,
    person: str,
    person_roles: list[str],
    is_admin: bool,
    verdict: str,
    subject: str,
    role: str | None = None,
    note: str | None = None,
) -> dict:
    """A person answers or overrules a decision. Returns the new log entry and the decision's status."""
    if verdict not in VERDICTS:
        raise InvalidEntry(f"verdict is one of {', '.join(VERDICTS)}.")
    if decision["kind"] != "decision":
        raise InvalidEntry("Only a decision can be answered or overridden.")
    kind = decision["outcome"]
    principal = decision.get("principal")
    consulted = [c["role"] for c in decision.get("consult", [])]
    informed = [i["role"] for i in (decision.get("basis") or {}).get("inform", [])]
    acting_as = None
    if kind == "consult":
        mine = [r for r in consulted if r in person_roles]
        if role:
            if role not in mine:
                raise PermissionError(
                    f"You do not fill {role}, or it was not consulted ({', '.join(consulted)})."
                )
            acting_as = role
        elif mine:
            acting_as = mine[0]
        elif not is_admin:
            raise PermissionError(f"Only the consulted roles may answer: {', '.join(consulted)}.")
    elif kind == "refuse":
        if verdict != "approved":
            raise InvalidEntry("A refusal stands without anyone confirming it; it can only be approved.")
        if person != principal and not is_admin:
            raise PermissionError(
                f"Only {principal}, whom the agent acts for, may approve above its authority."
            )
    else:  # allow
        if verdict != "rejected":
            raise InvalidEntry("An allowed decision needs no approval; it can only be rejected (stopped).")
        if person != principal and not is_admin and not any(r in person_roles for r in informed):
            raise PermissionError("Only the principal or an informed owner may stop an allowed action.")
    entry = log.append(
        {
            "kind": "override",
            "refersTo": decision["id"],
            "subject": subject,
            "agent": decision.get("agent"),
            "principal": principal,
            "action": decision.get("action"),
            "outcome": verdict,
            "basis": {"by": person, "role": acting_as},
            "note": note,
        }
    )
    return {"entry": entry, "status": status_of(decision, followups(log, decision["id"]))}


def report_outcome(
    log: DecisionLog, decision: dict, *, outcome: str, subject: str, note: str | None = None
) -> dict:
    """The agent says what it did."""
    if outcome not in OUTCOMES:
        raise InvalidEntry(f"outcome is one of {', '.join(OUTCOMES)}.")
    st = status_of(decision, followups(log, decision["id"]))
    if outcome == "done" and not st["mayAct"]:
        raise PermissionError(f"The decision is {st['status']}: the agent may not act on it.")
    if st["done"]:
        raise InvalidEntry(f"The outcome was already reported: {st['done']}.")
    entry = log.append(
        {
            "kind": "outcome",
            "refersTo": decision["id"],
            "subject": subject,
            "agent": decision.get("agent"),
            "principal": decision.get("principal"),
            "action": decision.get("action"),
            "outcome": outcome,
            "note": note,
        }
    )
    return {"entry": entry, "status": status_of(decision, followups(log, decision["id"]))}


def all_entries(log: DecisionLog, *, after_seq: int = 0, **filters) -> list[dict]:
    out, after = [], after_seq
    while True:
        page = log.find(after_seq=after, limit=1000, **filters)
        if not page:
            return out
        out += page
        after = page[-1]["seq"]


def done_today(log: DecisionLog, action: str, day: date) -> int:
    """Actions taken today: decisions for ``action`` recorded today that were done, or that were
    allowed and have no outcome yet (the agent is about to act). Consulted ones count once done."""
    decisions = [
        e for e in all_entries(log, action=action, kind="decision") if e["recordedAt"][:10] == day.isoformat()
    ]
    if not decisions:
        return 0
    first = decisions[0]["seq"] - 1
    later: dict[str, list[dict]] = {}
    for e in all_entries(log, after_seq=first):
        if e.get("refersTo"):
            later.setdefault(e["refersTo"], []).append(e)
    n = 0
    for d in decisions:
        st = status_of(d, later.get(d["id"], []))
        if st["done"] == "done" or (st["done"] is None and st["status"] == "allowed"):
            n += 1
    return n


def measures(
    log: DecisionLog, *, since: str | None = None, until: str | None = None, agent: str | None = None
) -> dict:
    """The first measures, from the decision log (definitions in this module's docstring)."""
    entries = all_entries(log, agent=agent) if agent else all_entries(log)
    in_range = [
        e
        for e in entries
        if e["kind"] == "decision"
        and (since is None or e["recordedAt"][:10] >= since)
        and (until is None or e["recordedAt"][:10] <= until)
    ]
    later: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("refersTo"):
            later.setdefault(e["refersTo"], []).append(e)
    by_outcome = {"allow": 0, "consult": 0, "refuse": 0}
    overruled = answered = rejected_consults = pending = escalations_approved = done = 0
    for d in in_range:
        by_outcome[d["outcome"]] += 1
        st = status_of(d, later.get(d["id"], []))
        if (d["outcome"] == "allow" and st["status"] == "rejected") or (
            d["outcome"] == "refuse" and st["status"] == "approved"
        ):
            overruled += 1
        if d["outcome"] == "consult":
            if st["status"] == "pending":
                pending += 1
            else:
                answered += 1
                rejected_consults += st["status"] == "rejected"
        if d["outcome"] == "refuse" and st["status"] == "approved":
            escalations_approved += 1
        done += st["done"] == "done"
    total = len(in_range)

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    return {
        "period": {"since": since, "until": until, "agent": agent},
        "decisions": total,
        "byOutcome": by_outcome,
        "carriedOut": done,
        "overrideRate": {"value": ratio(overruled, total), "overruled": overruled, "of": total},
        "consultationPrecision": {
            "value": ratio(rejected_consults, answered),
            "rejected": rejected_consults,
            "answered": answered,
            "pending": pending,
        },
        "delegationExtended": {
            "withinDelegation": ratio(by_outcome["allow"], total),
            "escalationsApproved": escalations_approved,
            "note": "Escalations above the limit that the principal approved are candidates for extending the agent's delegation.",
        },
        "definitions": "Working definitions; see service/src/veromesh/followup.py.",
    }
