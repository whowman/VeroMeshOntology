"""The decision log: what was decided for an agent, on what basis, and what happened next.

Every entry is appended, never changed: a decision (allow, consult or refuse,
with its reasons), and later entries that refer to it (a person's override,
the outcome). Each entry carries the hash of the one before it, so removing or
editing an entry anywhere breaks the chain from that point on, and
``verify()`` says where.

Two stores behind one interface:

* ``memory``: for development and tests; gone when the process stops.
* ``postgresql://user:password@host:port/database``: a table the service
  creates on first use, with a trigger that refuses UPDATE, DELETE and
  TRUNCATE. Appends take a transaction-scoped advisory lock, so concurrent
  writers (several API replicas) still form one chain.

Hashing: SHA-256 of the entry as canonical JSON (keys sorted, no spaces,
UTF-8), without its own ``hash`` and with ``prevHash``. Amounts are strings,
never floats, so the bytes hashed are the bytes stored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

log = logging.getLogger("veromesh.decisions")

GENESIS = "0" * 64
KINDS = ("decision", "override", "outcome")
DECISION_OUTCOMES = ("allow", "consult", "refuse")
# Fields an entry may carry, beyond those the log assigns (id, seq, recordedAt, prevHash, hash).
FIELDS = (
    "kind",
    "refersTo",
    "subject",
    "agent",
    "principal",
    "action",
    "amount",
    "currency",
    "authority",
    "outcome",
    "reasons",
    "consult",
    "basis",
    "note",
)


class InvalidEntry(ValueError):
    pass


def canonical(entry: dict) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


def check(fields: dict) -> dict:
    """Validate what a caller may write and normalise it; the log adds the rest."""
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise InvalidEntry(f"Unknown field(s): {', '.join(unknown)}.")
    kind = fields.get("kind")
    if kind not in KINDS:
        raise InvalidEntry(f"kind must be one of {', '.join(KINDS)}.")
    if kind == "decision":
        if fields.get("outcome") not in DECISION_OUTCOMES:
            raise InvalidEntry(f"A decision's outcome is one of {', '.join(DECISION_OUTCOMES)}.")
        if fields.get("refersTo"):
            raise InvalidEntry(
                "A decision refers to nothing earlier; overrides and outcomes refer to a decision."
            )
    elif not fields.get("refersTo"):
        raise InvalidEntry(f"An {kind} entry must refer to the decision it is about (refersTo).")
    if not fields.get("outcome"):
        raise InvalidEntry("outcome is required.")
    if not fields.get("subject"):
        raise InvalidEntry("subject (who asked or acted, as signed in) is required.")
    out = {k: v for k, v in fields.items() if v is not None}
    if "amount" in out:
        out["amount"] = str(out["amount"])  # never a float in the hashed bytes
    for k in ("reasons", "consult"):
        if k in out and not isinstance(out[k], list):
            raise InvalidEntry(f"{k} must be a list.")
    return out


@dataclass
class Verification:
    ok: bool
    entries: int
    last_hash: str
    broken_at: int | None = None
    problem: str | None = None

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok, "entries": self.entries, "lastHash": self.last_hash}
        if not self.ok:
            d.update(brokenAtSeq=self.broken_at, problem=self.problem)
        return d


def verify_chain(entries: list[dict]) -> Verification:
    prev = GENESIS
    for i, e in enumerate(entries, start=1):
        if e.get("seq") != i:
            return Verification(False, i - 1, prev, e.get("seq"), f"expected entry {i}, found {e.get('seq')}")
        if e.get("prevHash") != prev:
            return Verification(False, i - 1, prev, i, "does not follow the entry before it")
        if entry_hash(e) != e.get("hash"):
            return Verification(False, i - 1, prev, i, "its contents do not match its hash")
        prev = e["hash"]
    return Verification(True, len(entries), prev)


class DecisionLog(Protocol):
    def append(self, fields: dict) -> dict: ...
    def get(self, entry_id: str) -> dict | None: ...
    def find(
        self,
        *,
        agent: str | None = None,
        principal: str | None = None,
        action: str | None = None,
        kind: str | None = None,
        refers_to: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[dict]: ...
    def verify(self) -> Verification: ...


def _new_entry(fields: dict, seq: int, prev: str) -> dict:
    entry = {
        **check(fields),
        "id": str(uuid.uuid4()),
        "seq": seq,
        "recordedAt": datetime.now(UTC).isoformat(timespec="microseconds"),
        "prevHash": prev,
    }
    entry["hash"] = entry_hash(entry)
    return entry


def _matches(e: dict, agent, principal, action, kind, refers_to, after_seq) -> bool:
    return (
        e["seq"] > after_seq
        and (agent is None or e.get("agent") == agent)
        and (principal is None or e.get("principal") == principal)
        and (action is None or e.get("action") == action)
        and (kind is None or e.get("kind") == kind)
        and (refers_to is None or e.get("refersTo") == refers_to)
    )


class MemoryLog:
    """Development and tests only: nothing survives a restart."""

    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._lock = threading.Lock()

    def append(self, fields: dict) -> dict:
        with self._lock:
            if fields.get("refersTo") and not any(e["id"] == fields["refersTo"] for e in self._entries):
                raise InvalidEntry(f"No entry {fields['refersTo']} to refer to.")
            prev = self._entries[-1]["hash"] if self._entries else GENESIS
            entry = _new_entry(fields, len(self._entries) + 1, prev)
            self._entries.append(entry)
            return dict(entry)

    def get(self, entry_id: str) -> dict | None:
        return next((dict(e) for e in self._entries if e["id"] == entry_id), None)

    def find(
        self, *, agent=None, principal=None, action=None, kind=None, refers_to=None, after_seq=0, limit=100
    ):
        hits = [e for e in self._entries if _matches(e, agent, principal, action, kind, refers_to, after_seq)]
        return [dict(e) for e in hits[:limit]]

    def verify(self) -> Verification:
        return verify_chain(self._entries)


SCHEMA = """
CREATE TABLE IF NOT EXISTS veromesh_decision_log (
    seq         bigint PRIMARY KEY,
    id          uuid NOT NULL UNIQUE,
    recorded_at timestamptz NOT NULL,
    kind        text NOT NULL,
    refers_to   uuid REFERENCES veromesh_decision_log (id),
    agent       text,
    principal   text,
    action      text,
    entry       jsonb NOT NULL,
    hash        text NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS veromesh_decision_log_agent ON veromesh_decision_log (agent, seq);
CREATE INDEX IF NOT EXISTS veromesh_decision_log_principal ON veromesh_decision_log (principal, seq);
CREATE INDEX IF NOT EXISTS veromesh_decision_log_refers_to ON veromesh_decision_log (refers_to);
CREATE OR REPLACE FUNCTION veromesh_decision_log_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the decision log is append-only: % is not allowed', TG_OP;
END $$;
DROP TRIGGER IF EXISTS veromesh_decision_log_no_change ON veromesh_decision_log;
CREATE TRIGGER veromesh_decision_log_no_change BEFORE UPDATE OR DELETE ON veromesh_decision_log
    FOR EACH ROW EXECUTE FUNCTION veromesh_decision_log_append_only();
DROP TRIGGER IF EXISTS veromesh_decision_log_no_truncate ON veromesh_decision_log;
CREATE TRIGGER veromesh_decision_log_no_truncate BEFORE TRUNCATE ON veromesh_decision_log
    FOR EACH STATEMENT EXECUTE FUNCTION veromesh_decision_log_append_only();
"""

# One advisory lock key for every writer of this log (any number from a fixed source).
LOCK_KEY = int.from_bytes(hashlib.sha256(b"veromesh_decision_log").digest()[:8], "big", signed=True)


class PostgresLog:
    def __init__(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme not in ("postgresql", "postgres"):
            raise ValueError("The decision log URL must start with postgresql://")
        self._params = {
            "user": unquote(u.username or ""),
            "password": unquote(u.password or "") or None,
            "host": u.hostname or "localhost",
            "port": u.port or 5432,
            "database": (u.path or "/").lstrip("/") or None,
        }
        query = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        if query.get("sslmode") in ("require", "verify-ca", "verify-full"):
            import ssl

            ctx = ssl.create_default_context()
            if query["sslmode"] == "require":
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._params["ssl_context"] = ctx
        self._lock = threading.Lock()
        self._conn = None
        self._ready = False

    def _connect(self):
        import pg8000.native  # BSD-3-Clause

        if self._conn is None:
            self._conn = pg8000.native.Connection(**{k: v for k, v in self._params.items() if v is not None})
        if not self._ready:
            self._conn.run("BEGIN")
            self._conn.run("SELECT pg_advisory_xact_lock(:k)", k=LOCK_KEY)
            for stmt in _statements(SCHEMA):
                self._conn.run(stmt)
            self._conn.run("COMMIT")
            self._ready = True
        return self._conn

    def _run(self, fn):
        with self._lock:
            for attempt in (1, 2):
                try:
                    return fn(self._connect())
                except Exception as exc:
                    # A dropped connection: reconnect once. Anything else (or a second failure) is raised.
                    broken = self._conn is None or type(exc).__name__ in ("InterfaceError", "ConnectionError")
                    if self._conn is not None:
                        try:
                            self._conn.run("ROLLBACK")
                        except Exception:
                            try:
                                self._conn.close()
                            except Exception:
                                pass
                            self._conn = None
                    if attempt == 2 or not broken:
                        raise

    def append(self, fields: dict) -> dict:
        def go(c):
            c.run("BEGIN")
            try:
                c.run("SELECT pg_advisory_xact_lock(:k)", k=LOCK_KEY)
                if fields.get("refersTo"):
                    if not c.run(
                        "SELECT 1 FROM veromesh_decision_log WHERE id = :i", i=_uuid(fields["refersTo"])
                    ):
                        raise InvalidEntry(f"No entry {fields['refersTo']} to refer to.")
                last = c.run("SELECT seq, hash FROM veromesh_decision_log ORDER BY seq DESC LIMIT 1")
                seq, prev = (last[0][0] + 1, last[0][1]) if last else (1, GENESIS)
                entry = _new_entry(fields, seq, prev)
                c.run(
                    "INSERT INTO veromesh_decision_log"
                    " (seq, id, recorded_at, kind, refers_to, agent, principal, action, entry, hash)"
                    " VALUES (:seq, CAST(:id AS uuid), CAST(:at AS timestamptz), :kind, CAST(:ref AS uuid),"
                    " :agent, :principal, :action, CAST(:entry AS jsonb), :hash)",
                    seq=seq,
                    id=entry["id"],
                    at=entry["recordedAt"],
                    kind=entry["kind"],
                    ref=entry.get("refersTo"),
                    agent=entry.get("agent"),
                    principal=entry.get("principal"),
                    action=entry.get("action"),
                    entry=json.dumps(entry),
                    hash=entry["hash"],
                )
                c.run("COMMIT")
                return entry
            except Exception:
                c.run("ROLLBACK")
                raise

        return self._run(go)

    def get(self, entry_id: str) -> dict | None:
        try:
            uid = _uuid(entry_id)
        except InvalidEntry:
            return None
        rows = self._run(
            lambda c: c.run("SELECT entry FROM veromesh_decision_log WHERE id = CAST(:i AS uuid)", i=uid)
        )
        return _entry(rows[0][0]) if rows else None

    def find(
        self, *, agent=None, principal=None, action=None, kind=None, refers_to=None, after_seq=0, limit=100
    ):
        where, params = ["seq > :after"], {"after": after_seq, "limit": max(1, min(limit, 1000))}
        for col, val in (("agent", agent), ("principal", principal), ("action", action), ("kind", kind)):
            if val is not None:
                where.append(f"{col} = :{col}")
                params[col] = val
        if refers_to is not None:
            where.append("refers_to = CAST(:ref AS uuid)")
            params["ref"] = _uuid(refers_to)
        sql = f"SELECT entry FROM veromesh_decision_log WHERE {' AND '.join(where)} ORDER BY seq LIMIT :limit"
        return [_entry(r[0]) for r in self._run(lambda c: c.run(sql, **params))]

    def verify(self) -> Verification:
        rows = self._run(lambda c: c.run("SELECT entry FROM veromesh_decision_log ORDER BY seq"))
        return verify_chain([_entry(r[0]) for r in rows])


def _entry(value) -> dict:
    return json.loads(value) if isinstance(value, str) else value


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise InvalidEntry(f"'{value}' is not an entry ID.") from exc


def _statements(sql: str) -> list[str]:
    """Split the schema into statements, keeping $$-quoted function bodies whole."""
    out, cur, in_body = [], [], False
    for line in sql.strip().splitlines():
        cur.append(line)
        if line.count("$$") % 2:
            in_body = not in_body
        if not in_body and line.rstrip().endswith(";"):
            out.append("\n".join(cur).strip().rstrip(";"))
            cur = []
    if "".join(cur).strip():
        out.append("\n".join(cur).strip())
    return out


def make_log(spec: str) -> DecisionLog:
    if spec == "memory":
        return MemoryLog()
    if spec.startswith(("postgresql://", "postgres://")):
        return PostgresLog(spec)
    raise ValueError("VEROMESH_DECISION_LOG must be 'memory' or a postgresql:// URL.")
