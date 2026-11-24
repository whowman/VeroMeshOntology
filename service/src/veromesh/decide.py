"""The decision check: may this agent take this action, for its principal, now?

Answers **allow**, **consult** (named owners first) or **refuse**, with reasons,
and appends the decision to the decision log. Advisory: an agent can skip it;
enforcement at a gateway comes later.

The steps, all on the effective model (what VeroMesh believes now):

1. **For whom.** The agent acts for exactly one person (its principal). With an
   ``hr`` source registered, the principal must be employed: an hr source lists them,
   or, with no employee ID, an ``iam`` source does (a contractor). Otherwise refuse.
   Employed but with no account in any iam source (disabled): their agents consult the
   next authority up, skipping the principal's own (issue #67, decided 23 November).
2. **Set up for it.** The agent is registered to perform the action.
3. **Authority.** An authority the agent holds covers the action, is valid on
   the day, is in the amount's currency and its limit is not exceeded; its
   parent authorities (up to the one the principal holds) are valid and cover
   the action too. Above the agent's limit: consult the next authority up, the
   role whose authority for the action covers the amount with the smallest limit
   (a role with people in it first), and its co-approvers above their amounts.
   Only when no authority covers the amount: refuse, and escalate to the principal
   (decided 23 November, issue #68).
4. **Attested facts only.** Every fact behind that authority is attested and
   current (provenance.authority_basis); otherwise refuse.
5. **Co-approval.** A co-approval tier on the authority or a parent applies
   above its amount: consult the approver role.
6. **Impact.** For each KPI or capability the action affects, compare the
   action with the materiality threshold: below it, act and inform the
   owner; above it, consult the owner first. Thresholds in money
   ("USD per invoice") compare the amount; thresholds per day ("invoices per
   day") count today's allowed decisions for the action in the log. A
   threshold that cannot be evaluated is treated as material: consult.

Outcome: refuse if any step refuses; else consult if anyone must be consulted;
else allow (informing the owners below their thresholds).
"""

from __future__ import annotations

import contextlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from . import changesets, clock, domains, followup, incremental
from . import entities as E
from . import provenance as prov
from .decisions import DecisionLog
from .rdf_store import RDFStore

MONEY_UNIT = re.compile(r"^\s*([A-Za-z]{3})\b")  # "USD per invoice"
PER_DAY_UNIT = re.compile(r"\bper\s+day\s*$", re.IGNORECASE)  # "invoices per day"
OWNER_FIELDS = ("ownerRole", "businessOwner", "stewardRole")


class Model:
    """The effective model, read once per check. The entities are shared with other
    readers (changesets.shared_entities): a check reads them and never changes them."""

    def __init__(self, store: RDFStore, base: str) -> None:
        self._by_type: dict[str, tuple[list[dict], dict[str, dict]]] = {}
        self._index: dict[tuple[str, str], dict[str, list[dict]]] = {}
        self._store, self._base = store, base

    def _load(self, type_: str) -> tuple[list[dict], dict[str, dict]]:
        if type_ not in self._by_type:
            self._by_type[type_] = changesets.shared_entities(self._store, type_, self._base)
        return self._by_type[type_]

    def all(self, type_: str) -> list[dict]:
        return self._load(type_)[0]

    def get(self, type_: str, rid: str | None) -> dict | None:
        return self._load(type_)[1].get(rid) if rid else None

    def by(self, type_: str, field: str, *values) -> list[dict]:
        """Entities of ``type_`` whose ``field`` is (or, for a list, contains) one of
        ``values``, in model order: a lookup, not a scan of every entity."""
        key = (type_, field)
        if key not in self._index:
            self._index[key] = changesets.shared_index(self._store, type_, self._base, field)
        index = self._index[key]
        if len(values) == 1:
            return index.get(values[0], [])
        pos = {id(e): i for i, e in enumerate(self.all(type_))} if len(values) > 1 else {}
        found = {id(e): e for v in values for e in index.get(v, [])}
        return sorted(found.values(), key=lambda e: pos[id(e)])

    def name(self, type_: str, rid: str) -> str:
        e = self.get(type_, rid)
        return (e or {}).get("name") or rid

    def people_in(self, role: str) -> list[dict]:
        return [{"id": p["id"], "name": p.get("name")} for p in self.by("people", "rolesFilled", role)]

    def sources_holding(self, type_: str, rid: str) -> set[str]:
        """The source systems that state this entity (one query, not a read of every claim)."""
        iri = E.iri_for(self._base, rid)
        cls = E.TYPES[type_].cls
        res = self._store.query(
            f"""SELECT DISTINCT ?g WHERE {{ GRAPH ?g {{ <{iri}> a <{cls}> }}
                FILTER(STRSTARTS(STR(?g), "{changesets.SOURCE_GRAPH_BASE}")) }}"""
        )
        return {b["g"]["value"][len(changesets.SOURCE_GRAPH_BASE) :] for b in res["results"]["bindings"]}

    def employment(self, pid: str) -> str:
        """Whether a person is employed, decided by the authoritative source (issue #67).

        With an ``hr`` source registered: ``employed`` while an hr source lists them;
        ``contractor`` when only an ``iam`` source lists them and they have no employee
        ID (Entra ID is authoritative for contractors; decided 23 November);
        ``no-access`` when employed but an iam source exists and lists no enabled account
        for them; ``ended`` otherwise. With no hr source: ``unknown`` (no rule applies)."""
        cats = prov.categories(self._store)
        emp = domains.activate(self._store)["employment"]
        person = self.get("people", pid) or {}
        return employment_status(
            self.sources_holding("people", pid), cats, emp, bool(person.get("employeeId"))
        )

    def owner_roles(self, type_: str, rid: str) -> list[str]:
        e = self.get(type_, rid) or {}
        return sorted({e[k] for k in OWNER_FIELDS if e.get(k)})


