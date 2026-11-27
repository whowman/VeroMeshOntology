"""SAP LeanIX: the business capability map and who owns each capability, through the GraphQL API.

What it reads (``/services/pathfinder/v1/graphql``, Relay-style pages of ``allFactSheets``):

* every **BusinessCapability** fact sheet: name, description, status, its parent
  (``relToParent``), and its subscriptions (each with a user, a type and roles).

Sign-in: a technical user's API token, exchanged at ``/services/mtm/v1/oauth2/token``
(basic authentication as ``apitoken``, client credentials) for a short-lived access token.

What it writes, as one source (category ``ea``, which decides the capabilities domain by
default), in the same shape as the ServiceNow EA connector so that a capability loaded from
both is one entity with one owner role:

* **capabilities**: ID from the name in the workbook's style ("Accounts payable" ->
  AccountsPayable), or the fact sheet's ``externalId`` (``id_from="externalId"``); name,
  definition (the description), parent capability, and the owner **role**
  (``AccountsPayable.Owner``, "Owner of Accounts payable"). Archived fact sheets are left out.
* **roles**: one per owned capability.
* **supported by systems**: the applications related to each capability (its
  ``relBusinessCapabilityToApplication``), as the systems VeroMesh holds: the Application fact
  sheet's ``externalId`` when it is a system's ID or CMDB ID (as when LeanIX is synced with the
  CMDB), else its name. An application that matches no system is left out and reported.
* **people**: the owner (a RESPONSIBLE subscription, with the role named ``owner_role``,
  default "Business Owner"; else the first RESPONSIBLE subscriber), matched by email to the
  people VeroMesh holds. An owner who matches no one is reported.

Two capabilities that would get the same ID stop the load, as in ServiceNow EA.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Iterable

from ..core import BatchConnector, Item, MappingError, Record, source_time
from ..core.session import ApiError
from ..core.source import SourceApi
from .servicenow import SystemMatcher, _now, _slug, pascal

GRAPHQL = "/services/pathfinder/v1/graphql"
TOKEN = "/services/mtm/v1/oauth2/token"
PAGE = 200
QUERY = """query Capabilities($after: String) {
  allFactSheets(factSheetType: BusinessCapability, first: PAGE_SIZE, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id name description status updatedAt
      ... on BusinessCapability {
        externalId { externalId }
        relToParent { edges { node { factSheet { id } } } }
        relBusinessCapabilityToApplication { edges { node { factSheet {
          id name ... on Application { externalId { externalId } }
        } } } }
      }
      subscriptions { edges { node { type user { email displayName } roles { name } } } }
    } }
  }
}""".replace("PAGE_SIZE", str(PAGE))


class ApiToken:
    """A LeanIX technical user's API token, exchanged for access tokens (basic authentication)."""

    refreshable = True

    def __init__(self, transport, api_token: str, clock=time.time) -> None:
        self._t, self._clock = transport, clock
        self._basic = "Basic " + base64.b64encode(f"apitoken:{api_token}".encode()).decode()
        self._token: str | None = None
        self._expires = 0.0

    def token(self, force: bool = False) -> str | None:
        if force or not self._token or self._clock() > self._expires - 30:
            r = self._t.request(
                "POST",
                f"{self._t.base_url}{TOKEN}",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": self._basic},
            )
            if r.status_code != 200:
                raise ApiError(r.status_code, f"token request failed: {r.text[:200]}")
            body = r.json()
            self._token = body["access_token"]
            self._expires = self._clock() + float(body.get("expires_in", 300))
        return self._token


class LeanixClient:
    def __init__(self, api: SourceApi) -> None:
        self.api = api

    def capabilities(self) -> Iterable[dict]:
        after = None
        while True:
            body = self.api.post(GRAPHQL, {"query": QUERY, "variables": {"after": after}})
            if body.get("errors"):
                raise MappingError(f"LeanIX GraphQL: {body['errors'][0].get('message')}", GRAPHQL)
            page = body["data"]["allFactSheets"]
            for edge in page.get("edges") or []:
                yield edge["node"]
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return
            after = info.get("endCursor")


def _edges(conn) -> list[dict]:
    return [e["node"] for e in ((conn or {}).get("edges") or []) if e.get("node")]


