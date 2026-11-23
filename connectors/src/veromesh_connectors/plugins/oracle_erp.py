"""Oracle Fusion Cloud ERP: who can act in the ERP, and approval exceptions, through Oracle's REST API.

What it reads (documented REST resources, basic authentication, ``onlyData=true``, paged
by ``limit`` and ``offset`` until ``hasMore`` is false):

* ``GET /hcmRestApi/resources/11.13.18.05/userAccounts?expand=userAccountRoles``: each
  ERP account (``Username``, ``SuspendedFlag``, ``PersonNumber``) with its role codes;
* for ``exceptions``: ``/fscmRestApi/resources/11.13.18.05/purchaseOrders`` (buyer and
  approver person numbers), ``invoices`` and ``invoiceApprovalsAndNotificationsHistory``
  (who approved each invoice, by ERP user name), filtered with ``q`` on their dates.

What Oracle does not publish: the invoice approval **rules** themselves (amounts and
approver levels live in the BPM approval rules, set up in Functional Setup Manager, with
no REST resource to read them). So the limits in force reach VeroMesh as a workbook the
ERP team exports in the standard format, registered as an ``erp`` source; this connector
loads who can act, and checks what was approved against the authorities in VeroMesh.

What ``import`` writes, as source ``erp`` (category ``erp``):

* **people**: one per account that is not suspended and is linked to a person, with the
  roles they fill in the ERP when a role map (``RoleCode -> role ID``) is given. The ID is
  the person number when it is an employee ID VeroMesh knows (HR's and Entra ID's); else,
  when VeroMesh holds a person whose ID is the user name's local part (a contractor, as
  Entra ID names them), that ID; else the person number. The ERP is not the record of
  employment, so it writes no employee ID; and its accounts carry no name, so the name is
  the one VeroMesh already holds, else the ERP user name. A leaver whose account is still
  active therefore shows as access without employment.

What ``exceptions`` reports (``PUT /api/v1/findings/reported/erp``; each run replaces the
last), for purchase orders and invoices on or after ``since``:

* ``self-approved-po`` (high): a purchase order its buyer approved;
* ``approved-over-limit`` (high): an invoice approved by someone none of whose roles has an
  authority for ``ApproveSupplierInvoice`` that covers the amount (roles as VeroMesh holds them
  today, so a person who has since changed role may show);
* ``missing-co-approval`` (high): an invoice above an authority's co-approval amount with
  one approver.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from ..core import BatchConnector, Item, MappingError, Record
from ..core.source import SourceApi

HCM = "/hcmRestApi/resources/11.13.18.05"
FSCM = "/fscmRestApi/resources/11.13.18.05"
PAGE = 500  # Oracle's largest page
INVOICE_ACTION = "ApproveSupplierInvoice"


class BasicAuth:
    """HTTP basic authentication (an Oracle Fusion integration user)."""

    refreshable = False

    def __init__(self, user: str, password: str) -> None:
        self._header = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def token(self, force: bool = False) -> None:
        return None

    def header(self) -> str:
        return self._header


class OracleClient:
    def __init__(self, api: SourceApi) -> None:
        self.api = api

    def pages(self, path: str, params: dict | None = None) -> Iterator[dict]:
        offset = 0
        while True:
            body = self.api.get(
                path, {"onlyData": "true", **(params or {}), "limit": str(PAGE), "offset": str(offset)}
            )
            items = body.get("items", [])
            yield from items
            if not body.get("hasMore") or not items:
                return
            offset += len(items)


def local_id(user: str) -> str:
    """'JANE.DOE' or 'jane.doe@acme.com' -> 'jane.doe' (the ID Entra ID gives an account with no HR record)."""
    return re.sub(r"[^a-z0-9._-]+", "-", user.split("@")[0].lower()).strip("-.")[:128]


def _children(value) -> list[dict]:
    """An expanded child collection: a list, or Oracle's {"items": [...]}."""
    if isinstance(value, dict):
        return value.get("items", [])
    return value or []


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class OracleErpConnector(BatchConnector):
    name = "oracle-erp"
    kind = "connector"

    def __init__(
        self,
        client: OracleClient,
        *,
        role_map: dict[str, str] | None = None,
        people: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.role_map = role_map or {}
        people = people or []
        self.by_employee = {str(p["employeeId"]): p for p in people if p.get("employeeId")}
        self.by_id = {str(p["id"]): p for p in people}
        self.suspended = 0
        self.unlinked = 0

    def describe(self) -> str:
        notes = []
        if self.suspended:
            notes.append(f"{self.suspended} suspended accounts left out")
        if self.unlinked:
            notes.append(f"{self.unlinked} accounts with no person left out")
        return "Oracle Fusion Cloud ERP" + (f"; {'; '.join(notes)}" if notes else "")

    def fingerprint(self) -> str | None:
        return "oracle-erp"

    def read(self, schema: list[dict]) -> Iterable[Record]:
        seen: dict[str, str] = {}
        for a in self.client.pages(f"{HCM}/userAccounts", {"expand": "userAccountRoles"}):
            if a.get("SuspendedFlag"):
                self.suspended += 1
                continue
            pn = str(a.get("PersonNumber") or "").strip()
            if not pn:  # integration and system users: not people
                self.unlinked += 1
                continue
            if pn in seen:
                self.problems.append(
                    f"Oracle accounts {seen[pn]} and {a.get('Username')} both belong to person {pn}"
                )
                continue
            seen[pn] = a.get("Username") or a.get("GUID") or pn
            codes = [r.get("RoleCode") for r in _children(a.get("userAccountRoles")) if r.get("RoleCode")]
            yield Record(
                ref=f"Oracle user account {a.get('Username')}",
                data={"account": a, "codes": codes},
                type="people",
            )

    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if record.type != "people":
            raise MappingError(f"unexpected record type {record.type}", record.ref)
        a = record.data["account"]
        pn = str(a["PersonNumber"]).strip()
        user = str(a.get("Username") or "")
        local = local_id(user)
        known = self.by_employee.get(pn) or (self.by_id.get(local) if local else None)
        pid = known["id"] if known else pn
        entity: dict = {"id": pid, "name": (known or {}).get("name") or user or pn}
        roles = sorted({self.role_map[c] for c in record.data["codes"] if c in self.role_map})
        if roles:
            entity["rolesFilled"] = roles if len(roles) > 1 else roles[0]
        return [Item(type="people", entity=entity, ref=record.ref, observed_at=_now())]


# ---------------------------------------------------------------------------- exceptions


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def _list(v) -> list:
    if v in (None, ""):
        return []
    return v if isinstance(v, list) else [x.strip() for x in str(v).split(";") if x.strip()]


def exceptions(
    client: OracleClient,
    since: date,
    *,
    people: list[dict],
    authorities: list[dict],
    co_approvals: list[dict],
) -> list[dict]:
    """Approval exceptions since ``since``, as findings for VeroMesh."""
    out: list[dict] = []
    day = since.isoformat()
    by_number = {str(p["id"]): p for p in people} | {
        str(p["employeeId"]): p for p in people if p.get("employeeId")
    }

    for po in client.pages(f"{FSCM}/purchaseOrders", {"q": f"CreationDate>='{day}'"}):
        buyer, approver = po.get("BuyerPersonNumber"), po.get("ApproverPersonNumber")
        if buyer and approver and buyer == approver:
            p = by_number.get(str(buyer))
            out.append(
                {
                    "kind": "self-approved-po",
                    "severity": "high",
                    "detail": f"Purchase order {po['OrderNumber']} ({po.get('Total')} {po.get('Currency', '')}) "
                    f"was raised and approved by the same person, {p.get('name') if p else buyer}.".replace(
                        " )", ")"
                    ),
                    **({"subject": {"type": "people", "id": p["id"]}} if p else {}),
                    "about": {"po": po["OrderNumber"], "person": str(buyer), "amount": po.get("Total")},
                }
            )

    # Who may approve how much today: each role's largest invoice-approval limit, and co-approval tiers.
    limit: dict[str, Decimal] = {}
    for a in authorities:
        if INVOICE_ACTION not in _list(a.get("actionsCovered")) or not a.get("holder"):
            continue
        lim = _dec(a.get("limitAmount"))
        for h in _list(a.get("holder")):
            if lim is not None and lim > limit.get(h, Decimal(-1)):
                limit[h] = lim
    tiers = {}
    for c in co_approvals:
        amt = _dec(c.get("aboveAmount"))
        auth = next((a for a in authorities if a.get("id") == c.get("authority")), None)
        if amt is None or auth is None or INVOICE_ACTION not in _list(auth.get("actionsCovered")):
            continue
        for h in _list(auth.get("holder")):
            tiers[h] = min(amt, tiers.get(h, amt))
    # ERP user name -> the person: by person number (an employee ID VeroMesh knows), else by the
    # user name's local part (a contractor, as Entra ID names them).
    users: dict[str, dict] = {}
    for a in client.pages(f"{HCM}/userAccounts"):
        user = str(a.get("Username") or "")
        p = by_number.get(str(a.get("PersonNumber") or "")) or by_number.get(local_id(user))
        if user and p:
            users[user.upper()] = p
    approvals: dict[str, list[dict]] = {}
    for h in client.pages(
        f"{FSCM}/invoiceApprovalsAndNotificationsHistory", {"q": f"LastUpdateDate>='{day}'"}
    ):
        if h.get("Action") == "Approved" and h.get("Approver"):
            approvals.setdefault(h["InvoiceNumber"], []).append(h)
    for inv_no, steps in sorted(approvals.items()):
        amount = _dec(steps[0].get("ReviewedAmount"))
        if amount is None:
            continue
        approvers = []
        for s in steps:
            approvers.append((s["Approver"], users.get(str(s["Approver"]).upper())))
        first_user, first = approvers[0]
        roles = _list((first or {}).get("rolesFilled"))
        best = max((limit[r] for r in roles if r in limit), default=None)
        who = (first or {}).get("name") or first_user
        if first is not None and limit and (best is None or amount > best):
            out.append(
                {
                    "kind": "approved-over-limit",
                    "severity": "high",
                    "detail": f"Invoice {inv_no} ({amount}) was approved by {who}, "
                    + (
                        f"whose roles allow up to {best}."
                        if best is not None
                        else "whose roles hold no invoice-approval authority."
                    ),
                    "subject": {"type": "people", "id": first["id"]},
                    "about": {
                        "invoice": inv_no,
                        "amount": str(amount),
                        "approver": first["id"],
                        "limit": str(best) if best is not None else None,
                    },
                }
            )
        over = min((tiers[r] for r in roles if r in tiers), default=None)
        if over is not None and amount > over and len({u for u, _ in approvers}) < 2:
            out.append(
                {
                    "kind": "missing-co-approval",
                    "severity": "high",
                    "detail": f"Invoice {inv_no} ({amount}) is above the co-approval amount {over} and had one approver, {who}.",
                    **({"subject": {"type": "people", "id": first["id"]}} if first else {}),
                    "about": {
                        "invoice": inv_no,
                        "amount": str(amount),
                        "approver": (first or {}).get("id") or first_user,
                    },
                }
            )
    return out
