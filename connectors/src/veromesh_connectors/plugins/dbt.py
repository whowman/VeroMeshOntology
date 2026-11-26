"""dbt: the tables a dbt project builds, their lineage, owners and sensitivity, and the KPIs
its metrics compute, from the project's run artifacts.

Where the artifacts come from (dbt's documented artifact schemas: manifest v12, catalog,
run results):

* a folder: the ``target/`` directory a ``dbt build`` leaves (``--dir``);
* a URL that serves ``manifest.json`` (and optionally ``run_results.json``) beside each
  other, such as an artifact bucket behind HTTPS (``--url``);
* dbt Cloud's Administrative API v2: the artifacts of a job's latest run,
  ``/api/v2/accounts/{account}/jobs/{job}/artifacts/manifest.json`` with a service token
  (``--dbt-cloud --account --job``).

What it writes, as one source (category ``catalog``, which decides what data means by
default):

* **data products**: one per model and source, ID ``database.schema.name`` (the table as the
  warehouse names it), with its name, the data products it
  feeds (the manifest's ``depends_on``, read the other way), the platform it runs on
  (``--runs-on``, a system ID such as the lakehouse's), its sensitivity (the highest of its
  columns' classifications in ``meta``), its owner role, and its DataHub dataset URN
  (``urn:li:dataset:(urn:li:dataPlatform:<adapter>,<database.schema.name>,<env>)``) so that
  catalog pages and data-change impact meet;
* **roles**: "Owner of <table>", one per table with an owner (``meta.owner``, an email);
* **people**: each owner, filling their owner roles, matched by email to the people VeroMesh
  holds (an owner who matches no one is counted);
* **KPIs**: each dbt metric's KPI, computed from the data products the metric depends on.
  The KPI's ID is ``meta.veromesh_kpi`` where set, else the metric's name in the workbook's
  style (on_time_payment_rate -> OnTimePaymentRate). Only KPIs already in the model are
  linked: dbt says how a KPI is computed, not that it is one the organization tracks.

Tests, seeds and snapshots are not data products. A model whose last run failed is counted
in the run's summary (run_results.json), with what it skipped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from ..core import BatchConnector, Item, MappingError, Record
from ..core.source import SourceApi
from .servicenow import _now, pascal

#: classification in a column's meta -> VeroMesh sensitivity, lowest first.
LEVELS = ("Public", "Internal", "Confidential", "Restricted")
CLASSIFICATION = {
    "public": "Public",
    "internal": "Internal",
    "confidential": "Confidential",
    "sensitive": "Confidential",
    "restricted": "Restricted",
    "pii": "Restricted",
    "phi": "Restricted",
    "pci": "Restricted",
}
TABLES = ("model", "source")


class Artifacts:
    """Reads a dbt project's artifacts from a folder, a URL or dbt Cloud."""

    def __init__(self, *, folder: str | None = None, api: SourceApi | None = None, prefix: str = "") -> None:
        if not folder and api is None:
            raise ValueError("a folder or an API")
        self.folder, self.api, self.prefix = folder, api, prefix

    def get(self, name: str, required: bool = True) -> dict | None:
        try:
            if self.folder:
                return json.loads((Path(self.folder) / name).read_text())
            return self.api.get(f"{self.prefix}/{name}")
        except Exception as exc:  # noqa: BLE001 — reported as a problem, or skipped if optional
            if required:
                raise MappingError(f"could not read {name}: {exc}", name) from exc
            return None


