"""The usage report: what each install had in scope and did, month by month, signed.

Every install counts what it holds once an hour (the day's highest count of
identities is kept), and on the first day of each month makes the report for the
month before: identities in scope at month end and at the month's peak, the share
of the licensed tier, and everything else the install can count, by day. Only
identities in scope are charged; the rest is shown (decided 22 November).

A report is never limited and never limits: usage over the tier changes nothing
in the product. It is stored append-only beside the decision log, signed with the
install's own Ed25519 key and chained to the month before and to the decision
log's hash chain, so a changed or missing month shows. Installs with a mail
adapter set up also send it to VeroMesh (SMTP, a local sendmail, or Microsoft 365).

Identities in scope are people and AI agents in the effective model; service
accounts are counted from the IAM connector (Phase 2) and read 0 until then.

Storage follows the decision log (``VEROMESH_DECISION_LOG``): in memory for
development and tests, or PostgreSQL tables in the same database.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import hashlib
import json
import logging
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__

log = logging.getLogger("veromesh.usage")

FORMAT = "veromesh-usage-report/1"
DEFAULT_RECIPIENT = "OntologyToolLicensing@veromesh.ai"
# The licensed tiers (identities in scope each guarantees). Extra large is guaranteed
# at 500,000 and open above it; its share of tier is against 500,000.
TIERS = {
    "small": ("Small", 5_000),
    "medium": ("Medium", 25_000),
    "large": ("Large", 100_000),
    "extra-large": ("Extra large", 500_000),
}
ADAPTERS = ("off", "smtp", "sendmail", "m365")
RETRY_AFTER = timedelta(hours=1)  # a failed send is tried again this long after
NOTES = [
    "Only identities in scope are charged: the peak of the month. Everything else is shown, not charged.",
    "Service accounts are counted from the identity and access system connector (Phase 2); until then 0.",
]


# --------------------------------------------------------------------------- signing


def canonical(obj: Any) -> bytes:
    """The bytes a report is hashed and signed over: JSON, keys sorted, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def report_hash(report: dict) -> str:
    return hashlib.sha256(canonical(report)).hexdigest()


class Signer:
    """The install's Ed25519 key."""

    def __init__(self, pem: str) -> None:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(pem.encode(), password=None)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("The usage signing key must be an Ed25519 private key (PEM, PKCS#8).")
        self._key = key

    @staticmethod
    def generate_pem() -> str:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return (
            Ed25519PrivateKey.generate()
            .private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
            .decode()
        )

    @property
    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def sign(self, report: dict) -> dict:
        """The stored document: the report, its hash and the signature over its canonical bytes."""
        return {
            "report": report,
            "hash": report_hash(report),
            "signature": {
                "algorithm": "Ed25519",
                "publicKey": self.public_key,
                "value": base64.b64encode(self._key.sign(canonical(report))).decode(),
            },
        }


