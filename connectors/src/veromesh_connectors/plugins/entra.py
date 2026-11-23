"""Microsoft Entra ID: people, managers, role groups and AI agents, through Microsoft Graph v1.0.

What it reads (all documented Graph v1.0 calls, application permissions
``User.Read.All``, ``GroupMember.Read.All`` and ``Application.Read.All``):

* ``GET /users?$select=…&$expand=manager($select=id)`` paged by ``@odata.nextLink``;
* with ``role_group_prefix``: ``GET /groups?$filter=startswith(displayName,'<prefix>')``
  and ``GET /groups/{id}/members`` for each, a group per role;
* with ``agent_tag``: ``GET /servicePrincipals`` (those carrying the tag) and
  ``GET /servicePrincipals/{id}/owners``, the person each agent acts for;
* changes: ``GET /users/delta``, following ``@odata.nextLink`` and keeping the
  ``@odata.deltaLink`` for next time (``EntraChangesConnector``).

Sign-in: client credentials at ``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token``
with scope ``https://graph.microsoft.com/.default``.

What it writes:

* **people**: ID = the user's ``employeeId`` when set (the same ID as the HR
  system, so the two sources describe one person), else the user principal
  name's local part (contractors and other accounts with no HR record); name,
  email, employee ID, whom they report to, and with role groups the roles they
  fill. Disabled accounts are left out.
* **roles**: one per role group (``SG-APManager`` -> ``APManager``).
* **agents**: one per tagged service principal; ID from its display name, the
  application (client) ID it signs in with, and the owner it acts for. An agent
  whose owners have all left is left out (and so refused by the decision check).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ..core import BatchConnector, Event, EventConnector, Item, MappingError, Record
from ..core.source import SourceApi, SourceError

USER_FIELDS = "id,displayName,mail,userPrincipalName,employeeId,employeeType,accountEnabled,jobTitle"
PAGE = 999  # Graph's largest page for users


def local_id(upn: str) -> str:
    """'Jane.Doe@contoso.com' -> 'jane.doe'."""
    return re.sub(r"[^a-z0-9._-]+", "-", upn.split("@")[0].lower()).strip("-.")[:128]


def agent_id(name: str) -> str:
    """'Invoice Assistant' -> 'InvoiceAssistant'."""
    words = re.split(r"[^A-Za-z0-9]+", name)
    return "".join(w[:1].upper() + w[1:] for w in words)[:128] or "agent"


class GraphClient:
    def __init__(self, api: SourceApi, version: str = "v1.0") -> None:
        self.api = api
        self.v = version

    def pages(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Every item of a collection, following @odata.nextLink. The last page's
        @odata.deltaLink, if any, is left in ``self.delta_link``."""
        self.delta_link: str | None = None
        url: str | None = f"/{self.v}{path}"
        while url:
            body = self.api.get(url, params)
            params = None  # a next link carries its own query
            yield from body.get("value", [])
            url = body.get("@odata.nextLink")
            self.delta_link = body.get("@odata.deltaLink") or self.delta_link

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.api.get(f"/{self.v}{path}", params)

    def follow(self, link: str) -> Iterator[dict]:
        """Items from an absolute link (a saved deltaLink), following next links."""
        self.delta_link = None
        url: str | None = link
        while url:
            body = self.api.get(url)
            yield from body.get("value", [])
            url = body.get("@odata.nextLink")
            self.delta_link = body.get("@odata.deltaLink") or self.delta_link


def person_id(user: dict) -> str:
    return (user.get("employeeId") or "").strip() or local_id(user.get("userPrincipalName") or user["id"])


