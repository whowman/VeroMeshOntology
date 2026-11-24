"""HaloITSM: business applications and who can work tickets, through the Halo REST API.

What it reads (OAuth 2.0 client credentials at ``/auth/token`` with ``scope=all``, then
``/api/...``, paged with ``pageinate=true``, ``page_size`` and ``page_no`` until
``record_count`` is reached):

* ``/api/Asset``: assets of type "Business Application": ``key_field`` (the name),
  ``inventory_number``, ``inactive``, and the custom fields "Business Owner" (free text: a
  name or an email address) and "Criticality";
* ``/api/Agent``: the agents (IT staff with a licensed seat): name, email, ``isdisabled``.

What it writes, in two sources (``import`` loads both):

* ``halo`` (category ``cmdb``): systems. An application whose name matches a system VeroMesh
  already holds (from ServiceNow, say) is written under that system's ID, so the two describe
  one system; otherwise its inventory number. Its business owner, matched to a person by email
  or by name, fills the role "<system>.BusinessOwner", the same role the ServiceNow connector
  uses. An owner it cannot match is reported in the run's problems.
* ``halo-agents`` (category ``cmdb``, registered with ``grantsAccess``): every enabled agent,
  matched to a person by email. A leaver whose seat is still enabled shows as access without
  employment.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from ..core import BatchConnector, Item, MappingError, Record
from ..core.source import SourceApi
from .oracle_erp import local_id

PAGE = 100
APP_TYPE = "Business Application"
CRITICALITY = {"1": "critical", "2": "high", "3": "medium", "4": "low"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:120]


class HaloClient:
    def __init__(self, api: SourceApi) -> None:
        self.api = api

    def rows(self, resource: str, key: str) -> Iterator[dict]:
        page = 1
        while True:
            got = self.api.get(
                f"/api/{resource}", {"pageinate": "true", "page_size": str(PAGE), "page_no": str(page)}
            )
            rows = got.get(key, [])
            yield from rows
            if not rows or page * PAGE >= int(got.get("record_count", 0)):
                return
            page += 1


def _field(asset: dict, name: str) -> str:
    for f in asset.get("fields") or []:
        if str(f.get("name", "")).lower() == name.lower():
            return str(f.get("value") or "").strip()
    return ""


class HaloConnector(BatchConnector):
    name = "halo"
    kind = "connector"

    def __init__(
        self,
        client: HaloClient,
        *,
        people: list[dict] | None = None,
        systems: list[dict] | None = None,
        part: str = "assets",
    ) -> None:
        super().__init__()
        if part not in ("assets", "agents"):
            raise ValueError("part is assets or agents")
        self.part = part
        self.client = client
        people = people or []
        self.by_email = {str(p["email"]).lower(): p for p in people if p.get("email")}
        self.by_local = {str(p["id"]).lower(): p for p in people}
        self.by_name: dict[str, list[dict]] = {}
        for p in people:
            if p.get("name"):
                self.by_name.setdefault(str(p["name"]).lower(), []).append(p)
        self.systems = {str(s.get("name", "")).lower(): s for s in systems or [] if s.get("name")}

    def describe(self) -> str:
        return "HaloITSM " + ("business applications" if self.part == "assets" else "agents")

    def fingerprint(self) -> str | None:
        return f"halo-{self.part}"

    def person(self, text: str) -> dict | None:
        """A person from an email address or a name, when it names exactly one."""
        t = text.strip().lower()
        if not t:
            return None
        if "@" in t:
            return self.by_email.get(t) or self.by_local.get(local_id(t))
        hits = self.by_name.get(t, [])
        return hits[0] if len(hits) == 1 else None

    def read(self, schema: list[dict]) -> Iterable[Record]:
        if self.part == "agents":
            for a in self.client.rows("Agent", "agents"):
                if a.get("isdisabled"):
                    continue
                yield Record(ref=f"Halo agent {a.get('name')}", data=a, type="people")
            return
        owners: dict[str, tuple[dict, set[str]]] = {}
        for a in self.client.rows("Asset", "assets"):
            if a.get("assettype_name") != APP_TYPE:
                continue
            ref = f"Halo asset {a.get('inventory_number')}"
            text = _field(a, "Business Owner")
            if text:
                p = self.person(text)
                if p is None:
                    self.problems.append(f"{ref}: business owner '{text}' matches no one person in VeroMesh")
                else:
                    owners.setdefault(p["id"], (p, set()))[1].add(f"{self._sid(a)}.BusinessOwner")
                    a = {**a, "_owner": p["id"]}
            yield Record(ref=ref, data=a, type="systems")
        for pid, (p, roles) in sorted(owners.items()):
            yield Record(ref=f"Halo owner {pid}", data={"person": p, "roles": sorted(roles)}, type="owners")

    def _sid(self, a: dict) -> str:
        name = str(a.get("key_field") or a.get("inventory_number") or "")
        known = self.systems.get(name.lower())
        return known["id"] if known else _slug(str(a.get("inventory_number") or name))

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        a = record.data
        if record.type == "people":
            p = self.person(str(a.get("email") or ""))
            pid = p["id"] if p else local_id(str(a.get("email") or a.get("name") or ""))
            if not pid:
                raise MappingError("an agent with no email or name", record.ref)
            return [
                Item(
                    type="people",
                    entity={"id": pid, "name": (p or {}).get("name") or a.get("name") or pid},
                    ref=record.ref,
                    observed_at=_now(),
                )
            ]
        if record.type == "owners":
            p, roles = a["person"], a["roles"]
            e = {
                "id": p["id"],
                "name": p.get("name") or p["id"],
                "rolesFilled": roles if len(roles) > 1 else roles[0],
            }
            return [Item(type="people", entity=e, ref=record.ref, observed_at=_now())]
        if record.type != "systems":
            raise MappingError(f"unexpected record type {record.type}", record.ref)
        name = str(a.get("key_field") or a.get("inventory_number") or "")
        sid = self._sid(a)
        e: dict = {"id": sid, "name": name, "lifecycle": "retired" if a.get("inactive") else "active"}
        crit = _field(a, "Criticality")[:1]
        if crit in CRITICALITY:
            e["criticality"] = CRITICALITY[crit]
        items: list[Item] = []
        if a.get("_owner"):
            rid = f"{sid}.BusinessOwner"
            e["businessOwner"] = rid
            items.append(
                Item(
                    type="roles",
                    entity={"id": rid, "name": f"Business owner of {name}"},
                    ref=record.ref,
                    observed_at=_now(),
                )
            )
        return [*items, Item(type="systems", entity=e, ref=record.ref, observed_at=_now())]
