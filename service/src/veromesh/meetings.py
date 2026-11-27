"""Attestation in the meetings owners already hold (gap G9).

The rule (decided 26 November 2026): a steward or the meeting's chair records an
attestation *for* the owner, with the meeting as the evidence. It counts at once, the
owner is told (the Teams card), and the owner may object for five business days.

* A **meeting** is the evidence: its name, the day it was held, a link to the minutes or
  recording, its chair, and who set it up. It is kept in its own graph and on the decision
  log's hash chain, so the evidence cannot be changed after the fact.
* The **pre-read**: for each owner expected at the meeting, the facts waiting for them:
  fields of what they own (in any role they fill) that are unconfirmed or expired.
* **Recording**: each item names the entity, the owner (a person) and the role they act in.
  The owner must fill that role and it must be an owner or steward role of the entity, as
  when owners attest for themselves. The attestation names the owner as attester, and who
  recorded it, the meeting, and the last day to object; all of it on the hash chain.
* **Objection**: the owner disputes the same fields (an ordinary dispute) before the last
  day. A recorded attestation is *open* until then, *confirmed* if the owner attests
  themselves, *disputed* if they object, and *stands* once the window closes.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from . import clock
from . import provenance as P
from .changesets import _not_found
from .ontology import check_iri, literal
from .rdf_store import RDFStore

MEETINGS_GRAPH = "https://veromesh.ai/graph/meetings"
MEETING_BASE = "https://veromesh.ai/meeting/"
VM = "https://veromesh.ai/ontology/core#"
OBJECTION_DAYS = 5  # business days


def add_business_days(start: date, days: int) -> date:
    """``days`` working days after ``start`` (Monday to Friday; public holidays are not known)."""
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def create(
    store: RDFStore,
    *,
    name: str,
    held_on: str,
    created_by: str,
    link: str | None = None,
    chair: str | None = None,
    log=None,
) -> dict:
    if not name.strip():
        raise ValueError("A meeting needs a name.")
    try:
        date.fromisoformat(held_on)
    except ValueError as exc:
        raise ValueError("heldOn is a date, YYYY-MM-DD.") from exc
    mid = uuid.uuid4().hex[:12]
    m = {
        "id": mid,
        "name": name.strip(),
        "heldOn": held_on,
        "link": link,
        "chair": chair,
        "createdBy": created_by,
        "createdAt": P.now_ts(),
    }
    if log is not None:
        log.append({"kind": "meeting", "outcome": "recorded", "subject": created_by, "meetingRecord": m})
    node = check_iri(MEETING_BASE + mid)
    props = [
        f"<{VM}meetingId> {literal(mid)}",
        f"<{VM}name> {literal(m['name'])}",
        f"<{VM}heldOn> {literal(held_on)}",
    ]
    props += [
        f"<{VM}{k}> {literal(str(m[k]))}" for k in ("link", "chair", "createdBy", "createdAt") if m.get(k)
    ]
    store.update(f"INSERT DATA {{ GRAPH <{MEETINGS_GRAPH}> {{ <{node}> {' ; '.join(props)} . }} }}")
    return m


def list_meetings(store: RDFStore) -> list[dict]:
    rows = store.query(f"""SELECT ?m ?p ?o WHERE {{ GRAPH <{MEETINGS_GRAPH}> {{ ?m ?p ?o }} }}""")["results"][
        "bindings"
    ]
    out: dict[str, dict] = {}
    for r in rows:
        key = r["p"]["value"].removeprefix(VM)
        out.setdefault(r["m"]["value"], {})["id" if key == "meetingId" else key] = r["o"]["value"]
    return sorted(out.values(), key=lambda m: (m.get("heldOn", ""), m.get("createdAt", "")), reverse=True)


def get(store: RDFStore, mid: str) -> dict:
    m = next((m for m in list_meetings(store) if m["id"] == mid), None)
    if m is None:
        raise _not_found(f"No meeting '{mid}'.")
    return m


def preread(store: RDFStore, base: str, people: list[str]) -> dict:
    """For each person: the roles they fill and the facts waiting for them, owner by owner."""
    resolved, _ = P._current(store)
    out = []
    for pid in people:
        person = resolved.get(("people", pid))
        if person is None:
            raise _not_found(f"No person '{pid}' in the model.")
        filled = person.values.get("rolesFilled") or []
        filled = filled if isinstance(filled, list) else [filled]
        items = []
        for role in sorted(filled):
            for status in ("unconfirmed", "expired"):
                for row in P.review_queue(store, status=status, owner_role=role, limit=1000):
                    items.append({**row, "status": status, "role": role})
        out.append(
            {
                "person": pid,
                "name": person.values.get("name"),
                "roles": sorted(filled),
                "waiting": sorted(items, key=lambda i: (i["type"], i["id"])),
            }
        )
    return {"people": out, "total": sum(len(p["waiting"]) for p in out)}


def record(
    store: RDFStore,
    base: str,
    mid: str,
    items: list[dict],
    *,
    recorded_by: str,
    log=None,
) -> dict:
    """Attest each item for its owner, in meeting ``mid``. All are checked before any is
    written, so a bad item leaves nothing half recorded."""
    m = get(store, mid)
    until = add_business_days(clock.today(), OBJECTION_DAYS).isoformat()
    resolved, _ = P._current(store)
    for i, it in enumerate(items, 1):
        for k in ("type", "id", "owner", "role"):
            if not it.get(k):
                raise ValueError(f"Item {i} needs {k}.")
        if (it["type"], it["id"]) not in resolved:
            raise _not_found(f"Item {i}: no {it['type']} '{it['id']}'.")
        if it["owner"] == recorded_by:
            raise ValueError(f"Item {i}: you are the owner; attest it yourself rather than record it.")
        P._check_attester(
            resolved, it["type"], it["id"], it["owner"], it["role"], signed_in=True, owner_only=True
        )
    out = []
    for it in items:
        made = P.attest(
            store,
            base,
            it["type"],
            it["id"],
            verdict="attested",
            attester=it["owner"],
            role=it["role"],
            fields=it.get("fields"),
            note=it.get("note") or f"Recorded in {m['name']} on {m['heldOn']}.",
            signed_in=True,
            owner_only=True,
            log=log,
            recorded_by=recorded_by,
            meeting=mid,
            objection_until=until,
        )
        out.append({"type": it["type"], "id": it["id"], "owner": it["owner"], **made})
    return {"meeting": m, "objectionUntil": until, "recorded": out}


def _standing(a: P.Attestation, later: list[P.Attestation], today: date) -> str:
    """open, confirmed, disputed or stands: what became of an attestation recorded for someone."""
    mine = [
        x
        for x in later
        if x.iri != a.iri and x.person == a.person and x.at >= a.at and set(x.hashes) & set(a.hashes)
    ]
    if any(x.verdict == "disputed" for x in mine):
        return "disputed"
    if any(x.verdict == "attested" and not x.recorded_by for x in mine):
        return "confirmed"
    return "open" if today <= date.fromisoformat(a.objection_until or "1970-01-01") else "stands"


def recorded(store: RDFStore, *, meeting: str | None = None, person: str | None = None) -> list[dict]:
    """Attestations recorded for someone in a meeting, with where each stands."""
    atts = P.read_attestations(store)
    by_entity: dict[tuple[str, str], list[P.Attestation]] = {}
    for a in atts:
        by_entity.setdefault((a.type, a.id), []).append(a)
    today = clock.today()
    resolved, _ = P._current(store)
    out = []
    for a in atts:
        if not a.recorded_by or (meeting and a.meeting != meeting) or (person and a.person != person):
            continue
        out.append(
            {
                "type": a.type,
                "id": a.id,
                "name": (r.values.get("name") if (r := resolved.get((a.type, a.id))) else None),
                "fields": sorted(a.hashes),
                **a.summary(),
                "standing": _standing(a, by_entity[(a.type, a.id)], today),
            }
        )
    return sorted(out, key=lambda r: (r["at"], r["type"], r["id"]))