def employment_status(holding: set[str], cats: dict[str, str], emp: dict, has_employee_id: bool) -> str:
    """Whether a person is employed, from the sources that hold them (Model.employment,
    and the findings, which judge everyone at once)."""
    hr = {s for s, c in cats.items() if c in (emp.get("authoritative") or [])}
    if not hr:
        return "unknown"
    contractors = emp.get("contractors")
    iam = {s for s, c in cats.items() if c == "iam"}
    if holding & hr:
        if iam and not holding & iam and has_employee_id:
            return "no-access"
        return "employed"
    if contractors and not has_employee_id and any(cats.get(x) == contractors for x in holding):
        return "contractor"
    return "ended"


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"'{value}' is not an amount.") from exc


def _money(d: Decimal) -> str:
    return f"{d:,.2f}".rstrip("0").rstrip(".") if d == d.to_integral_value() else f"{d:,.2f}"


def _valid(a: dict, day: date) -> str | None:
    """Why ``a`` is not valid on ``day``, or None."""
    if a.get("validFrom") and day < date.fromisoformat(a["validFrom"]):
        return f"starts on {a['validFrom']}"
    if a.get("validUntil") and day > date.fromisoformat(a["validUntil"]):
        return f"ended on {a['validUntil']}"
    return None


def _next_authority(m: Model, action: str, amt, cur, day: date, skip: set[str]) -> dict | None:
    """The authority a role holds for ``action`` that covers ``amt`` on ``day``, with the
    smallest limit: the next authority up from the agent's. A role with people in it
    comes before a vacant one, so a decision does not wait on a post nobody fills."""
    found = []
    for a in m.by("authorities", "actionsCovered", action):
        holder = a.get("holder")
        if a["id"] in skip or not holder or m.get("roles", holder) is None:
            continue
        if any(_valid(x, day) for x in _chain(m, a)):
            continue
        limit = _dec(a.get("limitAmount"))
        if (
            amt is not None
            and limit is not None
            and ((a.get("currency") or "").upper() != cur or amt > limit)
        ):
            continue  # with no amount, any authority for the action covers it
        found.append((not m.people_in(holder), limit is None, limit or Decimal(0), a["id"], a))
    return min(found, key=lambda f: f[:4])[4] if found else None


def _chain(m: Model, auth: dict) -> list[dict]:
    """The authority and its parents, nearest first (stops on a loop or a missing parent)."""
    out, seen = [auth], {auth["id"]}
    while out[-1].get("parentAuthority") and out[-1]["parentAuthority"] not in seen:
        parent = m.get("authorities", out[-1]["parentAuthority"])
        if parent is None:
            break
        out.append(parent)
        seen.add(parent["id"])
    return out


