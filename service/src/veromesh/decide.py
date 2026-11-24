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


def _objective_owners(m: Model, obj: dict, kpis: set[str]) -> list[str]:
    own = m.owner_roles("objectives", obj["id"])
    return own or sorted({r for k in kpis for r in m.owner_roles("kpis", k)})


def _ancestors(m: Model, oid: str) -> list[str]:
    out, seen = [], {oid}
    while True:
        parent = (m.get("objectives", oid) or {}).get("parentObjective")
        if not parent or parent in seen:
            return out
        out.append(parent)
        seen.add(parent)
        oid = parent


def _tradeoffs(m: Model, material_on: dict[str, bool]) -> list[dict]:
    """Pairs of conflicting objectives that both move with the action. Each names which has
    priority (the lower number), whom to consult (both owners) and who settles it if they
    disagree: the owner of their nearest common parent objective."""
    touched: dict[str, set[str]] = {}
    for k in material_on:
        if m.get("kpis", k) is None:
            continue
        for o in m.by("objectives", "measuredByKpis", k):
            touched.setdefault(o["id"], set()).add(k)
    out, seen = [], set()
    for a in sorted(touched):
        oa = m.get("objectives", a) or {}
        for b in sorted(touched):
            ob = m.get("objectives", b) or {}
            if a >= b or (a, b) in seen:
                continue
            if b not in _ids(oa.get("conflictsWithObjectives")) and a not in _ids(
                ob.get("conflictsWithObjectives")
            ):
                continue
            seen.add((a, b))
            pa, pb = _dec(oa.get("priority")), _dec(ob.get("priority"))
            first = (
                a
                if pa is not None and (pb is None or pa < pb)
                else b
                if pb is not None and (pa is None or pb < pa)
                else None
            )
            common = next((x for x in _ancestors(m, a) if x in set(_ancestors(m, b))), None)
            settler = m.owner_roles("objectives", common) if common else []
            owners = sorted(
                set(_objective_owners(m, oa, touched[a])) | set(_objective_owners(m, ob, touched[b]))
            )
            material = any(material_on.get(k) for k in touched[a] | touched[b])
            na, nb = m.name("objectives", a), m.name("objectives", b)
            why = f"{na} and {nb} pull against each other"
            if first:
                why += f"; {m.name('objectives', first)} has priority"
            else:
                why += "; neither has priority"
            if settler:
                why += f", and {m.name('objectives', common)} ({', '.join(m.name('roles', r) for r in settler)}) settles it"
            if material and not first:
                owners = sorted(set(owners) | set(settler))
            out.append(
                {
                    "objectives": [a, b],
                    "priority": first,
                    "settledBy": {"objective": common, "roles": settler} if common else None,
                    "material": material,
                    "owners": owners,
                    "why": why,
                }
            )
    return out


def _ids(v) -> list[str]:
    if not v:
        return []
    return v if isinstance(v, list) else [v]


def _degraded(m: Model, act: dict, used: str | None) -> list[dict]:
    """Capabilities the action touches whose health KPI is past its threshold."""
    caps = {t for t in _ids(act.get("affects")) if m.get("capabilities", t)}
    caps |= {
        i.get("impactOn")
        for i in m.by("impacts", "action", act["id"])
        if m.get("capabilities", i.get("impactOn"))
    }
    if act.get("partOfProcess"):
        caps |= {c["id"] for c in m.by("capabilities", "realizedByProcesses", act["partOfProcess"])}
    if used and (cap := (m.get("authorities", used) or {}).get("withinCapability")):
        caps.add(cap)
    out = []
    for cap in sorted(caps):
        for k in m.by("kpis", "healthOfCapabilities", cap):
            v, thr, better = _dec(k.get("currentValue")), _dec(k.get("healthThreshold")), k.get("betterWhen")
            if v is None or thr is None or better not in ("higher", "lower"):
                continue
            if (better == "higher" and v < thr) or (better == "lower" and v > thr):
                out.append(
                    {
                        "capability": cap,
                        "kpi": k["id"],
                        "value": str(v),
                        "threshold": str(thr),
                        "measuredOn": k.get("measuredOn"),
                        "why": f"{m.name('capabilities', cap)} is degraded: {m.name('kpis', k['id'])} is "
                        f"{_money(v)} {k.get('unit') or ''}".rstrip()
                        + f", past its health threshold of {_money(thr)}",
                    }
                )
    return out