def verify(doc: dict, public_key: str | None = None, previous: dict | None = None) -> list[str]:
    """What is wrong with a signed report (nothing: an empty list). ``public_key``: the
    key the install's licence names (otherwise the one in the document is used, which
    proves only that the report was not changed after signing); ``previous``: the month
    before, to check the chain."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    problems = []
    report = doc.get("report")
    sig = doc.get("signature") or {}
    if not isinstance(report, dict):
        return ["The document holds no report."]
    if report.get("format") != FORMAT:
        problems.append(f"Unknown format {report.get('format')!r}.")
    if doc.get("hash") != report_hash(report):
        problems.append("The report's hash does not match its content: it was changed after it was made.")
    key = public_key or sig.get("publicKey")
    if public_key and sig.get("publicKey") != public_key:
        problems.append("The report was signed with a different key than the one given.")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key or "")).verify(
            base64.b64decode(sig.get("value") or ""), canonical(report)
        )
    except (InvalidSignature, ValueError):
        problems.append("The signature does not verify.")
    if previous is not None:
        prev = previous.get("report") or {}
        if report.get("previousReport") != previous.get("hash"):
            problems.append(
                f"The chain is broken: this report does not follow the {prev.get('month')} report given."
            )
        elif prev.get("install", {}).get("id") != report.get("install", {}).get("id"):
            problems.append("The previous report is from another install.")
        elif _month_after(prev.get("month", "")) != report.get("month"):
            problems.append(f"A month is missing between {prev.get('month')} and {report.get('month')}.")
    return problems


# --------------------------------------------------------------------------- months and days


def _month_after(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"


def month_bounds(month: str, time_zone: str) -> tuple[datetime, datetime]:
    """[start, end) of a calendar month in ``time_zone``."""
    tz = ZoneInfo(time_zone)
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1, tzinfo=tz)
    nxt = _month_after(month)
    return start, datetime(int(nxt[:4]), int(nxt[5:7]), 1, tzinfo=tz)


def month_of(instant: datetime, time_zone: str) -> str:
    return instant.astimezone(ZoneInfo(time_zone)).strftime("%Y-%m")


def days_of(month: str) -> list[str]:
    y, m = int(month[:4]), int(month[5:7])
    return [date(y, m, d).isoformat() for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def check_time_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"'{name}' is not a time zone (use an IANA name such as America/New_York).") from exc
    return name


# --------------------------------------------------------------------------- storage


class UsageStore(Protocol):
    def put_day(self, day: str, counts: dict) -> None: ...
    def days(self, first: str, last: str) -> dict[str, dict]: ...
    def first_day(self) -> str | None: ...
    def add_report(self, month: str, doc: dict) -> bool: ...
    def report(self, month: str) -> dict | None: ...
    def reports(self) -> list[dict]: ...
    def setting(self, key: str) -> Any: ...
    def put_setting(self, key: str, value: Any) -> None: ...
    def add_setting(self, key: str, value: Any) -> Any: ...
    def add_delivery(self, month: str, ok: bool, adapter: str, recipient: str, detail: str) -> None: ...
    def deliveries(self, month: str | None = None) -> list[dict]: ...
    def exclusive(self, key: int | None = None): ...


def _merge_day(old: dict | None, new: dict) -> dict:
    """A day's counts: the day's highest identities count is kept (with when it was seen);
    the other counts are the latest."""
    if old is None:
        return new
    keep = old["identities"]["total"] >= new["identities"]["total"]
    out = dict(new)
    if keep:
        out["identities"], out["peakAt"] = old["identities"], old.get("peakAt")
    return out


class MemoryUsage:
    """For development and tests; gone when the process stops."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._days: dict[str, dict] = {}
        self._reports: dict[str, tuple[str, str]] = {}  # month -> (created at, document text)
        self._settings: dict[str, Any] = {}
        self._deliveries: list[dict] = []

    def put_day(self, day: str, counts: dict) -> None:
        with self._lock:
            self._days[day] = _merge_day(self._days.get(day), counts)

    def days(self, first: str, last: str) -> dict[str, dict]:
        with self._lock:
            return {d: json.loads(json.dumps(c)) for d, c in self._days.items() if first <= d <= last}

    def first_day(self) -> str | None:
        with self._lock:
            return min(self._days, default=None)

    def add_report(self, month: str, doc: dict) -> bool:
        with self._lock:
            if month in self._reports:
                return False
            self._reports[month] = (datetime.now(UTC).isoformat(), json.dumps(doc))
            return True

    def report(self, month: str) -> dict | None:
        with self._lock:
            got = self._reports.get(month)
        return json.loads(got[1]) if got else None

    def reports(self) -> list[dict]:
        with self._lock:
            items = sorted(self._reports.items())
        return [{"month": m, "createdAt": at, "hash": json.loads(text)["hash"]} for m, (at, text) in items]

    def setting(self, key: str) -> Any:
        with self._lock:
            return json.loads(json.dumps(self._settings.get(key)))

    def put_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._settings[key] = json.loads(json.dumps(value))

    def add_setting(self, key: str, value: Any) -> Any:
        with self._lock:
            self._settings.setdefault(key, json.loads(json.dumps(value)))
            return self._settings[key]

    def add_delivery(self, month: str, ok: bool, adapter: str, recipient: str, detail: str) -> None:
        with self._lock:
            self._deliveries.append(
                {
                    "month": month,
                    "attemptedAt": datetime.now(UTC).isoformat(),
                    "ok": ok,
                    "adapter": adapter,
                    "recipient": recipient,
                    "detail": detail,
                }
            )

    def deliveries(self, month: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self._deliveries if month is None or d["month"] == month]

    @contextmanager
    def exclusive(self, key: int | None = None):
        with self._lock:
            yield


