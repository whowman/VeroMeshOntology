"""Telling owners about attestations a steward recorded for them in a meeting (gap G9).

When a steward records in a meeting what an owner confirmed there, the owner is told: what
was recorded, in which meeting, by whom, and until when they may object. One reminder
follows on the third business day if they have neither confirmed nor objected.

Channels are adapters behind one small interface (``Channel``), so the workflow does not
depend on any one cloud or chat product:

- ``email``: through the mail adapter the install already has for its usage report (SMTP,
  a local sendmail, or Microsoft 365), sent to the person's email in the model, with a link
  to the steward screen. Works on every install.
- ``teams``: an Adaptive Card from the VeroMesh bot, where the customer uses Teams
  (``teams.py``).

The install's settings (``notify``, kept beside the usage settings) give the channels in
order of preference (the first that reaches the person is used), whether notices show the
values recorded or only the facts' names, and the reminder day. Personal details (an email,
an employee ID, a reporting line) are never put in a notice.

Every notice sent is a ``notice`` entry on the decision log's hash chain, which is also how
a notice is sent once: an owner was told, by which channel and when, is evidence that they
had the chance to object. A notice that cannot be sent is retried at the next run.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from . import clock, meetings
from . import provenance as P
from .entities import FIELDS as ENTITY_FIELDS
from .entities import PERSONAL_FIELDS, TYPE_OF_SHEET

log = logging.getLogger("veromesh.notify")

SETTING = "notify"
CHANNELS = ("email", "teams")
SHOW = ("values", "names")
LOCK_KEY = int.from_bytes(hashlib.sha256(b"veromesh_notify").digest()[:8], "big", signed=True)


def defaults() -> dict:
    return {"channels": ["email"], "show": "values", "reminderBusinessDays": 3}


def check_settings(value: dict | None) -> dict:
    """The notice settings, complete and checked (ValueError says what is wrong)."""
    out = defaults()
    for k, v in (value or {}).items():
        if k not in out:
            raise ValueError(f"There is no notice setting '{k}'.")
        out[k] = v
    ch = out["channels"]
    if not isinstance(ch, list) or any(c not in CHANNELS for c in ch) or len(set(ch)) != len(ch):
        raise ValueError(
            f"channels must be a list of distinct channels from {', '.join(CHANNELS)} (empty: none)."
        )
    if out["show"] not in SHOW:
        raise ValueError("show must be 'values' (the values recorded, personal details hidden) or 'names'.")
    r = out["reminderBusinessDays"]
    if not isinstance(r, int) or isinstance(r, bool) or not 0 <= r < meetings.OBJECTION_DAYS:
        raise ValueError(
            f"reminderBusinessDays must be a whole number from 1 to {meetings.OBJECTION_DAYS - 1}, or 0 for no reminder."
        )
    return out


# --------------------------------------------------------------------------- what a notice says


@dataclass
class Item:
    """One fact recorded for the owner, and what it says (values only when shown)."""

    type: str
    id: str
    name: str
    role: str
    role_name: str
    attestation: str
    fields: list[tuple[str, str | None]]  # (label, value or None when not shown)
    hidden: list[str] = field(default_factory=list)  # personal fields left out
    standing: str = "open"  # open, confirmed, disputed or stands (meetings.recorded)


@dataclass
class Notice:
    kind: str  # recorded | reminder
    person: str
    person_name: str
    email: str | None
    meeting: dict
    recorded_by: str
    recorded_by_name: str
    objection_until: str
    items: list[Item]
    link: str | None  # the steward screen

    @property
    def attestations(self) -> list[str]:
        return [i.attestation for i in self.items]


class Unreachable(Exception):
    """This channel cannot reach this person (no address, the app not installed); the next
    channel is tried."""


class Channel(Protocol):
    name: str

    def send(self, notice: Notice) -> str:
        """Deliver the notice; return what the channel said. Raise Unreachable when the
        person cannot be reached this way, any other exception when sending failed."""
        ...


_SHEET_OF_TYPE = {t: sheet for sheet, t in TYPE_OF_SHEET.items()}


def type_label(t: str) -> str:
    """'kpis' -> 'KPI', 'data-products' -> 'data product', 'people' -> 'person'."""
    words = _SHEET_OF_TYPE.get(t, t).split(" ")
    last = words[-1]
    if last == "People":
        last = "Person"
    elif last.endswith("ies"):
        last = last[:-3] + "y"
    elif last.endswith("sses"):
        last = last[:-2]
    elif last.endswith("s"):
        last = last[:-1]
    one = " ".join(words[:-1] + [last])
    return one if one[:2].isupper() else one[0].lower() + one[1:]


def _day(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B %Y')}"


def text(n: Notice) -> tuple[str, str]:
    """Subject and plain-text body (email; also the Teams card's fallback text)."""
    m = n.meeting
    until = _day(n.objection_until)
    if n.kind == "reminder":
        subject = f"Reminder: you can object until {until} to what was recorded for you in {m['name']}"
        lead = (
            f"{n.recorded_by_name} recorded in {m['name']} ({_day(m['heldOn'])}) that you confirmed the facts "
            f"below. You have not confirmed them or objected yet. They already count; if any is wrong, "
            f"object by {until}. After that they stand."
        )
    else:
        subject = f"Please check: facts recorded for you in {m['name']}"
        lead = (
            f"{n.recorded_by_name} recorded in {m['name']} ({_day(m['heldOn'])}) that you confirmed the facts "
            f"below. They count now. If any is wrong, object by {until}; after that they stand."
        )
    lines = [f"Hello {n.person_name},", "", lead, ""]
    for it in n.items:
        lines.append(f"- {it.name} ({type_label(it.type)}), as {it.role_name}")
        for label, value in it.fields:
            lines.append(f"    {label}: {value}" if value is not None else f"    {label}")
        if it.hidden:
            lines.append(f"    (not shown here: {', '.join(it.hidden)})")
    lines.append("")
    if n.link:
        lines.append(f"Confirm or object on the steward screen: {n.link}")
    else:
        lines.append("Confirm or object on the VeroMesh steward screen (Waiting for you).")
    if m.get("link"):
        lines.append(f"The meeting's record: {m['link']}")
    lines += ["", "VeroMesh"]
    return subject, "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- email


class EmailChannel:
    """Email through the install's mail adapter (the one the usage report uses)."""

    name = "email"

    def __init__(self, mail_settings, settings, send=None) -> None:
        self._mail = mail_settings  # callable: the checked mail settings
        self._settings = settings
        self._send = send  # tests

    def send(self, notice: Notice) -> str:
        if not notice.email or "@" not in notice.email:
            raise Unreachable(f"{notice.person} has no email in the model.")
        mail = self._mail()
        if mail["adapter"] == "off":
            raise Unreachable("No mail adapter is set up (Administration, Usage).")
        from . import usage

        subject, body = text(notice)
        send = self._send or usage.send_mail
        return send({**mail, "to": notice.email}, self._settings, subject, body, None, None)


# --------------------------------------------------------------------------- the notifier


def _flat(v: Any) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list | tuple | set) else [str(v)]


