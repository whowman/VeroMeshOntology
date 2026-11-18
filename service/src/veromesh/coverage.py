"""The data element inventory: how complete it is, and where the gaps are.

A business data element is *covered* when the organization knows what it is,
who answers for it and where it lives:

* defined: it has a definition;
* owned: an owner role and a steward role;
* sourced: an authoritative source system;
* classified: a sensitivity level;
* bound: at least one physical column in the catalog holds it (a column binding);
* attested: an owner or steward has confirmed the facts above, and they still hold.

Critical data elements must be owned, stewarded and sourced (a validation rule);
here the same checks are counted so a steward can see progress toward Gate 1
("one value stream's critical data elements inventoried with owners").
Everything is read from the effective model; attestation status from provenance.
"""

from __future__ import annotations

from . import changesets
from . import provenance as P
from .rdf_store import RDFStore

CHECKS = ("defined", "owned", "sourced", "classified", "bound", "attested")
# Fields an attestation must cover for the element to count as attested.
ATTESTED_FIELDS = ("ownerRole", "stewardRole", "authoritativeSource", "critical")


def _attested(resolved: P.Resolved | None, atts: list[P.Attestation]) -> bool:
    if resolved is None:
        return False
    fields = [f for f in ATTESTED_FIELDS if f in resolved.winners]
    if not fields:
        return False
    return all(P.field_status(atts, resolved.winners[f], f)[0] == "attested" for f in fields)


def inventory(
    store: RDFStore, base: str, *, data_product: str | None = None, critical_only: bool = False
) -> dict:
    """Each data element with its checks, and the totals.

    ``data_product``: only the elements that data product contains (one value
    stream's inventory). ``critical_only``: only critical data elements."""
    elements = changesets.list_entities(store, "data-elements", base)
    bindings = changesets.list_entities(store, "column-bindings", base)
    if data_product is not None:
        products = {p["id"]: p for p in changesets.list_entities(store, "data-products", base)}
        if data_product not in products:
            raise LookupError(f"No data product '{data_product}' in the model.")
        wanted = set(products[data_product].get("containsDataElements") or [])
        elements = [e for e in elements if e["id"] in wanted]
    if critical_only:
        elements = [e for e in elements if e.get("critical") is True]

    columns: dict[str, list[str]] = {}
    for b in bindings:
        if b.get("dataElement") and b.get("column"):
            columns.setdefault(b["dataElement"], []).append(b["column"])
    resolved, atts = P._current(store)

    rows = []
    for e in sorted(elements, key=lambda e: (not e.get("critical"), e.get("name") or e["id"])):
        checks = {
            "defined": bool(e.get("definition")),
            "owned": bool(e.get("ownerRole")) and bool(e.get("stewardRole")),
            "sourced": bool(e.get("authoritativeSource")),
            "classified": bool(e.get("sensitivity")),
            "bound": bool(columns.get(e["id"])),
            "attested": _attested(resolved.get(("data-elements", e["id"])), atts),
        }
        rows.append(
            {
                "id": e["id"],
                "name": e.get("name") or e["id"],
                "critical": e.get("critical") is True,
                "ownerRole": e.get("ownerRole"),
                "stewardRole": e.get("stewardRole"),
                "authoritativeSource": e.get("authoritativeSource"),
                "sensitivity": e.get("sensitivity"),
                "columns": sorted(columns.get(e["id"], [])),
                "checks": checks,
                "gaps": [c for c in CHECKS if not checks[c]],
            }
        )

    def totals(subset: list[dict]) -> dict:
        n = len(subset)
        out = {"elements": n}
        for c in CHECKS:
            k = sum(1 for r in subset if r["checks"][c])
            out[c] = {"count": k, "share": round(k / n, 3) if n else None}
        out["complete"] = sum(1 for r in subset if not r["gaps"])
        return out

    return {
        "scope": {"dataProduct": data_product, "criticalOnly": critical_only},
        "checks": {
            "defined": "has a definition",
            "owned": "has an owner role and a steward role",
            "sourced": "has an authoritative source system",
            "classified": "has a sensitivity level",
            "bound": "at least one catalog column is bound to it",
            "attested": "its owner, steward, source and critical flag are attested and current",
        },
        "all": totals(rows),
        "critical": totals([r for r in rows if r["critical"]]),
        "elements": rows,
    }