SCHEMA = """
CREATE TABLE IF NOT EXISTS veromesh_usage_day (
    day         date PRIMARY KEY,
    counts      jsonb NOT NULL,
    updated_at  timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS veromesh_usage_report (
    month       text PRIMARY KEY,
    created_at  timestamptz NOT NULL,
    hash        text NOT NULL UNIQUE,
    document    text NOT NULL
);
CREATE OR REPLACE FUNCTION veromesh_usage_report_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'usage reports are append-only: % is not allowed', TG_OP;
END $$;
DROP TRIGGER IF EXISTS veromesh_usage_report_no_change ON veromesh_usage_report;
CREATE TRIGGER veromesh_usage_report_no_change BEFORE UPDATE OR DELETE ON veromesh_usage_report
    FOR EACH ROW EXECUTE FUNCTION veromesh_usage_report_append_only();
DROP TRIGGER IF EXISTS veromesh_usage_report_no_truncate ON veromesh_usage_report;
CREATE TRIGGER veromesh_usage_report_no_truncate BEFORE TRUNCATE ON veromesh_usage_report
    FOR EACH STATEMENT EXECUTE FUNCTION veromesh_usage_report_append_only();
CREATE TABLE IF NOT EXISTS veromesh_usage_setting (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS veromesh_usage_delivery (
    id           bigserial PRIMARY KEY,
    month        text NOT NULL REFERENCES veromesh_usage_report (month),
    attempted_at timestamptz NOT NULL,
    ok           boolean NOT NULL,
    adapter      text NOT NULL,
    recipient    text NOT NULL,
    detail       text NOT NULL
);
"""

LOCK_KEY = int.from_bytes(hashlib.sha256(b"veromesh_usage").digest()[:8], "big", signed=True)


class PostgresUsage:
    """Tables in the decision log's database (created on first use)."""

    def __init__(self, url: str) -> None:
        from .decisions import pg_params

        self._params = pg_params(url)
        self._lock = threading.RLock()
        self._conn = None

    def _connect(self):
        if self._conn is None:
            from .decisions import LOCK_KEY as LOG_LOCK
            from .decisions import _statements, pg_connect

            conn = pg_connect(self._params)
            conn.run("BEGIN")
            conn.run("SELECT pg_advisory_xact_lock(:k)", k=LOG_LOCK)  # one schema change at a time
            for stmt in _statements(SCHEMA):
                conn.run(stmt)
            conn.run("COMMIT")
            self._conn = conn
        return self._conn

    def _run(self, sql: str, **params):
        with self._lock:
            for attempt in (1, 2):
                try:
                    return self._connect().run(sql, **params)
                except Exception as exc:
                    broken = type(exc).__name__ in ("InterfaceError", "ConnectionError")
                    if self._conn is not None and broken:
                        try:
                            self._conn.close()
                        except Exception:
                            pass
                        self._conn = None
                    if attempt == 2 or not broken:
                        raise

    def put_day(self, day: str, counts: dict) -> None:
        with self._lock:
            rows = self._run("SELECT counts FROM veromesh_usage_day WHERE day = CAST(:d AS date)", d=day)
            merged = _merge_day(_json(rows[0][0]) if rows else None, counts)
            self._run(
                "INSERT INTO veromesh_usage_day (day, counts, updated_at) VALUES (CAST(:d AS date), CAST(:c AS jsonb), now())"
                " ON CONFLICT (day) DO UPDATE SET counts = EXCLUDED.counts, updated_at = now()",
                d=day,
                c=json.dumps(merged),
            )

    def days(self, first: str, last: str) -> dict[str, dict]:
        rows = self._run(
            "SELECT to_char(day, 'YYYY-MM-DD'), counts FROM veromesh_usage_day"
            " WHERE day BETWEEN CAST(:a AS date) AND CAST(:b AS date)",
            a=first,
            b=last,
        )
        return {d: _json(c) for d, c in rows}

    def first_day(self) -> str | None:
        rows = self._run("SELECT to_char(min(day), 'YYYY-MM-DD') FROM veromesh_usage_day")
        return rows[0][0] if rows and rows[0][0] else None

    def add_report(self, month: str, doc: dict) -> bool:
        rows = self._run(
            "INSERT INTO veromesh_usage_report (month, created_at, hash, document) VALUES (:m, now(), :h, :d)"
            " ON CONFLICT (month) DO NOTHING RETURNING month",
            m=month,
            h=doc["hash"],
            d=json.dumps(doc),
        )
        return bool(rows)

    def report(self, month: str) -> dict | None:
        rows = self._run("SELECT document FROM veromesh_usage_report WHERE month = :m", m=month)
        return json.loads(rows[0][0]) if rows else None

    def reports(self) -> list[dict]:
        rows = self._run("SELECT month, created_at, hash FROM veromesh_usage_report ORDER BY month")
        return [{"month": m, "createdAt": _iso(at), "hash": h} for m, at, h in rows]

    def setting(self, key: str) -> Any:
        rows = self._run("SELECT value FROM veromesh_usage_setting WHERE key = :k", k=key)
        return _json(rows[0][0]) if rows else None

    def put_setting(self, key: str, value: Any) -> None:
        self._run(
            "INSERT INTO veromesh_usage_setting (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            k=key,
            v=json.dumps(value),
        )

    def add_setting(self, key: str, value: Any) -> Any:
        self._run(
            "INSERT INTO veromesh_usage_setting (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), now())"
            " ON CONFLICT (key) DO NOTHING",
            k=key,
            v=json.dumps(value),
        )
        return self.setting(key)

    def add_delivery(self, month: str, ok: bool, adapter: str, recipient: str, detail: str) -> None:
        self._run(
            "INSERT INTO veromesh_usage_delivery (month, attempted_at, ok, adapter, recipient, detail)"
            " VALUES (:m, now(), :ok, :a, :r, :d)",
            m=month,
            ok=ok,
            a=adapter,
            r=recipient,
            d=detail[:2000],
        )

    def deliveries(self, month: str | None = None) -> list[dict]:
        sql = "SELECT month, attempted_at, ok, adapter, recipient, detail FROM veromesh_usage_delivery"
        rows = (
            self._run(sql + " WHERE month = :m ORDER BY id", m=month)
            if month
            else self._run(sql + " ORDER BY id DESC LIMIT 200")[::-1]
        )
        return [
            {"month": m, "attemptedAt": _iso(at), "ok": ok, "adapter": a, "recipient": r, "detail": d}
            for m, at, ok, a, r, d in rows
        ]

    @contextmanager
    def exclusive(self, key: int | None = None):
        """One run at a time across every copy of the service (a session advisory lock):
        the usage job's, or another job's with its own ``key``."""
        from .decisions import pg_connect

        k = LOCK_KEY if key is None else key
        conn = pg_connect(self._params)
        try:
            if not conn.run("SELECT pg_try_advisory_lock(:k)", k=k)[0][0]:
                raise _Busy()
            try:
                yield
            finally:
                conn.run("SELECT pg_advisory_unlock(:k)", k=k)
        finally:
            conn.close()


