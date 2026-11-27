"""The licence file: who an install is for, which tier, until when, signed by VeroMesh.

An install checks its licence itself, offline, against VeroMesh's public keys built
into the service (``licence_keys.json``); nothing calls home. VeroMesh's private key
never reaches an install: it is kept in VeroMesh's Azure Key Vault and used by the
issuing command on a signer's machine (decided 22 November).

The licence never limits usage and never gates safety (decided 18 to 22 November).
It sets the tier the usage report measures against, names the install's usage key
(so its reports verify only against that install), and ends on a date:

    licensed    until 60 days before the end
    warning     the last 60 days: a banner, nothing else
    grace       the grace period after the end (30 days, set per licence): everything works
    read-only   after that: nothing new is loaded or changed; reads, decision checks, the
                decision log, attestations, backups and the usage report keep working

An install with no licence runs as unlicensed for 30 days from its first start, then
goes read-only until a licence is loaded. Loading a licence ends read-only at once.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("veromesh.licence")

FORMAT = "veromesh-licence/1"
WARN_DAYS = 60
GRACE_DAYS = 30
UNLICENSED_DAYS = 30
TERM_DAYS = 365
KEYS_FILE = Path(__file__).with_name("licence_keys.json")
STAGES = ("unlicensed", "licensed", "warning", "grace", "read-only")
TIER_NAMES = ("small", "medium", "large", "extra-large")
ENVIRONMENTS = ("production", "test", "development")


class LicenceError(ValueError):
    """A licence that cannot be used, and why."""


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def key_id(public_key: str) -> str:
    """A short name for a public key (base64 raw Ed25519): the first 16 hex of its SHA-256."""
    return hashlib.sha256(base64.b64decode(public_key)).hexdigest()[:16]


def trusted_keys() -> dict[str, str]:
    """VeroMesh's licence public keys built into this release, by key ID."""
    try:
        keys = json.loads(KEYS_FILE.read_text()).get("keys", {})
    except (OSError, ValueError):
        return {}
    return {key_id(k): k for k in keys.values()} if isinstance(keys, dict) else {}


# --------------------------------------------------------------------------- issuing and checking