class LeanixConnector(BatchConnector):
    name = "leanix"
    kind = "connector"

    def __init__(
        self,
        client: LeanixClient,
        *,
        people: list[dict] | None = None,
        id_from: str = "name",
        owner_role: str = "Business Owner",
        systems: list[dict] | None = None,
    ) -> None:
        super().__init__()
        if id_from not in ("name", "externalId"):
            raise ValueError("id_from is name or externalId")
        self.client, self.id_from, self.owner_role = client, id_from, owner_role
        self.by_email = {str(p["email"]).lower(): p for p in people or [] if p.get("email")}
        self.caps: list[dict] = []
        self.ids: dict[str, str] = {}
        self.unmatched: list[str] = []
        self.systems = SystemMatcher(systems)
        self.unlinked: list[str] = []
        self.support: dict[str, list[str]] = {}

    def describe(self) -> str:
        note = f"; {len(self.unmatched)} owners match no one in VeroMesh" if self.unmatched else ""
        if self.unlinked:
            note += (
                f"; {len(self.unlinked)} applications match no system VeroMesh holds "
                f"({', '.join(sorted(set(self.unlinked))[:5])})"
            )
        return f"SAP LeanIX: {len(self.caps)} business capabilities{note}"

    def _support(self, fs: dict) -> list[str]:
        out: list[str] = []
        for n in _edges(fs.get("relBusinessCapabilityToApplication")):
            app = n.get("factSheet") or {}
            ext = (app.get("externalId") or {}).get("externalId")
            sid = self.systems.find(ext, name=app.get("name"))
            if sid is None:
                self.unlinked.append(str(app.get("name") or app.get("id")))
            elif sid not in out:
                out.append(sid)
        return sorted(out)

    def fingerprint(self) -> str | None:
        return "leanix"

    def _owner(self, fs: dict) -> dict | None:
        subs = [
            s for s in _edges(fs.get("subscriptions")) if s.get("type") == "RESPONSIBLE" and s.get("user")
        ]
        named = [s for s in subs if any(r.get("name") == self.owner_role for r in s.get("roles") or [])]
        return (named or subs or [None])[0]

    def read(self, schema: list[dict]) -> Iterable[Record]:
        self.caps = [c for c in self.client.capabilities() if str(c.get("status") or "ACTIVE") != "ARCHIVED"]
        taken: dict[str, str] = {}
        for c in self.caps:
            raw = (
                ((c.get("externalId") or {}).get("externalId") or "")
                if self.id_from == "externalId"
                else pascal(c.get("name") or "")
            )
            cid = _slug(str(raw)) or c["id"]
            if cid in taken:
                self.problems.append(
                    f"LeanIX capabilities {taken[cid]} and {c['id']} would both be {cid}: rename one, "
                    "or take IDs from the external ID (--id-from externalId)"
                )
            taken[cid] = c["id"]
            self.ids[c["id"]] = cid
        self.support = {c["id"]: self._support(c) for c in self.caps}  # before describe() is read
        owners: dict[str, list[str]] = {}
        for c in self.caps:
            yield Record(ref=f"LeanIX business capability {c.get('name')}", data=c, type="capabilities")
            if (sub := self._owner(c)) is not None:
                owners.setdefault(str(sub["user"].get("email") or "").lower(), []).append(
                    f"{self.ids[c['id']]}.Owner"
                )
        for email, roles in owners.items():
            person = self.by_email.get(email)
            if person is None:
                self.unmatched.append(email)
                continue
            yield Record(ref=f"LeanIX user {email}", data={"person": person, "roles": roles}, type="people")

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if record.type == "capabilities":
            c = record.data
            cid = self.ids[c["id"]]
            name = c.get("name") or cid
            e: dict = {"id": cid, "name": name}
            if c.get("description"):
                e["definition"] = c["description"]
            parents = [n["factSheet"]["id"] for n in _edges(c.get("relToParent")) if n.get("factSheet")]
            if parents and parents[0] in self.ids:  # an archived or missing parent: top level
                e["parentCapability"] = self.ids[parents[0]]
            if sup := self.support.get(c["id"]):
                e["supportedBySystems"] = sup
            items = []
            if self._owner(c) is not None:
                e["ownerRole"] = f"{cid}.Owner"
                items.append(
                    Item(
                        type="roles",
                        entity={"id": e["ownerRole"], "name": f"Owner of {name}"},
                        ref=record.ref,
                        observed_at=source_time(c.get("updatedAt")),
                    )
                )
            return [
                *items,
                Item(
                    type="capabilities", entity=e, ref=record.ref, observed_at=source_time(c.get("updatedAt"))
                ),
            ]
        if record.type == "people":
            p, roles = record.data["person"], sorted(set(record.data["roles"]))
            e = {
                "id": p["id"],
                "name": p.get("name") or p["id"],
                "rolesFilled": roles if len(roles) > 1 else roles[0],
            }
            return [Item(type="people", entity=e, ref=record.ref, observed_at=_now())]
        raise MappingError(f"unexpected record type {record.type}", record.ref)