class _Busy(Exception):
    """Another copy of the service is running the usage job."""


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _iso(value) -> str:
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else str(value)


def make_store(spec: str) -> UsageStore:
    if spec == "memory":
        return MemoryUsage()
    if spec.startswith(("postgresql://", "postgres://")):
        return PostgresUsage(spec)
    raise ValueError("VEROMESH_DECISION_LOG must be 'memory' or a postgresql:// URL.")


# --------------------------------------------------------------------------- settings


def _default_mail() -> dict:
    return {
        "adapter": "off",
        "to": DEFAULT_RECIPIENT,
        "from": "",
        "smtp": {"host": "", "port": 587, "security": "starttls", "username": ""},
        "sendmail": {"path": "/usr/sbin/sendmail"},
        "m365": {"tenantId": "", "clientId": "", "mailbox": ""},
    }


def check_mail(mail: dict) -> dict:
    """The mail settings, complete and checked (ValueError says what is wrong)."""
    out = _default_mail()
    for k, v in (mail or {}).items():
        if k in ("smtp", "sendmail", "m365"):
            if not isinstance(v, dict):
                raise ValueError(f"mail.{k} must be an object.")
            unknown = set(v) - set(out[k])
            if unknown:
                raise ValueError(f"mail.{k} has no setting {', '.join(sorted(unknown))}.")
            out[k].update(v)
        elif k in out:
            out[k] = v
        else:
            raise ValueError(f"There is no mail setting '{k}'.")
    if out["adapter"] not in ADAPTERS:
        raise ValueError(f"mail.adapter must be one of {', '.join(ADAPTERS)}.")
    if out["smtp"]["security"] not in ("starttls", "tls", "none"):
        raise ValueError("mail.smtp.security must be starttls, tls or none.")
    try:
        out["smtp"]["port"] = int(out["smtp"]["port"])
    except (TypeError, ValueError) as exc:
        raise ValueError("mail.smtp.port must be a number.") from exc
    if not out["to"] or "@" not in out["to"]:
        raise ValueError("mail.to must be an email address.")
    if out["adapter"] != "off" and out["adapter"] != "m365" and "@" not in (out["from"] or ""):
        raise ValueError("mail.from must be the address reports are sent from.")
    if out["adapter"] == "smtp" and not out["smtp"]["host"]:
        raise ValueError("mail.smtp.host is needed for the SMTP adapter.")
    if out["adapter"] == "m365" and not all(out["m365"][k] for k in ("tenantId", "clientId", "mailbox")):
        raise ValueError("The Microsoft 365 adapter needs mail.m365.tenantId, clientId and mailbox.")
    return out


