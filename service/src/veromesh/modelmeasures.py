"""Measures of the model itself (the position paper's M2 and M6), from provenance.

- **M2, share of facts sourced automatically:** of the fields VeroMesh holds now, the share
  that came from a system through its API (sources in a system category: HR, identity, ERP,
  CMDB, catalog) rather than typed by hand (a spreadsheet, the governance workbook, a
  steward's correction). It shows the model is built from systems that already exist, not
  re-keyed.
- **M6, change latency:** per source, the time from when a fact changed in the source (the
  record's own timestamp: ServiceNow's ``sys_updated_on``, LeanIX's ``updatedAt``, Databricks'
  ``updated_at``, dbt's build time) to when VeroMesh recorded the change. Median and 90th
  percentile, in hours. Only changes count: the source's first load is left out, because a
  record's age when VeroMesh first sees it is not a delay. A connector whose source gives no
  record timestamp stamps the load time, so its latency reads as about zero.

Both are counted over current claims (one per entity per source), each field once per source.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median

from . import provenance as P
from .rdf_store import RDFStore

#: Categories whose facts come from a system through its API.
SYSTEM_CATEGORIES = ("hr", "iam", "erp", "cmdb", "catalog")
#: Categories of facts entered by people.
MANUAL_CATEGORIES = ("spreadsheet", "governance", "steward")


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return xs[i]


def model_measures(store: RDFStore) -> dict:
    sources: dict[str, dict] = {}
    for c in P.read_claims(store):
        if c.source == P.ENDED_SOURCE:
            continue
        s = sources.setdefault(c.source, {"category": c.category, "entities": 0, "fields": 0, "lags": []})
        s["entities"] += 1
        s["fields"] += sum(1 for k, v in c.values.items() if k != "id" and v not in (None, "", []))
        seen, rec = _ts(c.observed_at), _ts(c.generated_at)
        if seen and rec:
            if seen.tzinfo is None and rec.tzinfo is not None:
                seen = seen.replace(tzinfo=rec.tzinfo)
            if rec.tzinfo is None and seen.tzinfo is not None:
                rec = rec.replace(tzinfo=seen.tzinfo)
            s["lags"].append((rec, max(0.0, (rec - seen).total_seconds() / 3600)))
    for s in sources.values():  # changes only: leave out what the source's first load recorded
        first = min((r for r, _ in s["lags"]), default=None)
        s["lags"] = [lag for r, lag in s["lags"] if r != first]
    auto = sum(s["fields"] for s in sources.values() if s["category"] in SYSTEM_CATEGORIES)
    manual = sum(s["fields"] for s in sources.values() if s["category"] not in SYSTEM_CATEGORIES)
    total = auto + manual
    rows = []
    for name, s in sorted(sources.items()):
        lags = s.pop("lags")
        rows.append(
            {
                "source": name,
                **s,
                "automatic": s["category"] in SYSTEM_CATEGORIES,
                "latencyHours": (
                    {"median": round(median(lags), 2), "p90": round(_pct(lags, 0.9), 2), "claims": len(lags)}
                    if lags
                    else None
                ),
            }
        )
    return {
        "M2": {
            "name": "Share of facts sourced automatically",
            "value": round(auto / total, 4) if total else None,
            "automaticFields": auto,
            "manualFields": manual,
            "systemCategories": list(SYSTEM_CATEGORIES),
        },
        "M6": {
            "name": "Change latency, source to model",
            "unit": "hours",
            "bySource": {r["source"]: r["latencyHours"] for r in rows if r["latencyHours"]},
        },
        "sources": rows,
    }
