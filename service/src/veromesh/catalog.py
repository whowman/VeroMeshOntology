"""Publish the organizational context of data products into the catalog (DataHub).

A data product in the model that names its catalog URN gets VeroMesh's view of
it written onto the catalog's own data product, as custom properties prefixed
``veromesh.``: who owns and stewards it, which capabilities produce and consume
it, which KPIs are computed from it, which agents consume it, what it feeds and
is fed by, the systems it runs on, its data elements, sensitivity and the
policies that bound it. People browsing DataHub then see who to ask and what
depends on the product without leaving the catalog.

Only roles, never people, are published: the catalog's readers are not checked
for need to know. Properties the catalog or other tools set are left alone; the
``veromesh.`` ones are replaced on every publish, so a link removed from the
model disappears from the catalog too. Values come from the effective model.
"""

from __future__ import annotations

from . import changesets
from .datahub import DataHubPort
from .rdf_store import RDFStore

PREFIX = "veromesh."

# (property, label) in the order DataHub shows them.
_PROPS = (
    ("id", "VeroMesh ID"),
    ("ownerRole", "owner role"),
    ("stewardRole", "steward role"),
    ("producedBy", "produced by capabilities"),
    ("consumedBy", "consumed by capabilities"),
    ("kpis", "KPIs computed from it"),
    ("agents", "agents consuming it"),
    ("feeds", "feeds data products"),
    ("fedBy", "fed by data products"),
    ("runsOn", "runs on systems"),
    ("dataElements", "data elements"),
    ("sensitivity", "sensitivity"),
    ("boundBy", "bounded by policies"),
)


def _names(store: RDFStore, base: str, type_: str) -> dict[str, str]:
    return {e["id"]: e.get("name") or e["id"] for e in changesets.list_entities(store, type_, base)}


def _pointing_at(rows: list[dict], field: str, dp: str) -> list[str]:
    return sorted(r["id"] for r in rows if dp in (r.get(field) or []))


def context(store: RDFStore, base: str) -> list[dict]:
    """Every data product with VeroMesh's context, as display names."""
    products = changesets.list_entities(store, "data-products", base)
    caps = changesets.list_entities(store, "capabilities", base)
    kpis = changesets.list_entities(store, "kpis", base)
    agents = changesets.list_entities(store, "agents", base)
    policies = changesets.list_entities(store, "policies", base)
    names = {t: _names(store, base, t) for t in ("roles", "systems", "data-elements", "data-products")} | {
        "capabilities": {c["id"]: c.get("name") or c["id"] for c in caps},
        "kpis": {k["id"]: k.get("name") or k["id"] for k in kpis},
        "agents": {a["id"]: a.get("name") or a["id"] for a in agents},
        "policies": {p["id"]: p.get("name") or p["id"] for p in policies},
    }

    def show(type_: str, ids) -> str:
        if not ids:
            return ""
        ids = [ids] if isinstance(ids, str) else ids
        return "; ".join(names[type_].get(i, i) for i in ids)

    out = []
    for dp in products:
        pid = dp["id"]
        values = {
            "id": pid,
            "ownerRole": show("roles", dp.get("ownerRole")),
            "stewardRole": show("roles", dp.get("stewardRole")),
            "producedBy": show("capabilities", _pointing_at(caps, "producesDataProducts", pid)),
            "consumedBy": show("capabilities", _pointing_at(caps, "consumesDataProducts", pid)),
            "kpis": show("kpis", _pointing_at(kpis, "computedFromDataProducts", pid)),
            "agents": show("agents", _pointing_at(agents, "consumesDataProducts", pid)),
            "feeds": show("data-products", dp.get("feedsDataProducts")),
            "fedBy": show("data-products", _pointing_at(products, "feedsDataProducts", pid)),
            "runsOn": show("systems", dp.get("runsOnSystems")),
            "dataElements": show("data-elements", dp.get("containsDataElements")),
            "sensitivity": dp.get("sensitivity") or "",
            "boundBy": show("policies", _pointing_at(policies, "bounds", pid)),
        }
        props = {PREFIX + label: values[key] for key, label in _PROPS if values[key]}
        out.append(
            {
                "id": pid,
                "name": dp.get("name") or pid,
                "catalogUrn": dp.get("catalogUrn"),
                "properties": props,
            }
        )
    return out


def publish_data_products(
    store: RDFStore, base: str, datahub: DataHubPort, ids: list[str] | None = None
) -> dict:
    """Write each data product's context (or only those in ``ids``) onto its catalog entry.
    Returns what happened to each."""
    published, not_in_catalog, no_urn = [], [], []
    rows = context(store, base)
    if ids is not None:
        unknown = sorted(set(ids) - {dp["id"] for dp in rows})
        if unknown:
            raise LookupError(f"No data product {', '.join(unknown)} in the model.")
        rows = [dp for dp in rows if dp["id"] in ids]
    for dp in rows:
        if not dp["catalogUrn"]:
            no_urn.append(dp["id"])
            continue
        if datahub.set_custom_properties(dp["catalogUrn"], dp["properties"], prefix=PREFIX):
            published.append({"id": dp["id"], "catalogUrn": dp["catalogUrn"], "properties": dp["properties"]})
        else:
            not_in_catalog.append({"id": dp["id"], "catalogUrn": dp["catalogUrn"]})
    return {
        "published": published,
        "notInCatalog": not_in_catalog,
        "noCatalogUrn": no_urn,
        "summary": f"{len(published)} published, {len(not_in_catalog)} not found in the catalog, "
        f"{len(no_urn)} without a catalog URN",
    }