def person_entity(user: dict, manager: str | None, roles: list[str]) -> dict:
    pid = person_id(user)
    out: dict = {"id": pid, "name": user.get("displayName") or pid}
    if user.get("employeeId"):
        out["employeeId"] = user["employeeId"]
    if user.get("mail"):
        out["email"] = user["mail"]
    if manager:
        out["reportsTo"] = manager
    if roles:
        out["rolesFilled"] = sorted(roles) if len(roles) > 1 else roles[0]
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class EntraConnector(BatchConnector):
    name = "entra"
    kind = "connector"

    def __init__(
        self,
        graph: GraphClient,
        *,
        tenant: str = "",
        role_group_prefix: str | None = None,
        agent_tag: str | None = "AIAgent",
    ) -> None:
        super().__init__()
        self.graph, self.tenant = graph, tenant
        self.role_group_prefix = role_group_prefix or None
        self.agent_tag = agent_tag or None
        self.disabled = 0
        self.no_mail = 0
        self.dropped: list[str] = []
        self.agents_left_out: list[str] = []

    def describe(self) -> str:
        notes = []
        if self.disabled:
            notes.append(f"{self.disabled} disabled accounts left out")
        if self.no_mail:
            notes.append(f"{self.no_mail} accounts with no mail address")
        if self.agents_left_out:
            notes.append(f"agents with no enabled owner left out: {', '.join(self.agents_left_out)}")
        if self.dropped:
            shown = ", ".join(self.dropped[:5]) + (" …" if len(self.dropped) > 5 else "")
            notes.append(f"{len(self.dropped)} references to accounts not in this run dropped ({shown})")
        return f"Microsoft Entra ID {self.tenant}".rstrip() + (f"; {'; '.join(notes)}" if notes else "")

    def fingerprint(self) -> str | None:
        return f"entra:{self.tenant}" if self.tenant else "entra"

    def read(self, schema: list[dict]) -> Iterable[Record]:
        users = list(
            self.graph.pages(
                "/users", {"$select": USER_FIELDS, "$expand": "manager($select=id)", "$top": str(PAGE)}
            )
        )
        enabled = {u["id"]: u for u in users if u.get("accountEnabled", True)}
        self.disabled = len(users) - len(enabled)
        ids = {oid: person_id(u) for oid, u in enabled.items()}
        seen: dict[str, str] = {}
        for oid, pid in ids.items():
            if pid in seen:
                self.problems.append(
                    f"Entra ID users {seen[pid]} and {oid} both map to person '{pid}' (same employeeId or name)"
                )
            seen[pid] = oid

        roles: dict[str, list[str]] = {}
        role_records = []
        if self.role_group_prefix:
            prefix = self.role_group_prefix.replace("'", "''")
            for g in self.graph.pages(
                "/groups",
                {"$filter": f"startswith(displayName,'{prefix}')", "$select": "id,displayName,description"},
            ):
                rid = g["displayName"][len(self.role_group_prefix) :].strip()
                if not rid:
                    continue
                role_records.append(
                    Record(ref=f"Entra ID group {g['displayName']}", data=g | {"roleId": rid}, type="roles")
                )
                for m in self.graph.pages(f"/groups/{g['id']}/members", {"$select": "id"}):
                    if m["id"] in enabled:
                        roles.setdefault(m["id"], []).append(rid)

        for oid, u in enabled.items():
            mgr = (u.get("manager") or {}).get("id")
            manager = ids.get(mgr) if mgr else None
            if mgr and manager is None:
                self.dropped.append(f"{ids[oid]}→manager {mgr}")
            if not u.get("mail"):
                self.no_mail += 1
            yield Record(
                ref=f"Entra ID user {u.get('userPrincipalName') or oid}",
                data={"user": u, "manager": manager, "roles": roles.get(oid, [])},
                type="people",
            )
        yield from role_records

        if self.agent_tag:
            for sp in self.graph.pages("/servicePrincipals", {"$select": "id,appId,displayName,tags,notes"}):
                if self.agent_tag not in (sp.get("tags") or []):
                    continue
                owners = [
                    o for o in self.graph.pages(f"/servicePrincipals/{sp['id']}/owners", {"$select": "id"})
                ]
                acts_for = next((ids[o["id"]] for o in owners if o["id"] in ids), None)
                if acts_for is None:  # an agent acts for someone; with no enabled owner it cannot act
                    self.agents_left_out.append(sp["displayName"])
                    continue
                yield Record(
                    ref=f"Entra ID service principal {sp['displayName']} ({sp['appId']})",
                    data={"sp": sp, "actsFor": acts_for},
                    type="agents",
                )

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if record.type == "people":
            u = record.data["user"]
            return [
                Item(
                    type="people",
                    entity=person_entity(u, record.data["manager"], record.data["roles"]),
                    ref=record.ref,
                    observed_at=_now(),
                )
            ]
        if record.type == "roles":
            g = record.data
            entity = {"id": g["roleId"], "name": g["roleId"]}
            if g.get("description"):
                entity["definition"] = g["description"]
            return [Item(type="roles", entity=entity, ref=record.ref)]
        if record.type == "agents":
            sp = record.data["sp"]
            entity = {
                "id": agent_id(sp["displayName"]),
                "name": sp["displayName"],
                "signInClientId": sp["appId"],
            }
            if record.data["actsFor"]:
                entity["actsFor"] = record.data["actsFor"]
            return [Item(type="agents", entity=entity, ref=record.ref, observed_at=_now())]
        raise MappingError(f"unexpected record type {record.type}", record.ref)