# --------------------------------------------------------------------------- the usage of one install


class Usage:
    """Counting, reports and delivery for one install. ``model``: a callable giving the
    (RDF store, instance base) to count from; ``settings``: the service's Settings."""

    def __init__(self, store: UsageStore, decision_log, settings, model=None) -> None:
        self.store = store
        self.log = decision_log
        self.settings = settings
        self.model = model
        self.licensing = None  # licence.Licensing, when the install has one (the tier comes from it)

    # ---- install, tier, key, settings

    def install(self) -> dict:
        s = self.settings
        install_id = s.install_id or self.store.add_setting("install-id", str(uuid.uuid4()))
        return {
            "id": install_id,
            "customer": s.customer_name,
            "hosting": s.hosting,
            "version": __version__,
        }

    def tier(self) -> dict | None:
        """The licensed tier: from the licence in force, else VEROMESH_LICENSED_TIER."""
        from_licence = self.licensing.tier() if self.licensing is not None else None
        name = from_licence or (self.settings.licensed_tier or "").strip().lower()
        if not name:
            return None
        if name not in TIERS:
            raise ValueError(
                f"VEROMESH_LICENSED_TIER must be one of {', '.join(TIERS)} (or empty), not '{name}'."
            )
        label, ceiling = TIERS[name]
        return {"name": label, "identities": ceiling}

    def signer(self) -> Signer:
        pem = self.settings.usage_signing_key
        if not pem:
            pem = self.store.add_setting("signing-key", Signer.generate_pem())
        return Signer(pem)

    def time_zone(self) -> str:
        return self.store.setting("time-zone") or self.settings.time_zone

    def mail(self) -> dict:
        return check_mail(self.store.setting("mail") or {})

    def get_settings(self) -> dict:
        return {
            "timeZone": self.time_zone(),
            "mail": self.mail(),
            "secrets": {
                "smtpPassword": bool(self.settings.usage_smtp_password),
                "m365ClientSecret": bool(self.settings.usage_m365_client_secret),
            },
        }

    def put_settings(self, body: dict) -> dict:
        unknown = set(body) - {"timeZone", "mail"}
        if unknown:
            raise ValueError(f"There is no usage setting {', '.join(sorted(unknown))}.")
        if "timeZone" in body:
            self.store.put_setting("time-zone", check_time_zone(str(body["timeZone"])))
        if "mail" in body:
            merged = self.mail()
            for k, v in (body["mail"] or {}).items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            self.store.put_setting("mail", check_mail(merged))
        return self.get_settings()

    # ---- counting

    def count(self) -> dict:
        """What the model holds now: identities in scope, and the shown-not-charged counts."""
        from . import changesets
        from . import entities as E
        from . import provenance as P
        from .changesets import CHANGESET_GRAPH, VM

        store, base = self.model()
        entities = {t: len(changesets.list_entities(store, t, base)) for t in E.TYPES}
        people, agents = entities.get("people", 0), entities.get("agents", 0)
        tz = ZoneInfo(self.time_zone())
        now = datetime.now(UTC)
        today = now.astimezone(tz).date()
        start = datetime(today.year, today.month, today.day, tzinfo=tz).astimezone(UTC)
        rows = store.query(
            f"""SELECT (COUNT(?c) AS ?n) WHERE {{ GRAPH <{CHANGESET_GRAPH}> {{ ?c <{VM.committedAt}> ?t }}
                FILTER (?t >= "{start.isoformat()}"^^<http://www.w3.org/2001/XMLSchema#dateTime>) }}"""
        )["results"]["bindings"]
        committed = int(rows[0]["n"]["value"]) if rows else 0
        return {
            "identities": {
                "people": people,
                "agents": agents,
                "serviceAccounts": 0,
                "total": people + agents,
            },
            "peakAt": now.isoformat(),
            "entities": entities,
            "attestations": len(P._read_attestations(store, None)),
            "changesetsCommitted": committed,
            "countedAt": now.isoformat(),
        }

    def take_count(self) -> dict:
        counts = self.count()
        day = datetime.now(UTC).astimezone(ZoneInfo(self.time_zone())).date().isoformat()
        self.store.put_day(day, counts)
        return {"day": day, **counts}

    # ---- reports

    def build(self, month: str, *, until: datetime | None = None) -> dict:
        """The report for ``month`` (unsigned). ``until``: for the month so far."""
        tz_name = self.time_zone()
        start, end = month_bounds(month, tz_name)
        if until is not None:
            end = min(end, until)
        days = days_of(month)
        counted = self.store.days(days[0], days[-1])
        log_days = self.log.count_by_day(start, end, tz_name)
        rows, kinds_total = [], {}
        for d in days:
            c = counted.get(d)
            entries = log_days.get(d, {})
            for k, n in entries.items():
                kinds_total[k] = kinds_total.get(k, 0) + n
            rows.append(
                {
                    "date": d,
                    "identities": c["identities"] if c else None,
                    "decisionLog": dict(sorted(entries.items())),
                    "entities": c.get("entities") if c else None,
                    "attestations": c.get("attestations") if c else None,
                    "changesetsCommitted": c.get("changesetsCommitted") if c else None,
                }
            )
        with_counts = [r for r in rows if r["identities"] is not None]
        peak = max(with_counts, key=lambda r: r["identities"]["total"], default=None)
        last = with_counts[-1] if with_counts else None
        tier = self.tier()
        share = None
        if tier and peak:
            share = str(
                (Decimal(peak["identities"]["total"]) / Decimal(tier["identities"])).quantize(
                    Decimal("0.001"), ROUND_HALF_UP
                )
            )
        busiest = max(rows, key=lambda r: r["decisionLog"].get("decision", 0), default=None)
        seq = self.log.last_seq_before(end)
        head = self.log.find(after_seq=seq - 1, limit=1) if seq else []
        before = [r for r in self.store.reports() if r["month"] < month]
        return {
            "format": FORMAT,
            "install": self.install(),
            "month": month,
            "timeZone": tz_name,
            "from": start.isoformat(),
            "until": end.isoformat(),
            "tier": tier,
            "licence": self._licence_line(),
            "identities": {
                "monthEnd": {**last["identities"], "date": last["date"]} if last else None,
                "peak": {**peak["identities"], "date": peak["date"]} if peak else None,
                "shareOfTier": share,
                "daysCounted": len(with_counts),
            },
            "charged": {"identitiesInScope": peak["identities"]["total"] if peak else None},
            "shown": {
                "decisionLog": dict(sorted(kinds_total.items())),
                "busiestDay": (
                    {"date": busiest["date"], "decisions": busiest["decisionLog"].get("decision", 0)}
                    if busiest and busiest["decisionLog"].get("decision")
                    else None
                ),
                "entities": last["entities"] if last else None,
                "attestations": last["attestations"] if last else None,
                "changesetsCommitted": sum(r["changesetsCommitted"] or 0 for r in rows),
                "days": rows,
            },
            "decisionLogHead": {"seq": seq, "hash": head[0]["hash"] if head else None},
            "previousReport": before[-1]["hash"] if before else None,
            "createdAt": datetime.now(UTC).isoformat(),
            "notes": NOTES,
        }

    def _licence_line(self) -> dict | None:
        if self.licensing is None:
            return None
        st = self.licensing.status()
        lic = st["licence"]
        return {
            "id": lic["id"] if lic else None,
            "stage": st["stage"],
            "validUntil": lic["validUntil"] if lic else None,
        }

    def make_due(self, now: datetime | None = None) -> list[str]:
        """Make every report not yet made, from the first counted month to the last ended one."""
        now = now or datetime.now(UTC)
        tz = self.time_zone()
        first = self.store.first_day()
        if first is None:
            return []
        made, month, current = [], first[:7], month_of(now, tz)
        signer = None
        while month < current:
            if self.store.report(month) is None:
                signer = signer or self.signer()
                if self.store.add_report(month, signer.sign(self.build(month))):
                    made.append(month)
                    log.info("usage report for %s made", month)
            month = _month_after(month)
        return made

    def so_far(self) -> dict:
        """This month so far, as the report will count it (not signed)."""
        now = datetime.now(UTC)
        return {**self.build(month_of(now, self.time_zone()), until=now), "signed": False}

    # ---- delivery

    def deliver_pending(self, now: datetime | None = None) -> list[dict]:
        """Send every report not yet delivered, unless it was tried within RETRY_AFTER."""
        mail = self.mail()
        if mail["adapter"] == "off":
            return []
        now = now or datetime.now(UTC)
        out = []
        for r in self.store.reports():
            tries = self.store.deliveries(r["month"])
            if any(t["ok"] for t in tries):
                continue
            if tries and now - datetime.fromisoformat(tries[-1]["attemptedAt"]) < RETRY_AFTER:
                continue
            out.append(self.send(r["month"], mail))
        return out

    def send(self, month: str, mail: dict | None = None) -> dict:
        """Send one report now and record how it went."""
        mail = mail or self.mail()
        doc = self.store.report(month)
        if doc is None:
            raise LookupError(f"There is no usage report for {month}.")
        if mail["adapter"] == "off":
            raise ValueError("No mail adapter is set up (usage settings, mail.adapter).")
        try:
            detail = send_mail(mail, self.settings, *message_for(doc))
            ok = True
        except Exception as exc:  # noqa: BLE001 — recorded, retried later
            detail, ok = f"{type(exc).__name__}: {exc}", False
            log.warning("usage report %s not sent: %s", month, detail)
        self.store.add_delivery(month, ok, mail["adapter"], mail["to"], detail)
        return {
            "month": month,
            "ok": ok,
            "adapter": mail["adapter"],
            "recipient": mail["to"],
            "detail": detail,
        }

    def send_test(self) -> dict:
        mail = self.mail()
        if mail["adapter"] == "off":
            raise ValueError("No mail adapter is set up (usage settings, mail.adapter).")
        inst = self.install()
        subject = f"VeroMesh usage reports: test from {inst['customer'] or inst['id']}"
        body = (
            f"A test message from VeroMesh install {inst['id']}: usage reports will be sent to {mail['to']}."
        )
        try:
            return {"ok": True, "detail": send_mail(mail, self.settings, subject, body, None, None)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    # ---- the hourly run

    def run(self, now: datetime | None = None) -> dict:
        """Count, make the reports that are due, send what waits. One copy at a time."""
        try:
            with self.store.exclusive():
                counted = self.take_count() if self.model is not None else None
                made = self.make_due(now)
                sent = self.deliver_pending(now)
                return {"counted": counted, "made": made, "sent": sent}
        except _Busy:
            return {"skipped": "another copy of the service is running the usage job"}

    def overview(self) -> dict:
        reports = self.store.reports()
        tries = self.store.deliveries()
        for r in reports:
            mine = [t for t in tries if t["month"] == r["month"]]
            r["delivery"] = mine[-1] if mine else None
        return {
            "install": self.install(),
            "tier": self.tier(),
            "settings": self.get_settings(),
            "current": self.so_far(),
            "reports": reports,
        }


# --------------------------------------------------------------------------- mail


def message_for(doc: dict) -> tuple[str, str, str, bytes]:
    """(subject, body, attachment name, attachment bytes) for a signed report."""
    r = doc["report"]
    inst = r["install"]
    who = inst["customer"] or inst["id"]
    ids = r["identities"]
    peak = ids["peak"]["total"] if ids["peak"] else "not counted"
    tier = f"{r['tier']['name']} ({r['tier']['identities']:,})" if r["tier"] else "not set"
    share = f"{Decimal(ids['shareOfTier']) * 100:.1f}%" if ids["shareOfTier"] else "-"
    subject = f"VeroMesh usage report {r['month']}: {who}"
    body = (
        f"Usage report for {r['month']} ({r['timeZone']}) from VeroMesh install {inst['id']}"
        f" ({who}, {inst['hosting']}, version {inst['version']}).\n\n"
        f"Identities in scope at the peak: {peak}\n"
        f"Tier: {tier}; share of tier: {share}\n"
        f"Decision checks: {r['shown']['decisionLog'].get('decision', 0)}\n\n"
        f"The signed report is attached (hash {doc['hash']}). Check it with: veromesh-usage verify <file>\n"
    )
    name = f"veromesh-usage-{inst['id']}-{r['month']}.json"
    return subject, body, name, json.dumps(doc, indent=2).encode()


def _email(mail: dict, subject: str, body: str, name: str | None, data: bytes | None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail["from"] or mail["m365"].get("mailbox", "")
    msg["To"] = mail["to"]
    msg.set_content(body)
    if name and data is not None:
        msg.add_attachment(data, maintype="application", subtype="json", filename=name)
    return msg


def send_mail(mail: dict, settings, subject: str, body: str, name: str | None, data: bytes | None) -> str:
    """Send through the configured adapter; returns what the server said."""
    adapter = mail["adapter"]
    if adapter == "smtp":
        import smtplib

        smtp = mail["smtp"]
        cls = smtplib.SMTP_SSL if smtp["security"] == "tls" else smtplib.SMTP
        with cls(smtp["host"], smtp["port"], timeout=60) as server:
            if smtp["security"] == "starttls":
                server.starttls()
            if smtp["username"]:
                server.login(smtp["username"], settings.usage_smtp_password or "")
            refused = server.send_message(_email(mail, subject, body, name, data))
        if refused:
            raise RuntimeError(f"refused: {refused}")
        return f"sent by SMTP through {smtp['host']}:{smtp['port']}"
    if adapter == "sendmail":
        path = mail["sendmail"]["path"]
        done = subprocess.run(
            [path, "-t", "-oi"],
            input=_email(mail, subject, body, name, data).as_bytes(),
            capture_output=True,
            timeout=120,
            check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(
                f"{path} exited {done.returncode}: {done.stderr.decode(errors='replace')[:500]}"
            )
        return f"handed to {path}"
    if adapter == "m365":
        return _send_m365(mail, settings, subject, body, name, data)
    raise ValueError(f"No mail adapter '{adapter}'.")


GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"


def _send_m365(mail: dict, settings, subject: str, body: str, name, data, client=None) -> str:
    """Microsoft 365 through Graph: an app registration with the Mail.Send application
    permission (limited to the mailbox by an application access policy), sending as the
    shared mailbox."""
    import httpx

    m = mail["m365"]
    secret = settings.usage_m365_client_secret
    if not secret:
        raise ValueError("VEROMESH_USAGE_M365_CLIENT_SECRET is not set.")
    http = client or httpx.Client(timeout=60)
    try:
        tok = http.post(
            f"{LOGIN}/{m['tenantId']}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": m["clientId"],
                "client_secret": secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if tok.status_code != 200:
            raise RuntimeError(f"sign-in to Microsoft 365 failed ({tok.status_code}): {tok.text[:300]}")
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": mail["to"]}}],
        }
        if name and data is not None:
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": "application/json",
                    "contentBytes": base64.b64encode(data).decode(),
                }
            ]
        sent = http.post(
            f"{GRAPH}/users/{m['mailbox']}/sendMail",
            headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
            json={"message": message, "saveToSentItems": True},
        )
        if sent.status_code != 202:
            raise RuntimeError(f"Microsoft 365 refused the message ({sent.status_code}): {sent.text[:300]}")
        return f"sent by Microsoft 365 as {m['mailbox']}"
    finally:
        if client is None:
            http.close()


# --------------------------------------------------------------------------- the hourly job


class Job:
    """Runs ``usage.run()`` now (after ``delay`` seconds) and then every ``interval``."""

    def __init__(self, make_usage, interval: float, delay: float = 60.0) -> None:
        self._make = make_usage
        self._interval, self._delay = interval, delay
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="usage-report", daemon=True)

    def start(self) -> Job:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        wait = self._delay
        while not self._stop.wait(wait):
            wait = self._interval
            try:
                self._make().run()
            except Exception:  # noqa: BLE001 — logged; the next run tries again
                log.exception("usage job failed")