def _consult_entry(m: Model, role: str, why: str) -> dict:
    return {"role": role, "roleName": m.name("roles", role), "people": m.people_in(role), "why": why}


def check(
    store: RDFStore,
    base: str,
    log: DecisionLog,
    *,
    agent: str,
    action: str,
    subject: str,
    amount=None,
    currency: str | None = None,
    on: date | None = None,
    note: str | None = None,
    require_attested: bool = True,
) -> dict:
    """Decide, append the decision to the log, and return the log entry plus who to inform."""
    domains.activate(store)
    incremental.warm(store, base)  # follow what other copies changed, without reading the model
    pin = store.pinned() if hasattr(store, "pinned") else contextlib.nullcontext()
    with pin:  # one version read for the whole check, not one per cached read
        return _check(store, base, log, agent, action, subject, amount, currency, on, note, require_attested)


def _check(store, base, log, agent, action, subject, amount, currency, on, note, require_attested) -> dict:
    m = Model(store, base)
    day = on or clock.today()
    amt = _dec(amount)
    cur = currency.upper() if currency else None
    if amt is not None and amt < 0:
        raise ValueError("The amount cannot be negative.")
    if amt is not None and not cur:
        raise ValueError("Give the currency with the amount.")

    refuse: list[str] = []
    consult: list[dict] = []
    inform: list[dict] = []
    reasons: list[str] = []
    escalate: dict | None = None
    used: str | None = None
    basis: dict = {"onDate": day.isoformat()}

    # 1. For whom
    ag = m.get("agents", agent)
    if ag is None:
        raise prov_not_found(f"No agent '{agent}' in the model.")
    principal = ag.get("actsFor")
    suspended = False
    if not principal or m.get("people", principal) is None:
        refuse.append(f"{m.name('agents', agent)} acts for nobody in the model.")
    else:
        status = m.employment(principal)
        basis["principalStatus"] = status
        if status == "ended":
            refuse.append(
                f"Its principal, {m.name('people', principal)}, is no longer employed: "
                "no HR source lists them."
            )
        elif status == "no-access":
            suspended = True  # employed, but their account is disabled: skip to the next authority up
    act = m.get("actions", action)
    if act is None:
        raise prov_not_found(f"No action '{action}' in the model.")
    act_name = act.get("name") or action

    # 2. Set up for it
    if action not in ag.get("performsActions", []):
        refuse.append(f"{ag.get('name') or agent} is not registered to {act_name.lower()}.")

    # 3. Authority
    held = [a for a in m.by("authorities", "holder", agent) if action in a.get("actionsCovered", [])]
    if not held and not refuse:
        refuse.append(f"No authority the agent holds covers '{act_name}'.")
    problems: dict[str, list[str]] = {}
    for a in held:
        why: list[str] = []
        for link in _chain(m, a):
            label = (
                "its authority"
                if link is a
                else f"the authority it derives from ({link.get('name') or link['id']})"
            )
            v = _valid(link, day)
            if v:
                why.append(f"{label} {v}")
            if action not in link.get("actionsCovered", []):
                why.append(f"{label} does not cover '{act_name}'")
        limit = _dec(a.get("limitAmount"))
        if amt is not None and limit is not None:
            if (a.get("currency") or "").upper() != cur:
                why.append(f"its limit is in {a.get('currency')}, the amount in {cur}")
            elif amt > limit:
                why.append(f"{_money(amt)} {cur} is above its limit of {_money(limit)} {cur}")
        problems[a["id"]] = why
        if not why and used is None:
            used = a["id"]
    # A suspended principal (employed, account disabled) cannot be the authority behind the
    # agent: skip their own authorities and go to the next authority up (decided 23 November).
    principal_auths: set[str] = set()
    if suspended:
        mine = (m.get("people", principal) or {}).get("rolesFilled") or []
        mine = {mine} if isinstance(mine, str) else set(mine)
        principal_auths = {
            a["id"] for a in m.by("authorities", "actionsCovered", action) if a.get("holder") in mine
        }
    if used is None and held and not refuse:
        a = held[0]
        over_limit = any("above its limit" in w for w in problems[a["id"]])
        only_limit = over_limit and all("above its limit" in w for w in problems[a["id"]])
        skip = {x["id"] for x in held} | principal_auths
        nxt = _next_authority(m, action, amt, cur, day, skip=skip) if only_limit and amt is not None else None
        if nxt is not None:
            why = (
                f"{'; '.join(problems[a['id']])} (the agent's authority); within "
                f"'{nxt.get('name') or nxt['id']}'"
            )
            consult.append(_consult_entry(m, nxt["holder"], why))
            for t in m.by("co-approvals", "authority", *[x["id"] for x in _chain(m, nxt)]):
                above = _dec(t.get("aboveAmount"))
                if above is not None and amt is not None and amt > above:
                    consult.append(
                        _consult_entry(
                            m, t["approverRole"], f"co-approval required above {_money(above)} {cur}"
                        )
                    )
            reasons.append(
                f"Above the agent's authority: {'; '.join(problems[a['id']])}. "
                f"Consult the next authority up: {m.name('roles', nxt['holder'])}."
            )
            basis["nextAuthority"] = nxt["id"]
        else:
            refuse.append(f"Not within the agent's authority: {'; '.join(problems[a['id']])}.")
            if over_limit and principal:
                escalate = {
                    "person": principal,
                    "name": m.name("people", principal),
                    "why": "above the agent's limit, and no authority covers the amount",
                }

    if suspended and not refuse and "nextAuthority" not in basis:
        nxt = _next_authority(m, action, amt, cur, day, skip={x["id"] for x in held} | principal_auths)
        who = m.name("people", principal)
        if nxt is not None:
            consult.append(
                _consult_entry(
                    m,
                    nxt["holder"],
                    f"its principal, {who}, is employed but their account is disabled; "
                    f"within '{nxt.get('name') or nxt['id']}'",
                )
            )
            reasons.append(
                f"Its principal, {who}, is employed but has no active account: "
                f"consult the next authority up, {m.name('roles', nxt['holder'])}."
            )
            basis["nextAuthority"] = nxt["id"]
        else:
            refuse.append(
                f"Its principal, {who}, is employed but has no active account, and no other authority covers this."
            )

    if used:
        auth = m.get("authorities", used)
        lim = _dec(auth.get("limitAmount"))
        reasons.append(
            f"Within '{auth.get('name') or used}'"
            + (f" (limit {_money(lim)} {auth.get('currency')})" if lim is not None else "")
            + f", valid on {day.isoformat()}."
        )
        basis["authority"] = used
        # 4. Attested facts only
        b = prov.authority_basis(store, used)
        basis["authorityConfirmed"] = b["confirmed"]
        if not b["confirmed"]:
            basis["unconfirmed"] = b["reasons"]
            if require_attested:
                refuse.append(
                    "The facts behind this authority are not all attested and current, so the agent may not rely on it: "
                    + "; ".join(b["reasons"][:5])
                    + (f" (and {len(b['reasons']) - 5} more)" if len(b["reasons"]) > 5 else "")
                    + "."
                )
        # 5. Co-approval
        if amt is not None:
            chain_ids = [x["id"] for x in _chain(m, auth)]
            for t in m.by("co-approvals", "authority", *chain_ids):
                above = _dec(t.get("aboveAmount"))
                if above is not None and amt > above:
                    consult.append(
                        _consult_entry(
                            m, t["approverRole"], f"co-approval required above {_money(above)} {cur}"
                        )
                    )

    # 6. Impact
    if not refuse:
        impacts = m.by("impacts", "action", action)
        with_threshold = {i.get("impactOn") for i in impacts}
        done_today: int | None = None
        for i in impacts:
            target = i.get("impactOn")
            ttype = "kpis" if m.get("kpis", target) else "capabilities"
            owners = m.owner_roles(ttype, target)
            thr, unit = _dec(i.get("materialityThreshold")), i.get("thresholdUnit") or ""
            tname = m.name(ttype, target)
            money, per_day = MONEY_UNIT.match(unit), PER_DAY_UNIT.search(unit)
            if thr is None:
                material, why = True, f"no materiality threshold for {tname}"
            elif per_day:  # actions taken today: allowed, or consulted and then done
                if done_today is None:
                    done_today = followup.done_today(log, action, day)
                material = done_today + 1 > thr
                why = f"{done_today + 1} today against a threshold of {_money(thr)} {unit} on {tname}"
            elif money and amt is not None and money.group(1).upper() == cur:
                material = amt > thr
                why = f"{_money(amt)} {cur} against a threshold of {_money(thr)} {unit} on {tname}"
            else:
                material = True
                why = (
                    f"the threshold on {tname} ({_money(thr)} {unit}) cannot be checked against this request"
                )
            for role in owners or []:
                (consult if material else inform).append(
                    _consult_entry(m, role, ("material: " if material else "below materiality: ") + why)
                )
            if not owners:
                reasons.append(f"{tname} has no owner to inform or consult ({why}).")
        for target in act.get("affects", []):
            if target in with_threshold:
                continue
            ttype = "kpis" if m.get("kpis", target) else "capabilities"
            for role in m.owner_roles(ttype, target):
                consult.append(
                    _consult_entry(
                        m, role, f"{m.name(ttype, target)} has no materiality threshold for this action"
                    )
                )

    consult = _merge(consult)
    inform = [i for i in _merge(inform) if i["role"] not in {c["role"] for c in consult}]
    outcome = "refuse" if refuse else ("consult" if consult else "allow")
    reasons = (
        refuse + reasons
        if refuse
        else reasons + [f"Consult {c['roleName']}: {'; '.join(c['why'])}." for c in consult]
    )
    if outcome == "allow":
        reasons.append("Act, and inform: " + (", ".join(i["roleName"] for i in inform) or "nobody") + ".")
    basis["inform"] = inform
    if escalate:
        basis["escalateTo"] = escalate
    entry = log.append(
        {
            "kind": "decision",
            "subject": subject,
            "agent": agent,
            "principal": principal,
            "action": action,
            "amount": str(amt) if amt is not None else None,
            "currency": cur,
            "authority": used,
            "outcome": outcome,
            "reasons": reasons,
            "consult": consult,
            "basis": basis,
            "note": note,
        }
    )
    return {**entry, "inform": inform, "escalateTo": escalate}