class EntraChangesConnector(EventConnector):
    """User changes from Graph delta queries, one VeroMesh event per changed user.

    The state file keeps the deltaLink and which person each account maps to
    (a removed account comes back with only its object ID). With no state file,
    the first run only records the current position: load everyone with
    ``EntraConnector`` first, then run this on a schedule.
    """

    name = "entra"
    kind = "connector"

    def __init__(
        self, graph: GraphClient, state_file: str | Path, *, role_group_prefix: str | None = None
    ) -> None:
        super().__init__()
        self.graph = graph
        self.role_group_prefix = role_group_prefix or None
        self.state_path = Path(state_file)
        self.state: dict = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.initialized = False

    def describe(self) -> str:
        return "Microsoft Entra ID changes"

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=1, sort_keys=True))

    def events(self, schema: list[dict]) -> Iterable[Event]:
        people: dict[str, str] = self.state.setdefault("people", {})
        link = self.state.get("deltaLink")
        if not link:
            for u in self.graph.pages("/users/delta", {"$select": USER_FIELDS}):
                if "@removed" not in u:
                    people[u["id"]] = person_id(u)
            self.state["deltaLink"] = self.graph.delta_link
            self.initialized = True
            self.save()
            return
        for u in self.graph.follow(link):
            oid = u["id"]
            digest = sha256(json.dumps(u, sort_keys=True).encode()).hexdigest()[:12]
            eid = f"entra-{oid}-{digest}"
            if "@removed" in u or u.get("accountEnabled") is False:
                pid = people.pop(oid, None) or (person_id(u) if u.get("userPrincipalName") else None)
                if pid:
                    yield Event(id=eid, deletes=[("people", pid)])
                continue
            people[oid] = person_id(u)
            manager = None
            try:
                m = self.graph.get(
                    f"/users/{oid}/manager", {"$select": "id,employeeId,userPrincipalName,accountEnabled"}
                )
                if m.get("accountEnabled", True):  # a disabled manager has left: no reference to them
                    manager = person_id(m)
            except SourceError as exc:  # no manager is a 404 in Graph
                if exc.status != 404:
                    raise
            roles = []
            if self.role_group_prefix:  # an upsert replaces the person, so keep the roles the batch run set
                n = len(self.role_group_prefix)
                for g in self.graph.pages(f"/users/{oid}/memberOf", {"$select": "id,displayName"}):
                    name = g.get("displayName") or ""
                    if name.startswith(self.role_group_prefix) and name[n:].strip():
                        roles.append(name[n:].strip())
            yield Event(
                id=eid,
                records=[
                    Record(
                        ref=f"Entra ID user {u.get('userPrincipalName') or oid}",
                        data={"user": u, "manager": manager, "roles": roles},
                        type="people",
                    )
                ],
            )
        # Saved by the caller (save()) only when every event was applied, so a failed event is retried.
        self.state["deltaLink"] = self.graph.delta_link or link

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        u = record.data["user"]
        entity = person_entity(u, record.data["manager"], record.data["roles"])
        return [Item(type="people", entity=entity, ref=record.ref, observed_at=_now())]
