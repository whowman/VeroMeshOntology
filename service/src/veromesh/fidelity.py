"""Source fidelity (step 7a; step 3 of the source-weighting design).

How well this organization keeps a source, measured rather than assumed, per registered
source and per domain the source is authoritative for. A band (high, medium, low) is
worked out from signals VeroMesh already records, over the last 90 days:

- ``corrections``: the share of its facts in the domain that owners disputed or stewards
  corrected;
- ``lateness``: how many days after a change took effect the source reported it (a
  termination entered late: the end date before the time it was recorded; a change whose
  effective date is before the source's previous statement);
- ``gaps``: the share of its facts missing a field decisions need (a person's employee ID
  or manager, a system's owner, an authority's holder);
- ``contradicted``: the share of its facts an evidence source contradicts (a conflict);
- ``loads``: refused loads, and days since the last good one.

Until a source has ``MIN_FACTS`` facts in the domain and a committed load, its band is the
**declared** rating given when it was registered (``fidelity``), which can
come from the governance maturity assessment's answers for the domain. With none
declared, a source counts as high until it is measured (how VeroMesh behaved before).

Fidelity never makes an unsuited source authoritative. It changes how far VeroMesh relies
on the authoritative one: low makes the decision check consult the source's owner rather
than allow; medium and low shorten attestation review cycles (half and a quarter) and put
the source's facts first in the review queue; a known HR lateness turns an account
disabled before HR reports a leaver into an urgent finding for HR. Among sources of one
category, the declared rating breaks a tie before recency does.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

from . import domains
from . import provenance as P
from .modelcache import cached
from .rdf_store import RDFStore

BANDS = ("high", "medium", "low")
DEFAULT_DECLARED = "high"  # unset: trusted as before fidelity, until measured
WINDOW_DAYS = 90
MIN_FACTS = 20
#: Review cycles: the cadence is multiplied by this for a source's band.
REVIEW_FACTOR = {"high": 1.0, "medium": 0.5, "low": 0.25}
#: Fields a decision needs, per domain and type: missing ones are gaps.
GAP_FIELDS = {
    "employment": {"people": ["employeeId"]},
    "organization": {"people": ["reportsTo"]},
    "systems": {"systems": ["businessOwner", "technicalOwner"]},
    "authority-in-force": {"authorities": ["holder", "actionsCovered"]},
    "access": {"agents": ["signInClientId"]},
}
#: (medium above, low above) per signal.
THRESHOLDS = {
    "corrections": (0.02, 0.10),
    "lateness": (1, 7),  # median days (medium), 90th percentile days (low)
    "gaps": (0.05, 0.20),
    # Evidence often differs on harmless details (a nickname): on its own this lowers a
    # source to medium at most; a correction or dispute is what shows evidence was right.
    "contradicted": (0.25, 1.01),
    "refusedLoads": (0, 2),
    "daysSinceGoodLoad": (3, 14),
}


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _date_of(s: str | None):
    d = _dt(s if s and "T" in str(s) else (f"{s}T00:00:00+00:00" if s else None))
    return d.date() if d else None


_LAST: dict[int, dict] = {}  # the last result per store, for callers that may use a stale one


def sources(store: RDFStore) -> dict[str, dict]:
    """Per registered source: {domain: {band, measured, declared, signals, reasons}}."""
    out = cached(store, "fidelity", lambda: _compute(store))
    _LAST[id(store)] = out
    return out


def band(store: RDFStore, source: str | None, domain: str | None = None, *, stale_ok: bool = False) -> str:
    """A source's band for a domain (its worst over its domains without one); unknown: declared.
    ``stale_ok``: the last bands worked out will do (an attestation's review cycle: a 90-day
    measure need not be recomputed for each of many attestations in a row)."""
    if not source:
        return DEFAULT_DECLARED
    known = _LAST.get(id(store)) if stale_ok else None
    per = (known if known is not None else sources(store)).get(source) or {}
    if domain and domain in per:
        return per[domain]["band"]
    if per:
        return max((d["band"] for d in per.values()), key=BANDS.index)
    return (P.registry(store).get(source) or {}).get("fidelity") or DEFAULT_DECLARED


def _compute(store: RDFStore) -> dict[str, dict]:
    doms = domains.activate(store)
    reg = P.registry(store)
    now = datetime.now(UTC)
    since = now - timedelta(days=WINDOW_DAYS)
    current = P.read_claims(store)
    history = P.read_claims(store, current=False)
    by_source: dict[str, list] = {}
    for c in history:
        by_source.setdefault(c.source, []).append(c)
    atts: dict[tuple, list] = {}
    for a in P.read_attestations(store):
        atts.setdefault((a.type, a.id), []).append(a)
    resolved, _ = P._current(store)
    steward = {c.key: c for c in current if c.source == P.STEWARD_SOURCE}
    from .changesets import list_changesets

    out: dict[str, dict] = {}
    for name, info in sorted(reg.items()):
        cat = info["category"]
        if cat in ("steward", "ended"):
            continue
        declared = info.get("fidelity") or DEFAULT_DECLARED
        mine = [c for c in current if c.source == name]
        hist = sorted(
            (c for c in by_source.get(name, []) if (_dt(c.generated_at) or now) >= since),
            key=lambda c: c.generated_at or "",
        )
        runs = list_changesets(store, source=name, limit=200)
        recent = [r for r in runs if (_dt(r.get("createdAt")) or now) >= since]
        refused = sum(1 for r in recent if r.get("status") in ("invalid", "failed"))
        good = [r for r in runs if r.get("status") == "committed"]
        last_good = _dt(good[0].get("createdAt")) if good else None
        per: dict[str, dict] = {}
        for dname, d in doms.items():
            if cat not in (d.get("authoritative") or []):
                continue
            fields = d.get("fields") or {}
            facts = [c for c in mine if c.type in fields and any(f in c.values for f in fields[c.type])]
            n = len(facts)
            sig: dict = {"facts": n}
            # Corrections and disputes on its facts in the domain.
            disputed = 0
            for c in facts:
                fs = [f for f in fields[c.type] if f in c.values]
                if any(P.verdict_on(atts.get(c.key, []), c, f) == "disputed" for f in fs):
                    disputed += 1
                    continue
                st = steward.get(c.key)
                if st and any(
                    f in st.values and not P._same_value(st.values[f], c.values.get(f)) for f in fs
                ):
                    disputed += 1
            sig["corrections"] = round(disputed / n, 4) if n else 0.0
            # Lateness: an end reported after it took effect; a change dated before the
            # source's previous statement of the entity.
            late: list[float] = []
            prev: dict[tuple, P.Claim] = {}
            for c in hist:
                if c.type not in fields:
                    continue
                obs = _dt(c.observed_at) or _dt(c.generated_at)
                end = _date_of(c.valid_until)
                if end and obs and obs.date() > end:
                    late.append((obs.date() - end).days)
                p = prev.get(c.key)
                start = _date_of(c.valid_from)
                if (
                    p is not None
                    and start
                    and obs
                    and c.valid_from
                    != p.valid_from  # a new effective date, already passed when the source last spoke
                    and (_dt(p.observed_at) or obs).date() >= start
                ):
                    late.append((obs.date() - start).days)
                prev[c.key] = c
            sig["lateness"] = (
                {"count": len(late), "medianDays": statistics.median(late), "p90Days": _p90(late)}
                if late
                else {"count": 0, "medianDays": 0, "p90Days": 0}
            )
            # Gaps in fields decisions need.
            need = GAP_FIELDS.get(dname, {})
            gap_facts = [c for c in mine if c.type in need]  # a missing field leaves no domain field at all
            gaps = sum(1 for c in gap_facts if any(P.empty(c.values.get(f)) for f in need[c.type]))
            sig["gaps"] = round(gaps / len(gap_facts), 4) if gap_facts else 0.0
            # Contradicted by evidence: another source states a different value.
            contra = 0
            for c in facts:
                r = resolved.get(c.key)
                if r is None:
                    continue
                for f in fields[c.type]:
                    if f in c.values and any(
                        o.source != name and not P._same_value(o.values.get(f), c.values.get(f))
                        for o in r.conflicts.get(f, [])
                    ):
                        contra += 1
                        break
            sig["contradicted"] = round(contra / n, 4) if n else 0.0
            sig["refusedLoads"] = refused
            sig["daysSinceGoodLoad"] = (now - last_good).days if last_good else None
            measured = n >= MIN_FACTS and last_good is not None
            got, reasons = (
                _band(sig) if measured else (declared, [f"declared {declared}: too little history yet"])
            )
            per[dname] = {
                "band": got,
                "measured": measured,
                "declared": declared,
                "signals": sig,
                "reasons": reasons,
            }
        out[name] = per
    return out


def _p90(xs: list[float]) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def _band(sig: dict) -> tuple[str, list[str]]:
    """The band and, signal by signal, why (the reasons a source's owner is told)."""
    low, med = [], []

    def check(label, value, key, unit=""):
        if value is None:
            return
        m, lo = THRESHOLDS[key]
        shown = f"{value:.0%}" if isinstance(value, float) and value <= 1 and not unit else f"{value}{unit}"
        if value > lo:
            low.append(f"{label} {shown}")
        elif value > m:
            med.append(f"{label} {shown}")

    check("corrected or disputed", sig["corrections"], "corrections")
    lat = sig["lateness"]
    if lat["count"]:
        if lat["p90Days"] > THRESHOLDS["lateness"][1]:
            low.append(f"changes reported up to {lat['p90Days']} days late (90th percentile)")
        elif lat["medianDays"] > THRESHOLDS["lateness"][0]:
            med.append(f"changes reported a median {lat['medianDays']} days late")
    check("missing fields decisions need", sig["gaps"], "gaps")
    check("contradicted by evidence", sig["contradicted"], "contradicted")
    check("refused loads:", sig["refusedLoads"], "refusedLoads", unit="")
    check("days since the last good load:", sig["daysSinceGoodLoad"], "daysSinceGoodLoad", unit="")
    if low:
        return "low", low + med
    if med:
        return "medium", med
    return "high", []


def reasons(store: RDFStore, source: str, domain: str) -> list[str]:
    """Signal by signal, why a source has its band for a domain (from the last bands worked out)."""
    known = _LAST.get(id(store))
    per = (known if known is not None else sources(store)).get(source) or {}
    return list((per.get(domain) or {}).get("reasons") or [])


def forget(store: RDFStore) -> None:
    """Drop the last bands kept for stale reads (a source registered or re-rated)."""
    _LAST.pop(id(store), None)


def lateness_days(store: RDFStore, source: str, domain: str = "employment") -> float:
    """The median days a source reports changes late in a domain (0 when none seen)."""
    d = (sources(store).get(source) or {}).get(domain)
    return float(d["signals"]["lateness"]["medianDays"]) if d else 0.0