def _use_case(m: Model, agent: str, action: str, day: date) -> tuple[dict | None, str]:
    """The current approved AI use case covering the agent for the action, or why there is none."""
    seen = []
    for uc in m.by("ai-use-cases", "coversAgents", agent):
        status = uc.get("status")
        allows = uc.get("allowsActions") or []
        allows = allows if isinstance(allows, list) else [allows]
        review = uc.get("reviewBy")
        if status not in ("approved", "conditional"):
            seen.append(f"{m.name('ai-use-cases', uc['id'])} is {status}")
        elif allows and action not in allows:
            seen.append(f"{m.name('ai-use-cases', uc['id'])} does not allow {action}")
        elif review and str(review) < day.isoformat():
            seen.append(f"{m.name('ai-use-cases', uc['id'])} was due for review on {review}")
        else:
            return uc, ""
    name = m.name("agents", agent)
    if not seen:
        return None, f"No approved AI use case covers {name}."
    return None, f"No current approved AI use case covers {name} for {action}: " + "; ".join(seen) + "."


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
    require_use_case: bool = False,
    unmapped_impact: str = "consult",
) -> dict:
    """Decide, append the decision to the log, and return the log entry plus who to inform.

    ``require_use_case``: refuse an agent no current approved AI use case covers for the action
    (the governance board's decision gates the agent). ``unmapped_impact``: what an action with
    no mapped impact gets: ``consult`` the owners of its process (the white paper: unknown
    impact is a reason to consult, not a licence to act) or ``allow``."""
    if unmapped_impact not in ("consult", "allow"):
        raise ValueError("unmapped_impact is consult or allow.")
    domains.activate(store)
    incremental.warm(store, base)  # follow what other copies changed, without reading the model
    pin = store.pinned() if hasattr(store, "pinned") else contextlib.nullcontext()
    with pin:  # one version read for the whole check, not one per cached read
        return _check(
            store,
            base,
            log,
            agent,
            action,
            subject,
            amount,
            currency,
            on,
            note,
            require_attested,
            require_use_case,
            unmapped_impact,
        )