# --------------------------------------------------------------------------- command line


def _usage_from_settings() -> Usage:
    from .api import build_service
    from .config import get_settings
    from .decisions import make_log

    s = get_settings()
    service = build_service(s)
    return Usage(
        make_store(s.decision_log), make_log(s.decision_log), s, lambda: (service.store, s.instance_base)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="veromesh-usage", description="VeroMesh usage reports")
    sub = ap.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="check a signed report (and its chain)")
    v.add_argument("report")
    v.add_argument("--previous", help="the month before's report, to check the chain")
    v.add_argument("--public-key", help="the install's public key from its licence (base64)")
    r = sub.add_parser("report", help="write a month's signed report (made first if it is due)")
    r.add_argument("--month", required=True, help="YYYY-MM")
    r.add_argument("--out", help="write here (default: standard output)")
    sub.add_parser("run", help="count now, make the reports that are due and send what waits")
    a = ap.parse_args(argv)

    if a.command == "verify":
        doc = json.loads(Path(a.report).read_text())
        prev = json.loads(Path(a.previous).read_text()) if a.previous else None
        problems = verify(doc, a.public_key, prev)
        month = (doc.get("report") or {}).get("month", "?")
        for p in problems:
            print(f"{a.report}: {p}", file=sys.stderr)
        if not problems:
            print(f"{a.report}: the {month} report verifies.")
        return 1 if problems else 0
    usage = _usage_from_settings()
    if a.command == "run":
        print(json.dumps(usage.run(), indent=2))
        return 0
    usage.run()
    doc = usage.store.report(a.month)
    if doc is None:
        print(f"No report for {a.month}: a month gets its report once it has ended.", file=sys.stderr)
        return 1
    text = json.dumps(doc, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