def impact_of(store: RDFStore, base: str, action: str) -> dict:
    """What an action affects, with each materiality threshold and the owners to inform or consult."""
    m = Model(store, base)
    act = m.get("actions", action)
    if act is None:
        raise prov_not_found(f"No action '{action}' in the model.")
    thresholds = {i.get("impactOn"): i for i in m.by("impacts", "action", action)}
    out = []
    for target in sorted(set(act.get("affects", [])) | set(thresholds)):
        ttype = "kpis" if m.get("kpis", target) else "capabilities"
        i = thresholds.get(target)
        out.append(
            {
                "type": ttype,
                "id": target,
                "name": m.name(ttype, target),
                "materialityThreshold": i.get("materialityThreshold") if i else None,
                "thresholdUnit": i.get("thresholdUnit") if i else None,
                "owners": [
                    {"role": r, "roleName": m.name("roles", r), "people": m.people_in(r)}
                    for r in m.owner_roles(ttype, target)
                ],
            }
        )
    return {"action": action, "name": act.get("name") or action, "affects": out}


def owners_of(store: RDFStore, base: str, type_: str, rid: str) -> dict:
    """The owner and steward roles of an entity, and the people who fill them."""
    m = Model(store, base)
    e = m.get(type_, rid)
    if e is None:
        raise prov_not_found(f"No {type_} with ID '{rid}'.")
    found = {k: e[k] for k in OWNER_FIELDS + ("technicalOwner",) if e.get(k)}
    return {
        "type": type_,
        "id": rid,
        "name": e.get("name") or rid,
        "owners": [
            {"field": k, "role": r, "roleName": m.name("roles", r), "people": m.people_in(r)}
            for k, r in found.items()
        ],
    }


def _merge(entries: list[dict]) -> list[dict]:
    """One entry per role, with every reason for it."""
    out: dict[str, dict] = {}
    for e in entries:
        cur = out.setdefault(e["role"], {**e, "why": []})
        cur["why"].append(e["why"]) if isinstance(e["why"], str) else cur["why"].extend(e["why"])
    return list(out.values())


def prov_not_found(msg: str) -> Exception:
    from .changesets import _not_found

    return _not_found(msg)