def _check(
    store,
    base,
    log,
    agent,
    action,
    subject,
    amount,
    currency,
    on,
    note,
    require_attested,
    require_use_case=False,
    unmapped_impact="consult",
) -> dict:
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
    if hasattr(store, "version"):  # the published model this decision was made on (pinned for the check)
        basis["modelVersion"] = store.version()

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
    if require_use_case:
        uc, why_not = _use_case(m, agent, action, day)
        if uc is None:
            refuse.append(why_not)
        else:
            basis["useCase"] = uc["id"]
            if uc.get("status") == "conditional" and uc.get("conditions"):
                reasons.append(
                    f"Approved with conditions ({m.name('ai-use-cases', uc['id'])}): {uc['conditions']}"
                )

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
        material_on: dict[str, bool] = {}
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
            material_on[target] = material_on.get(target, False) or material
            for role in owners or []:
                (consult if material else inform).append(
                    _consult_entry(m, role, ("material: " if material else "below materiality: ") + why)
                )
            if not owners:
                reasons.append(f"{tname} has no owner to inform or consult ({why}).")
        for target in act.get("affects", []):
            if target in with_threshold:
                continue
            material_on[target] = True
            ttype = "kpis" if m.get("kpis", target) else "capabilities"
            for role in m.owner_roles(ttype, target):
                consult.append(
                    _consult_entry(
                        m, role, f"{m.name(ttype, target)} has no materiality threshold for this action"
                    )
                )

        # 7. Trade-offs: objectives the action moves that pull against each other.
        tradeoffs = _tradeoffs(m, material_on)
        for t in tradeoffs:
            for role in t["owners"]:
                (consult if t["material"] else inform).append(_consult_entry(m, role, t["why"]))
        if tradeoffs:
            basis["tradeoffs"] = [
                {k: v for k, v in t.items() if k not in ("owners", "why")} for t in tradeoffs
            ]
        # 8. Health: a degraded capability the action touches is its owner's to decide on.
        for d in _degraded(m, act, used):
            for role in m.owner_roles("capabilities", d["capability"]):
                consult.append(_consult_entry(m, role, d["why"]))
            basis.setdefault("degraded", []).append({k: v for k, v in d.items() if k != "why"})

        if not impacts and not act.get("affects") and unmapped_impact == "consult":
            # Unknown impact: consult whoever is accountable, nearest first: the owners of the
            # process the action is part of, else of the capability the agent's authority is
            # within, else the agent's principal (the person accountable for what it does).
            why = f"no impact is mapped for {act_name.lower()}, so its impact is unknown"
            proc = act.get("partOfProcess")
            owners = m.owner_roles("processes", proc) if proc else []
            if not owners and used:
                cap = (m.get("authorities", used) or {}).get("withinCapability")
                owners = m.owner_roles("capabilities", cap) if cap else []
            if not owners and principal:
                roles_ = (m.get("people", principal) or {}).get("rolesFilled") or []
                owners = sorted(roles_ if isinstance(roles_, list) else [roles_])[:1]
            for role in owners:
                consult.append(_consult_entry(m, role, why))
            if not owners:
                refuse.append(
                    f"No impact is mapped for {act_name.lower()} and nobody accountable can be consulted; "
                    "map its impact or set unmapped impact to allow."
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


def data_change_impact(
    store: RDFStore, base: str, product: str | None = None, *, urn: str | None = None
) -> dict:
    """A data product is about to change (a column renamed, a definition changed, a pipeline
    moved): everything downstream of it, and whom to tell (design checkpoint gap 3, the
    Delivery Plan's Gate 3). Follows *feeds* to every downstream data product, then the KPIs
    computed from them, the objectives those KPIs measure, the capabilities and agents that
    consume them, and routes the change to each owner role with the reasons."""
    m = Model(store, base)
    if urn and not product:
        hits = m.by("data-products", "catalogUrn", urn)
        if not hits:
            raise prov_not_found(f"No data product with catalog URN '{urn}'.")
        product = hits[0]["id"]
    start = m.get("data-products", product or "")
    if start is None:
        raise prov_not_found(f"No data product '{product}' in the model.")
    affected, order, todo = {start["id"]}, [start["id"]], [start["id"]]
    while todo:
        for nxt in _ids((m.get("data-products", todo.pop(0)) or {}).get("feedsDataProducts")):
            if nxt not in affected and m.get("data-products", nxt):
                affected.add(nxt)
                order.append(nxt)
                todo.append(nxt)
    route: list[dict] = []

    def tell(role: str | None, why: str) -> None:
        if role:
            route.append(_consult_entry(m, role, why))

    pname = m.name("data-products", start["id"])
    for dp in order:
        e = m.get("data-products", dp) or {}
        what = "the data product that changes" if dp == start["id"] else f"downstream of {pname}"
        tell(e.get("ownerRole"), f"owns {m.name('data-products', dp)}, {what}")
        tell(e.get("stewardRole"), f"stewards {m.name('data-products', dp)}, {what}")
        for sys in _ids(e.get("runsOnSystems")):
            tell(
                (m.get("systems", sys) or {}).get("technicalOwner"),
                f"runs {m.name('systems', sys)}, where {m.name('data-products', dp)} lives",
            )
    kpis = sorted({k["id"] for dp in order for k in m.by("kpis", "computedFromDataProducts", dp)})
    for k in kpis:
        tell(
            (m.get("kpis", k) or {}).get("ownerRole"),
            f"owns {m.name('kpis', k)}, computed from data that changes",
        )
    objectives = sorted({o["id"] for k in kpis for o in m.by("objectives", "measuredByKpis", k)})
    for o in objectives:
        tell(
            (m.get("objectives", o) or {}).get("ownerRole"),
            f"owns {m.name('objectives', o)}, measured by a KPI that changes",
        )
    caps = sorted({c["id"] for dp in order for c in m.by("capabilities", "consumesDataProducts", dp)})
    for c in caps:
        tell(
            (m.get("capabilities", c) or {}).get("ownerRole"),
            f"owns {m.name('capabilities', c)}, which depends on data that changes",
        )
    agents = []
    for a in sorted({a["id"] for dp in order for a in m.by("agents", "consumesDataProducts", dp)}):
        ag = m.get("agents", a) or {}
        agents.append({"id": a, "name": m.name("agents", a), "actsFor": ag.get("actsFor")})
        for r in _ids((m.get("people", ag.get("actsFor") or "") or {}).get("rolesFilled"))[:1]:
            tell(r, f"{m.name('agents', a)} acts for them and consumes data that changes")

    def named(type_, ids):
        return [{"id": i, "name": m.name(type_, i)} for i in ids]

    return {
        "dataProduct": {"id": start["id"], "name": pname},
        "downstream": named("data-products", order[1:]),
        "kpis": named("kpis", kpis),
        "objectives": named("objectives", objectives),
        "capabilities": named("capabilities", caps),
        "agents": agents,
        "notify": sorted(_merge(route), key=lambda e: e["role"]),
    }


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