class DbtConnector(BatchConnector):
    name = "dbt"
    kind = "connector"

    def __init__(
        self,
        artifacts: Artifacts,
        *,
        people: list[dict] | None = None,
        kpis: list[dict] | None = None,
        runs_on: str | None = None,
        env: str = "PROD",
    ) -> None:
        super().__init__()
        self.artifacts, self.runs_on, self.env = artifacts, runs_on, env
        self.by_email = {str(p["email"]).lower(): p for p in people or [] if p.get("email")}
        self.kpis = {k["id"]: k.get("name") or k["id"] for k in kpis or []}
        self.tables: dict[str, dict] = {}  # unique_id -> node
        self.ids: dict[str, str] = {}  # unique_id -> data product ID
        self.feeds: dict[str, set[str]] = {}
        self.platform = "dbt"
        self.failed: list[str] = []
        self.skipped: list[str] = []
        self.unmatched: set[str] = set()
        self.unknown_kpis: set[str] = set()

    def describe(self) -> str:
        def n(count: int, one: str, many: str) -> str:
            return f"{count} {one if count == 1 else many}"

        notes = []
        if self.failed:
            notes.append(
                f"{n(len(self.failed), 'model', 'models')} failed in the last run ({len(self.skipped)} skipped after)"
            )
        if self.unmatched:
            notes.append(f"{n(len(self.unmatched), 'owner matches', 'owners match')} no one in VeroMesh")
        if self.unknown_kpis:
            notes.append(f"{n(len(self.unknown_kpis), 'metric is', 'metrics are')} not a KPI in the model")
        extra = "; " + "; ".join(notes) if notes else ""
        return f"dbt: {len(self.tables)} tables{extra}"

    def fingerprint(self) -> str | None:
        return "dbt"

    def urn(self, dp_id: str) -> str:
        return f"urn:li:dataset:(urn:li:dataPlatform:{self.platform},{dp_id},{self.env})"

    def read(self, schema: list[dict]) -> Iterable[Record]:
        try:
            manifest = self.artifacts.get("manifest.json")
        except MappingError as exc:
            self.problems.append(str(exc))
            return
        self.platform = (manifest.get("metadata") or {}).get("adapter_type") or "dbt"
        nodes = {**(manifest.get("sources") or {}), **(manifest.get("nodes") or {})}
        self.tables = {uid: n for uid, n in nodes.items() if n.get("resource_type") in TABLES}
        for uid, n in self.tables.items():
            self.ids[uid] = (
                ".".join(str(n.get(k) or "") for k in ("database", "schema", "name")).strip(".") or uid
            )
        for uid, n in self.tables.items():
            for up in (n.get("depends_on") or {}).get("nodes") or []:
                if up in self.tables:
                    self.feeds.setdefault(up, set()).add(self.ids[uid])
        run = self.artifacts.get("run_results.json", required=False) or {}
        for r in run.get("results") or []:
            if r.get("unique_id") in self.tables:
                if r.get("status") in ("error", "fail"):
                    self.failed.append(self.ids[r["unique_id"]])
                elif r.get("status") == "skipped":
                    self.skipped.append(self.ids[r["unique_id"]])
        owners: dict[str, list[str]] = {}
        for uid, n in self.tables.items():
            yield Record(ref=f"dbt {n.get('resource_type')} {uid}", data=n, type="data-products")
            if email := str((n.get("meta") or {}).get("owner") or "").strip().lower():
                owners.setdefault(email, []).append(f"{self.ids[uid]}.Owner")
        for email, roles in owners.items():
            if (person := self.by_email.get(email)) is None:
                self.unmatched.add(email)
                continue
            yield Record(ref=f"dbt owner {email}", data={"person": person, "roles": roles}, type="people")
        for mid, m in (manifest.get("metrics") or {}).items():
            kid = str((m.get("meta") or {}).get("veromesh_kpi") or pascal(str(m.get("name") or "")))
            if kid not in self.kpis:
                self.unknown_kpis.add(kid)
                continue
            yield Record(ref=f"dbt metric {mid}", data={**m, "_kpi": kid}, type="kpis")

    def _sensitivity(self, n: dict) -> str | None:
        found = [
            CLASSIFICATION[c]
            for col in (n.get("columns") or {}).values()
            if (c := str((col.get("meta") or {}).get("classification") or "").lower()) in CLASSIFICATION
        ]
        return max(found, key=LEVELS.index) if found else None

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if record.type == "data-products":
            n = record.data
            uid = n["unique_id"]
            dp = self.ids[uid]
            e: dict = {"id": dp, "name": n.get("name") or dp, "catalogUrn": self.urn(dp)}
            if feeds := sorted(self.feeds.get(uid, ())):
                e["feedsDataProducts"] = feeds if len(feeds) > 1 else feeds[0]
            if self.runs_on:
                e["runsOnSystems"] = self.runs_on
            if s := self._sensitivity(n):
                e["sensitivity"] = s
            items = []
            if (n.get("meta") or {}).get("owner"):
                e["ownerRole"] = f"{dp}.Owner"
                items.append(
                    Item(
                        type="roles",
                        entity={"id": e["ownerRole"], "name": f"Owner of {dp}"},
                        ref=record.ref,
                        observed_at=_now(),
                    )
                )
            return [*items, Item(type="data-products", entity=e, ref=record.ref, observed_at=_now())]
        if record.type == "people":
            p, roles = record.data["person"], sorted(set(record.data["roles"]))
            e = {
                "id": p["id"],
                "name": p.get("name") or p["id"],
                "rolesFilled": roles if len(roles) > 1 else roles[0],
            }
            return [Item(type="people", entity=e, ref=record.ref, observed_at=_now())]
        if record.type == "kpis":
            m = record.data
            kid = m["_kpi"]
            built = sorted(
                {self.ids[u] for u in (m.get("depends_on") or {}).get("nodes") or [] if u in self.ids}
            )
            if not built:
                return []
            e = {
                "id": kid,
                "name": self.kpis[kid],
                "computedFromDataProducts": built if len(built) > 1 else built[0],
            }
            return [Item(type="kpis", entity=e, ref=record.ref, observed_at=_now())]
        raise MappingError(f"unexpected record type {record.type}", record.ref)
