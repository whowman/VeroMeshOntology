"""Databricks Unity Catalog: the lakehouse's tables, owners, stewards, sensitivity and lineage.

What it reads, through Databricks' public REST APIs:

* Unity Catalog: ``/api/2.1/unity-catalog/catalogs``, ``schemas`` and ``tables`` (paged
  with ``page_token``): each table's full name, owner, comment, columns and properties;
* lineage: ``/api/2.0/lineage-tracking/table-lineage`` for each table (its downstreams);
* with a SQL warehouse (``--warehouse``), through the Statement Execution API
  (``/api/2.0/sql/statements``): ``system.access.table_lineage`` in one query instead of a
  call per table, and the columns' classification tags from
  ``system.information_schema.column_tags``. Without a warehouse there are no tags, so no
  sensitivity.

Sign-in: a personal access token (``DATABRICKS_TOKEN``), or OAuth machine-to-machine with a
service principal (``DATABRICKS_CLIENT_ID``, ``DATABRICKS_CLIENT_SECRET``, at
``/oidc/v1/token``).

What it writes, as one source (category ``catalog``), in the same shape as the dbt
connector, so a table both describe is one data product:

* **data products**: one per table, ID its full name (``catalog.schema.table``), with the
  tables it feeds, the system it runs on (``--runs-on``), its sensitivity (the highest of
  its columns' ``classification`` tags), its DataHub dataset URN
  (``urn:li:dataset:(urn:li:dataPlatform:databricks,<full name>,<env>)``), an owner role
  when its owner is a person, and a steward role from the ``steward`` property;
* **roles** "Owner of <table>" and "Steward of <table>", and **people** filling them,
  matched by email to the people VeroMesh holds.

A table owned by a group (Unity Catalog allows it) has no owner role: an accountable
person is missing, and the run counts it. Catalogs can be limited with ``--catalog``.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core import BatchConnector, Item, MappingError, Record
from ..core.source import SourceApi
from .dbt import CLASSIFICATION, LEVELS
from .servicenow import _now

UC = "/api/2.1/unity-catalog"


class DatabricksClient:
    def __init__(self, api: SourceApi, warehouse: str | None = None) -> None:
        self.api, self.warehouse = api, warehouse

    def catalogs(self) -> list[str]:
        return [c["name"] for c in self.api.get(f"{UC}/catalogs").get("catalogs") or []]

    def schemas(self, catalog: str) -> list[str]:
        return [
            s["name"] for s in self.api.get(f"{UC}/schemas", {"catalog_name": catalog}).get("schemas") or []
        ]

    def tables(self, catalog: str, schema: str) -> Iterable[dict]:
        token = None
        while True:
            params = {"catalog_name": catalog, "schema_name": schema, "max_results": 50}
            if token:
                params["page_token"] = token
            page = self.api.get(f"{UC}/tables", params)
            yield from page.get("tables") or []
            token = page.get("next_page_token")
            if not token:
                return

    def downstreams(self, full_name: str) -> list[str]:
        body = self.api.get("/api/2.0/lineage-tracking/table-lineage", {"table_name": full_name})
        out = []
        for d in body.get("downstreams") or []:
            t = d.get("tableInfo") or {}
            if t.get("name"):
                out.append(f"{t.get('catalog_name')}.{t.get('schema_name')}.{t['name']}")
        return out

    def sql(self, statement: str) -> list[dict]:
        r = self.api.post(
            "/api/2.0/sql/statements",
            {"statement": statement, "warehouse_id": self.warehouse, "wait_timeout": "30s"},
        )
        state = (r.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            raise MappingError(f"SQL statement {state}: {statement}", "/api/2.0/sql/statements")
        cols = [c["name"] for c in ((r.get("manifest") or {}).get("schema") or {}).get("columns") or []]
        return [dict(zip(cols, row, strict=False)) for row in (r.get("result") or {}).get("data_array") or []]


class UnityCatalogConnector(BatchConnector):
    name = "databricks"
    kind = "connector"

    def __init__(
        self,
        client: DatabricksClient,
        *,
        people: list[dict] | None = None,
        catalogs: list[str] | None = None,
        runs_on: str | None = None,
        env: str = "PROD",
    ) -> None:
        super().__init__()
        self.client, self.only, self.runs_on, self.env = client, set(catalogs or []), runs_on, env
        self.by_email = {str(p["email"]).lower(): p for p in people or [] if p.get("email")}
        self.tables: dict[str, dict] = {}
        self.feeds: dict[str, set[str]] = {}
        self.tags: dict[str, list[str]] = {}
        self.group_owned: list[str] = []
        self.unmatched: set[str] = set()

    def describe(self) -> str:
        notes = []
        if self.group_owned:
            notes.append(f"{len(self.group_owned)} owned by a group, not a person")
        if self.unmatched:
            n = len(self.unmatched)
            notes.append(
                f"{n} {'owner or steward matches' if n == 1 else 'owners or stewards match'} no one in VeroMesh"
            )
        if not self.client.warehouse:
            notes.append("no SQL warehouse, so no classification tags")
        extra = "; " + "; ".join(notes) if notes else ""
        return f"Databricks Unity Catalog: {len(self.tables)} tables{extra}"

    def fingerprint(self) -> str | None:
        return "databricks"

    def urn(self, full: str) -> str:
        return f"urn:li:dataset:(urn:li:dataPlatform:databricks,{full},{self.env})"

    def _person(self, who: str | None):
        who = str(who or "").strip().lower()
        if "@" not in who:
            return None
        p = self.by_email.get(who)
        if p is None:
            self.unmatched.add(who)
        return p

    def read(self, schema: list[dict]) -> Iterable[Record]:
        for cat in self.client.catalogs():
            if self.only and cat not in self.only:
                continue
            for sch in self.client.schemas(cat):
                for t in self.client.tables(cat, sch):
                    self.tables[t["full_name"]] = t
        if self.client.warehouse:
            for row in self.client.sql("SELECT * FROM system.access.table_lineage"):
                src, dst = row.get("source_table_full_name"), row.get("target_table_full_name")
                if src in self.tables and dst in self.tables:
                    self.feeds.setdefault(src, set()).add(dst)
            for row in self.client.sql("SELECT * FROM system.information_schema.column_tags"):
                if str(row.get("tag_name") or "").lower() == "classification":
                    full = f"{row.get('catalog_name')}.{row.get('schema_name')}.{row.get('table_name')}"
                    self.tags.setdefault(full, []).append(str(row.get("tag_value") or ""))
        else:
            for full in self.tables:
                self.feeds[full] = {d for d in self.client.downstreams(full) if d in self.tables}
        roles: dict[str, tuple[dict, list[str]]] = {}
        for full, t in self.tables.items():
            yield Record(ref=f"Databricks table {full}", data=t, type="data-products")
            owner = self._person(t.get("owner"))
            if owner is None and "@" not in str(t.get("owner") or ""):
                self.group_owned.append(full)
            if owner:
                roles.setdefault(owner["id"], (owner, []))[1].append(f"{full}.Owner")
            if steward := self._person((t.get("properties") or {}).get("steward")):
                roles.setdefault(steward["id"], (steward, []))[1].append(f"{full}.Steward")
        for pid, (person, held) in roles.items():
            yield Record(
                ref=f"Databricks owner or steward {pid}",
                data={"person": person, "roles": held},
                type="people",
            )

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if record.type == "data-products":
            t = record.data
            full = t["full_name"]
            e: dict = {"id": full, "name": t.get("name") or full, "catalogUrn": self.urn(full)}
            if feeds := sorted(self.feeds.get(full, ())):
                e["feedsDataProducts"] = feeds if len(feeds) > 1 else feeds[0]
            if self.runs_on:
                e["runsOnSystems"] = self.runs_on
            found = [
                CLASSIFICATION[c.lower()] for c in self.tags.get(full, []) if c.lower() in CLASSIFICATION
            ]
            if found:
                e["sensitivity"] = max(found, key=LEVELS.index)
            items = []
            if "@" in str(t.get("owner") or "") and self.by_email.get(str(t["owner"]).lower()):
                e["ownerRole"] = f"{full}.Owner"
                items.append(
                    Item(
                        type="roles",
                        entity={"id": e["ownerRole"], "name": f"Owner of {full}"},
                        ref=record.ref,
                        observed_at=_now(),
                    )
                )
            steward = str((t.get("properties") or {}).get("steward") or "").lower()
            if steward and self.by_email.get(steward):
                e["stewardRole"] = f"{full}.Steward"
                items.append(
                    Item(
                        type="roles",
                        entity={"id": e["stewardRole"], "name": f"Steward of {full}"},
                        ref=record.ref,
                        observed_at=_now(),
                    )
                )
            return [*items, Item(type="data-products", entity=e, ref=record.ref, observed_at=_now())]
        if record.type == "people":
            p, held = record.data["person"], sorted(set(record.data["roles"]))
            e = {
                "id": p["id"],
                "name": p.get("name") or p["id"],
                "rolesFilled": held if len(held) > 1 else held[0],
            }
            return [Item(type="people", entity=e, ref=record.ref, observed_at=_now())]
        raise MappingError(f"unexpected record type {record.type}", record.ref)