def _private(pem: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenceError("The licence signing key must be an Ed25519 private key (PEM, PKCS#8).")
    return key


def public_of(pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = _private(pem).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _day(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise LicenceError(f"{field} must be a date, YYYY-MM-DD.") from exc


def check_fields(lic: dict) -> dict:
    """The licence's fields, complete and checked (LicenceError says what is wrong)."""
    need = ("id", "customer", "installId", "tier", "issued", "validFrom", "validUntil", "usageKey")
    missing = [f for f in need if not lic.get(f)]
    if missing:
        raise LicenceError(f"The licence has no {', '.join(missing)}.")
    if lic.get("format") != FORMAT:
        raise LicenceError(f"Unknown licence format {lic.get('format')!r}.")
    if lic["tier"] not in TIER_NAMES:
        raise LicenceError(f"tier must be one of {', '.join(TIER_NAMES)}.")
    start, end = _day(lic["validFrom"], "validFrom"), _day(lic["validUntil"], "validUntil")
    _day(lic["issued"], "issued")
    if end < start:
        raise LicenceError("validUntil is before validFrom.")
    grace = lic.get("graceDays", GRACE_DAYS)
    if not isinstance(grace, int) or grace < 0:
        raise LicenceError("graceDays must be a whole number of days, 0 or more.")
    if lic.get("hosting", "self-hosted") not in ("self-hosted", "hosted"):
        raise LicenceError("hosting must be self-hosted or hosted.")
    if lic.get("environment", "production") not in ENVIRONMENTS:
        raise LicenceError(f"environment must be one of {', '.join(ENVIRONMENTS)}.")
    if not isinstance(lic.get("modules", []), list):
        raise LicenceError("modules must be a list.")
    try:
        if len(base64.b64decode(lic["usageKey"], validate=True)) != 32:
            raise ValueError
    except ValueError as exc:
        raise LicenceError("usageKey must be the install's Ed25519 public key (base64).") from exc
    return lic


def issue(pem: str, **fields) -> dict:
    """A signed licence document. ``fields``: the licence's fields (``format`` is added;
    ``issued`` defaults to today, ``validUntil`` to a year after ``validFrom``)."""
    lic = {
        "format": FORMAT,
        "graceDays": GRACE_DAYS,
        "hosting": "self-hosted",
        "environment": "production",
        "modules": [],
    }
    lic.update({k: v for k, v in fields.items() if v is not None})
    lic.setdefault("issued", datetime.now(UTC).date().isoformat())
    if "validFrom" in lic and "validUntil" not in lic:
        lic["validUntil"] = (_day(lic["validFrom"], "validFrom") + timedelta(days=TERM_DAYS - 1)).isoformat()
    check_fields(lic)
    pub = public_of(pem)
    return {
        "licence": lic,
        "signature": {
            "algorithm": "Ed25519",
            "keyId": key_id(pub),
            "value": base64.b64encode(_private(pem).sign(canonical(lic))).decode(),
        },
    }


def verify(
    doc: dict, *, install_id: str | None = None, usage_key: str | None = None, keys: dict | None = None
) -> dict:
    """The licence in ``doc`` if it is genuine and for this install, else LicenceError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(doc, dict) or not isinstance(doc.get("licence"), dict):
        raise LicenceError("This is not a VeroMesh licence (no licence in it).")
    lic, sig = doc["licence"], doc.get("signature") or {}
    keys = trusted_keys() if keys is None else keys
    public = keys.get(sig.get("keyId", ""))
    if public is None:
        raise LicenceError("The licence is signed with a key this release does not know.")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public)).verify(
            base64.b64decode(sig.get("value") or ""), canonical(lic)
        )
    except (InvalidSignature, ValueError) as exc:
        raise LicenceError(
            "The licence's signature does not verify: it was changed or not issued by VeroMesh."
        ) from exc
    check_fields(lic)
    if install_id is not None and lic["installId"] != install_id:
        raise LicenceError(f"The licence is for install {lic['installId']}, not this one ({install_id}).")
    if usage_key is not None and lic["usageKey"] != usage_key:
        raise LicenceError("The licence names another usage key than this install's.")
    return lic


# --------------------------------------------------------------------------- stages


def _start(d: str) -> datetime:
    return datetime.combine(date.fromisoformat(d), datetime.min.time(), UTC)


def ends_at(lic: dict) -> datetime:
    """When the licence ends: the end of its last day (UTC)."""
    return _start(lic["validUntil"]) + timedelta(days=1)


def stage_of(lic: dict | None, first_start: datetime, now: datetime) -> dict:
    """Where an install stands: its stage, whether it is read-only, and the dates that matter."""
    if lic is None:
        read_only_from = first_start + timedelta(days=UNLICENSED_DAYS)
        ro = now >= read_only_from
        return {
            "stage": "read-only" if ro else "unlicensed",
            "readOnly": ro,
            "licensed": False,
            "readOnlyFrom": read_only_from.isoformat(),
            "message": (
                "No licence: this install is read-only until a licence is loaded."
                if ro
                else f"No licence yet: this install goes read-only on {read_only_from.date().isoformat()} unless one is loaded."
            ),
        }
    end = ends_at(lic)
    grace_end = end + timedelta(days=lic.get("graceDays", GRACE_DAYS))
    days_left = (end - now).days
    if now >= grace_end:
        stage, msg = (
            "read-only",
            f"The licence ended on {lic['validUntil']}: this install is read-only until a renewal is loaded.",
        )
    elif now >= end:
        stage = "grace"
        msg = f"The licence ended on {lic['validUntil']}. Everything works until {grace_end.date().isoformat()}; then the install goes read-only."
    elif now >= end - timedelta(days=WARN_DAYS):
        stage, msg = (
            "warning",
            f"The licence ends on {lic['validUntil']} ({days_left} days). Load a renewal before then.",
        )
    else:
        stage, msg = "licensed", ""
    return {
        "stage": stage,
        "readOnly": stage == "read-only",
        "licensed": True,
        "endsAt": end.isoformat(),
        "readOnlyFrom": grace_end.isoformat(),
        "daysLeft": days_left,
        "message": msg,
    }


# --------------------------------------------------------------------------- the install's licence


class Licensing:
    """The licence of one install: from ``VEROMESH_LICENCE`` (a Secret) and from licences
    uploaded by an administrator (kept in the usage tables), checked and staged.

    ``usage``: the install's usage.Usage (its store, install ID and usage key)."""

    REFRESH_S = 60.0

    def __init__(self, usage, settings, keys: dict | None = None) -> None:
        self.usage = usage
        self.settings = settings
        self.keys = keys
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None

    def first_start(self) -> datetime:
        return datetime.fromisoformat(
            self.usage.store.add_setting("first-start", datetime.now(UTC).isoformat())
        )

    def _identity(self) -> tuple[str, str]:
        return self.usage.install()["id"], self.usage.signer().public_key

    def _candidates(self) -> list[tuple[str, dict]]:
        found = []
        text = self.settings.licence
        if text:
            try:
                found.append(("secret", json.loads(text)))
            except ValueError:
                log.warning("VEROMESH_LICENCE is not JSON; it is ignored")
        for doc in self.usage.store.setting("licences") or []:
            found.append(("uploaded", doc))
        return found

    def licences(self, now: datetime | None = None) -> list[dict]:
        """Every licence held, checked: the good ones and why the others are not used."""
        install_id, usage_key = self._identity()
        out = []
        for origin, doc in self._candidates():
            try:
                lic = verify(doc, install_id=install_id, usage_key=usage_key, keys=self.keys)
                out.append({"origin": origin, "licence": lic, "problem": None})
            except LicenceError as exc:
                out.append(
                    {
                        "origin": origin,
                        "licence": (doc.get("licence") if isinstance(doc, dict) else None),
                        "problem": str(exc),
                    }
                )
        return out

    def active(self, now: datetime | None = None) -> dict | None:
        """The licence in force: of the good ones already begun, the one that ends last."""
        now = now or datetime.now(UTC)
        good = [
            x["licence"]
            for x in self.licences()
            if x["problem"] is None and _start(x["licence"]["validFrom"]) <= now
        ]
        return max(good, key=lambda lic: (lic["validUntil"], lic["issued"]), default=None)

    def status(self, now: datetime | None = None, *, fresh: bool = False) -> dict:
        """The install's stage (kept for a minute: every write asks)."""
        if now is None and not fresh:
            with self._lock:
                if self._cached and time.monotonic() - self._cached[0] < self.REFRESH_S:
                    return self._cached[1]
        at = now or datetime.now(UTC)
        lic = self.active(at)
        out = {**stage_of(lic, self.first_start(), at), "licence": lic}
        if now is None:
            with self._lock:
                self._cached = (time.monotonic(), out)
        return out

    def tier(self) -> str | None:
        lic = self.status()["licence"]
        return lic["tier"] if lic else None

    def load(self, doc: dict) -> dict:
        """Check and keep an uploaded licence; it is in force from its validFrom."""
        install_id, usage_key = self._identity()
        lic = verify(doc, install_id=install_id, usage_key=usage_key, keys=self.keys)
        kept = [
            d
            for d in (self.usage.store.setting("licences") or [])
            if (d.get("licence") or {}).get("id") != lic["id"]
        ]
        self.usage.store.put_setting("licences", kept + [doc])
        log.info("licence %s loaded (%s to %s)", lic["id"], lic["validFrom"], lic["validUntil"])
        return self.status(fresh=True)

    def overview(self) -> dict:
        inst = self.usage.install()
        return {
            "install": {**inst, "usageKey": self.usage.signer().public_key},
            "status": self.status(fresh=True),
            "licences": self.licences(),
            "trustedKeys": sorted(self.keys if self.keys is not None else trusted_keys()),
        }


# --------------------------------------------------------------------------- what read-only stops

# Writes that load or change the model, its bindings or the catalog: refused in read-only.
# Everything else stays open: reads (SPARQL is read-only), decision checks, overrides and
# outcomes, attestations, MCP, the usage report and loading a licence.
_SAFE_WRITES = (
    "/api/v1/decisions/",
    "/api/v1/licence",
    "/api/v1/meetings",  # attesting in meetings, as attesting anywhere, is never stopped
    "/api/v1/notify/",  # telling owners of what was recorded for them
    "/api/v1/teams/",  # their answers in Teams
    "/api/v1/usage",
    "/sparql",
    "/mcp",
)


def blocked(method: str, path: str) -> bool:
    """Whether read-only refuses this request."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return False
    if any(path == p or path.startswith(p.rstrip("/") + "/") or path.startswith(p) for p in _SAFE_WRITES):
        return False
    if path.endswith("/attestations") and path.startswith("/api/v1/"):
        return False  # people confirming facts keeps decisions safe: never stopped
    return True


# --------------------------------------------------------------------------- command line


def _read_key(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="veromesh-licence", description="VeroMesh licences")
    sub = ap.add_subparsers(dest="command", required=True)
    k = sub.add_parser("keygen", help="make a licence signing key pair (once, for VeroMesh's key vault)")
    k.add_argument("--out", required=True, help="where to write the private key (PEM)")
    i = sub.add_parser("issue", help="sign a licence")
    i.add_argument(
        "--key-file", required=True, help="VeroMesh's private key (PEM); '-' reads it from standard input"
    )
    i.add_argument("--id", required=True)
    i.add_argument("--customer", required=True)
    i.add_argument("--install-id", required=True)
    i.add_argument(
        "--usage-key", required=True, help="the install's public key, from its Administration > Licence page"
    )
    i.add_argument("--tier", required=True, choices=TIER_NAMES)
    i.add_argument("--from", dest="valid_from", required=True, help="YYYY-MM-DD")
    i.add_argument("--until", dest="valid_until", help="YYYY-MM-DD (default: a year less a day after --from)")
    i.add_argument("--grace-days", type=int, default=GRACE_DAYS)
    i.add_argument("--hosting", choices=("self-hosted", "hosted"), default="self-hosted")
    i.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    i.add_argument("--out", help="write here (default: standard output)")
    v = sub.add_parser("verify", help="check a licence against this release's keys")
    v.add_argument("licence")
    v.add_argument("--install-id")
    v.add_argument("--usage-key")
    a = ap.parse_args(argv)

    if a.command == "keygen":
        from .usage import Signer

        pem = Signer.generate_pem()
        Path(a.out).write_text(pem)
        Path(a.out).chmod(0o600)
        pub = public_of(pem)
        print(f"Private key written to {a.out}: put it in the key vault and delete this file.")
        print(f"Public key, for licence_keys.json: {pub}")
        print(f"Key ID: {key_id(pub)}")
        return 0
    if a.command == "issue":
        doc = issue(
            _read_key(a.key_file),
            id=a.id,
            customer=a.customer,
            installId=a.install_id,
            usageKey=a.usage_key,
            tier=a.tier,
            validFrom=a.valid_from,
            validUntil=a.valid_until,
            graceDays=a.grace_days,
            hosting=a.hosting,
            environment=a.environment,
        )
        text = json.dumps(doc, indent=2) + "\n"
        if a.out:
            Path(a.out).write_text(text)
        else:
            sys.stdout.write(text)
        return 0
    try:
        lic = verify(json.loads(Path(a.licence).read_text()), install_id=a.install_id, usage_key=a.usage_key)
    except (LicenceError, ValueError) as exc:
        print(f"{a.licence}: {exc}", file=sys.stderr)
        return 1
    print(
        f"{a.licence}: licence {lic['id']} for {lic['customer']} ({lic['installId']}), {lic['tier']}, {lic['validFrom']} to {lic['validUntil']}: genuine."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