class Notifier:
    """Works out which owners are due a notice and sends it, once (the decision log says
    what was sent). ``settings_store``: where the install keeps its settings (the usage
    store); ``channels``: the adapters by name."""

    def __init__(
        self, store, decision_log, settings_store, channels: dict[str, Channel], public_url: str | None = None
    ) -> None:
        self.store, self.log, self.settings_store = store, decision_log, settings_store
        self.channels = channels
        self.link = public_url.rstrip("/") + "/steward/" if public_url else None
        self.problems: dict[tuple[str, str, str], str] = {}  # (meeting, person, kind) -> last failure
        self._kick = threading.Lock()

    # ------------------------------------------------------------ settings

    def settings(self) -> dict:
        return check_settings(self.settings_store.setting(SETTING) or {})

    def put_settings(self, body: dict, *, by: str | None = None) -> dict:
        merged = {**self.settings(), **(body or {})}
        out = check_settings(merged)
        self.settings_store.put_setting(SETTING, out)
        if self.log is not None:
            self.log.append(
                {
                    "kind": "configuration",
                    "setting": SETTING,
                    "value": out,
                    "outcome": "changed",
                    "subject": by or "unknown",
                }
            )
        return out

    # ------------------------------------------------------------ what was sent

    def sent(self) -> dict[tuple[str, str, str], dict]:
        """(meeting, person, recorded|reminder) -> the notice entry, for every notice sent."""
        out: dict[tuple[str, str, str], dict] = {}
        after = 0
        while True:
            page = self.log.find(kind="notice", after_seq=after, limit=1000)
            for e in page:
                out.setdefault((e["meeting"], e["subject"], e["notice"]), e)
            if len(page) < 1000:
                return out
            after = page[-1]["seq"]

    def annotate(self, rows: list[dict]) -> list[dict]:
        """Add to each recorded attestation how its owner was told (or why not yet)."""
        sent = self.sent()
        out = []
        for r in rows:
            told = [
                {"notice": k, "channel": e["channel"], "at": e["recordedAt"]}
                for k in ("recorded", "reminder")
                if (e := sent.get((r["meeting"], r["person"], k))) and r["attestation"] in e["attestations"]
            ]
            problem = self.problems.get((r["meeting"], r["person"], "recorded")) if not told else None
            out.append({**r, "notified": told, **({"notifyProblem": problem} if problem else {})})
        return out

    # ------------------------------------------------------------ what is due

    def due(self, today: date | None = None) -> list[tuple[str, str, str, list[dict]]]:
        """(kind, meeting, person, recorded rows) for each notice due now: the first notice
        for open attestations nobody has told the owner about, and the reminder on the
        reminder day for those still open."""
        today = today or clock.today()
        cfg = self.settings()
        if not cfg["channels"]:
            return []
        sent = self.sent()
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in meetings.recorded(self.store):
            if r["standing"] == "open":
                groups.setdefault((r["meeting"], r["person"]), []).append(r)
        out = []
        for (mid, pid), rows in sorted(groups.items()):
            first = sent.get((mid, pid, "recorded"))
            told = set(first["attestations"]) if first else set()
            fresh = [r for r in rows if r["attestation"] not in told]
            if fresh:
                out.append(("recorded", mid, pid, fresh))
                continue
            days = cfg["reminderBusinessDays"]
            if not days or (mid, pid, "reminder") in sent:
                continue
            on = meetings.add_business_days(date.fromisoformat(min(r["at"] for r in rows)[:10]), days)
            if today >= on:
                out.append(("reminder", mid, pid, rows))
        return out

    # ------------------------------------------------------------ building and sending

    def build(self, kind: str, mid: str, pid: str, rows: list[dict]) -> Notice:
        cfg = self.settings()
        resolved, _ = P._current(self.store)
        names = {k[1]: r.values.get("name") for k, r in resolved.items() if r.values.get("name")}

        def name(i: str | None) -> str:
            return names.get(i or "", i or "")

        person = resolved.get(("people", pid))
        pv = person.values if person else {}
        items = []
        for r in rows:
            ent = resolved.get((r["type"], r["id"]))
            labels = ENTITY_FIELDS.get(r["type"], {})
            personal = PERSONAL_FIELDS.get(r["type"], frozenset())
            fields, hidden = [], []
            for f in r["fields"]:
                label = labels[f].header if f in labels else f
                if f in personal:
                    hidden.append(label)
                elif cfg["show"] == "values" and ent is not None:
                    fields.append((label, ", ".join(name(v) for v in _flat(ent.values.get(f))) or "(empty)"))
                else:
                    fields.append((label, None))
            items.append(
                Item(
                    type=r["type"],
                    id=r["id"],
                    name=r.get("name") or r["id"],
                    role=r["role"],
                    role_name=name(r["role"]),
                    attestation=r["attestation"],
                    fields=fields,
                    hidden=hidden,
                    standing=r.get("standing", "open"),
                )
            )
        rec = rows[0].get("recordedBy") or ""
        return Notice(
            kind=kind,
            person=pid,
            person_name=pv.get("name") or pid,
            email=next(iter(_flat(pv.get("email"))), None),
            meeting=meetings.get(self.store, mid),
            recorded_by=rec,
            recorded_by_name=name(rec),
            objection_until=min(r["objectionUntil"] for r in rows),
            items=items,
            link=self.link,
        )

    def send(self, notice: Notice) -> dict:
        """Try the channels in order; the first that reaches the person is recorded."""
        problems = []
        for ch in self.settings()["channels"]:
            adapter = self.channels.get(ch)
            if adapter is None:
                problems.append(f"{ch}: not set up on this install")
                continue
            try:
                said = adapter.send(notice)
            except Unreachable as e:
                problems.append(f"{ch}: {e}")
                continue
            except Exception as e:  # noqa: BLE001 — reported, retried at the next run
                log.warning("notice to %s by %s failed: %s", notice.person, ch, e)
                problems.append(f"{ch}: {e}")
                continue
            entry = self.log.append(
                {
                    "kind": "notice",
                    "notice": notice.kind,
                    "subject": notice.person,
                    "meeting": notice.meeting["id"],
                    "channel": ch,
                    "attestations": notice.attestations,
                    "outcome": "sent",
                    "note": said[:500],
                }
            )
            self.problems.pop((notice.meeting["id"], notice.person, notice.kind), None)
            return {"person": notice.person, "notice": notice.kind, "channel": ch, "entry": entry["id"]}
        problem = "; ".join(problems) or "no channel"
        self.problems[(notice.meeting["id"], notice.person, notice.kind)] = problem
        return {"person": notice.person, "notice": notice.kind, "problem": problem}

    def run(self, today: date | None = None) -> dict:
        """Send every notice due; safe to run from several copies (one at a time)."""
        from .usage import _Busy

        try:
            with self.settings_store.exclusive(LOCK_KEY):
                results = [self.send(self.build(*d)) for d in self.due(today)]
                for ch in self.channels.values():
                    sweep = getattr(ch, "sweep", None)
                    if sweep is not None:
                        try:
                            sweep()  # cards whose facts are all answered or past their window
                        except Exception:  # noqa: BLE001 — tried again at the next run
                            log.exception("updating %s messages failed", ch.name)
        except _Busy:
            return {"skipped": "another copy of the service is sending notices"}
        return {
            "sent": [r for r in results if "channel" in r],
            "notSent": [r for r in results if "problem" in r],
        }

    def kick(self) -> None:
        """Run now in the background (after a recording), unless a kicked run is going."""
        if not self._kick.acquire(blocking=False):
            return

        def go():
            try:
                self.run()
            except Exception:  # noqa: BLE001 — logged; the hourly run tries again
                log.exception("sending notices failed")
            finally:
                self._kick.release()

        threading.Thread(target=go, name="veromesh-notify", daemon=True).start()
