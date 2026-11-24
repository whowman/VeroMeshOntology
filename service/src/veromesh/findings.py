"""Findings: what the model shows needs someone's attention, routed to its owner (issue #67, step 2d).

Each finding names its kind, a severity, what it is about, the source behind it and
the **owner role** it goes to, with the people who fill that role today. Most are worked
out from the model on request; connectors that read a system's transactions report the
rest (``reported``), for example purchase orders their buyer approved.

Worked out from the model:

- ``access-without-employment`` (high): no HR source lists the person, yet a system
  that grants access (an ``iam`` or ``erp`` source) still does. A leaver whose account
  or ERP role outlived their employment, or someone who never had an HR record. One
  finding per system, to that system's owner. Contractors (no employee ID, held by the
  employment domain's ``contractors`` category) are not findings.
- ``employed-without-access`` (info): HR lists them, an identity system is registered,
  and it holds no enabled account for them. Their agents consult the next authority up.
- ``out-of-sync`` (medium): two sources that both decide a domain state different
  values (the AP workflow's limit against the governance workbook's), or a source that
  takes a domain from another (``derivedFrom`` on the source) no longer matches it.
  The value in force is the winner's; the finding goes to the owner of the source in
  force, or of the derived source. Other disagreements stay in the steward's conflict
  queue (``GET /api/v1/conflicts``).
- ``ended-reference`` (medium): an entity its source has dropped (kept by the ``ended``
  source so references hold) is still named by another entity, for example a leaver
  still named as a manager or an authority's delegator. To the owner of the source that
  names them.
- ``domain-without-authority`` (info): the model holds facts in a domain, and no
  registered source is in a category that decides it. To administrators.
- ``owner-has-left`` (high): everyone who fills an owner role of something (a system's
  business or technical owner, a KPI's, a data product's...) has left: HR no longer lists them,
  while another source (the CMDB, the workbook) still names them. To the owning source's owner.
- ``capability-degraded`` (high): a KPI that shows a capability's health is past its health
  threshold. To the capability's owner: agents acting on it consult them until it recovers.
- ``objective-conflict-unsettled`` (medium): two objectives conflict, neither has priority,
  and they share no parent objective whose owner could settle it. To both owners.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from . import changesets, domains
from . import entities as E
from . import provenance as P
from .decide import employment_status
from .modelcache import cached
from .ontology import literal
from .rdf_store import RDFStore

FINDINGS_GRAPH = "https://veromesh.ai/graph/findings"
REPORTED = "https://veromesh.ai/ontology/provenance#reportedFindings"
SEVERITIES = ("high", "medium", "info")
#: Source categories whose systems let a person act (sign in, approve).
ACCESS_CATEGORIES = ("iam", "erp")
KIND = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
MAX_REPORTED = 10_000


def _fid(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _same(a, b) -> bool:
    def norm(v):
        if isinstance(v, list):
            return sorted(norm(x) for x in v)
        try:
            return str(Decimal(str(v)).normalize())
        except (InvalidOperation, ValueError):
            return str(v)

    return norm(a) == norm(b)


# ---------------------------------------------------------------------------- reported by connectors


def check_reported(items: list) -> list[dict]:
    """Validate findings a connector reports; returns them cleaned."""
    if not isinstance(items, list):
        raise ValueError("Send a list of findings.")
    if len(items) > MAX_REPORTED:
        raise ValueError(f"At most {MAX_REPORTED} findings per source.")
    out = []
    for i, f in enumerate(items):
        if not isinstance(f, dict):
            raise ValueError(f"Finding {i}: an object.")
        if not KIND.match(str(f.get("kind", ""))):
            raise ValueError(f"Finding {i}: kind is lower-case words joined by '-'.")
        if not isinstance(f.get("detail"), str) or not f["detail"].strip():
            raise ValueError(f"Finding {i}: detail (what was found, in words) is required.")
        sev = f.get("severity", "medium")
        if sev not in SEVERITIES:
            raise ValueError(f"Finding {i}: severity is one of {', '.join(SEVERITIES)}.")
        subj = f.get("subject")
        if subj is not None:
            if not isinstance(subj, dict) or subj.get("type") not in E.TYPES or not subj.get("id"):
                raise ValueError(f"Finding {i}: subject is {{type, id}} with a model type.")
        about = f.get("about", {})
        if not isinstance(about, dict):
            raise ValueError(f"Finding {i}: about is an object.")
        out.append(
            {
                "kind": f["kind"],
                "severity": sev,
                "detail": f["detail"].strip()[:1000],
                **({"subject": {"type": subj["type"], "id": str(subj["id"])}} if subj else {}),
                "about": about,
                **({"ownerRole": str(f["ownerRole"])} if f.get("ownerRole") else {}),
            }
        )
    return out


def set_reported(store: RDFStore, source: str, items: list) -> int:
    """Replace what ``source`` reports (each run states everything it found)."""
    from .changesets import check_source

    check_source(source)
    cleaned = check_reported(items)
    s = f"https://veromesh.ai/findings/reported/{source}"
    payload = json.dumps(
        {"reportedAt": datetime.now(UTC).isoformat(timespec="seconds"), "findings": cleaned}, sort_keys=True
    )
    store.update(
        f"""DELETE {{ GRAPH <{FINDINGS_GRAPH}> {{ <{s}> ?p ?o }} }} WHERE {{ GRAPH <{FINDINGS_GRAPH}> {{ <{s}> ?p ?o }} }} ;
            INSERT DATA {{ GRAPH <{FINDINGS_GRAPH}> {{
              <{s}> <{P.VMP.name}> {literal(source)} ; <{REPORTED}> {literal(payload)} . }} }}"""
    )
    return len(cleaned)


def _reported(store: RDFStore) -> dict[str, dict]:
    res = store.query(
        f"""SELECT ?name ?v WHERE {{ GRAPH <{FINDINGS_GRAPH}> {{ ?s <{P.VMP.name}> ?name ; <{REPORTED}> ?v }} }}"""
    )
    return {b["name"]["value"]: json.loads(b["v"]["value"]) for b in res["results"]["bindings"]}


# ---------------------------------------------------------------------------- worked out


def _compute(store: RDFStore) -> list[dict]:
    doms = domains.activate(store)
    cats = P.categories(store)
    reg = P.registry(store)
    resolved = P.resolve(P.read_claims(store), P.read_attestations(store))
    fd = domains.field_domains(doms)
    emp = doms["employment"]
    out: list[dict] = []

    def owner(src: str | None) -> str | None:
        return (reg.get(src) or {}).get("ownerRole") if src else None

    def name(t: str, rid: str) -> str:
        r = resolved.get((t, rid))
        return (r.values.get("name") if r else None) or rid

    def add(kind, severity, subject, detail, *, source=None, owner_role=None, about=None, key=()):
        out.append(
            {
                "id": _fid(kind, subject, source, *key),
                "kind": kind,
                "severity": severity,
                "subject": {**subject, "name": name(subject["type"], subject["id"])},
                "detail": detail,
                "source": source,
                "ownerRole": owner_role,
                "about": about or {},
            }
        )

    iam_sources = sorted(s for s, c in cats.items() if c == "iam")
    status_of: dict[str, str] = {}
    for (t, rid), r in sorted(resolved.items()):
        if t != "people":
            continue
        holding = {c.source for c in r.claims} - {P.ENDED_SOURCE}
        if not holding:
            continue
        status = employment_status(holding, cats, emp, bool(r.values.get("employeeId")))
        status_of[rid] = status
        who = r.values.get("name") or rid
        if status == "ended":
            for src in sorted(holding):
                if cats.get(src) in ACCESS_CATEGORIES:
                    add(
                        "access-without-employment",
                        "high",
                        {"type": "people", "id": rid},
                        f"No HR source lists {who}, but {src} ({cats[src]}) still gives them access.",
                        source=src,
                        owner_role=owner(src),
                        about={"category": cats[src]},
                    )
        elif status == "no-access" and iam_sources:
            add(
                "employed-without-access",
                "info",
                {"type": "people", "id": rid},
                f"{who} is employed, but no identity system ({', '.join(iam_sources)}) holds an enabled account "
                "for them. Their agents consult the next authority up.",
                source=iam_sources[0],
                owner_role=owner(iam_sources[0]),
            )

    for (t, rid), r in sorted(resolved.items()):
        for f, cands in sorted(r.conflicts.items()):
            dom = fd.get((t, f))
            if not dom:
                continue
            auth = doms[dom].get("authoritative") or []
            win = r.winners[f]
            others = [
                c for c in cands if c.source != win.source and not _same(c.values.get(f), win.values.get(f))
            ]
            derived = [
                (c, o)
                for c in cands
                for o in cands
                if (reg.get(c.source) or {}).get("derivedFrom", {}).get(dom) == o.source
                and not _same(c.values.get(f), o.values.get(f))
            ]
            if derived:
                for c, o in derived:
                    add(
                        "out-of-sync",
                        "medium",
                        {"type": t, "id": rid},
                        f"{t} {rid} {f}: {c.source} takes {dom} from {o.source}, but states {c.values.get(f)} "
                        f"where {o.source} states {o.values.get(f)}.",
                        source=c.source,
                        owner_role=owner(c.source),
                        about={
                            "domain": dom,
                            "field": f,
                            "derivedFrom": o.source,
                            "inForce": r.values.get(f),
                        },
                        key=(f, o.source),
                    )
                continue
            if win.category in auth and any(
                c.category in auth and c.category != win.category for c in others
            ):
                for c in others:
                    if c.category not in auth:
                        continue
                    add(
                        "out-of-sync",
                        "medium",
                        {"type": t, "id": rid},
                        f"{t} {rid} {f}: {win.source} ({win.category}, in force) states {win.values.get(f)}; "
                        f"{c.source} ({c.category}) states {c.values.get(f)}.",
                        source=win.source,
                        owner_role=owner(win.source),
                        about={
                            "domain": dom,
                            "field": f,
                            "inForce": win.values.get(f),
                            "other": c.source,
                            "otherValue": c.values.get(f),
                        },
                        key=(f, c.source),
                    )

    ended = {key for key, r in resolved.items() if {c.source for c in r.claims} == {P.ENDED_SOURCE}}
    if ended:
        ended_ids = {rid: t for t, rid in ended}
        for (t, rid), r in sorted(resolved.items()):
            if (t, rid) in ended:
                continue
            for col in E.TYPES[t].cols:
                if col.kind != "ref":
                    continue
                f = E.field_key(col)
                v = r.values.get(f)
                for x in v if isinstance(v, list) else [v] if v else []:
                    if x in ended_ids:
                        src = r.winners[f].source if f in r.winners else None
                        add(
                            "ended-reference",
                            "medium",
                            {"type": ended_ids[x], "id": x},
                            f"{name(ended_ids[x], x)} is no longer in their source, but {t} {rid} still names them "
                            f"as {f}.",
                            source=src,
                            owner_role=owner(src),
                            about={"referrer": {"type": t, "id": rid, "field": f}},
                            key=(t, rid, f),
                        )

    holders: dict[str, list[str]] = {}
    for pid in status_of:
        roles = resolved[("people", pid)].values.get("rolesFilled") or []
        for role in roles if isinstance(roles, list) else [roles]:
            holders.setdefault(role, []).append(pid)
    owner_fields = ("ownerRole", "stewardRole", "businessOwner", "technicalOwner")
    for (t, rid), r in sorted(resolved.items()):
        for f in owner_fields:
            role = r.values.get(f)
            if not isinstance(role, str) or not holders.get(role):
                continue
            if any(status_of[p] != "ended" for p in holders[role]):
                continue
            src = r.winners[f].source if f in r.winners else None
            who = ", ".join(name("people", p) for p in sorted(holders[role]))
            add(
                "owner-has-left",
                "high",
                {"type": t, "id": rid},
                f"{name(t, rid)}'s {f} is {name('roles', role)}, filled only by {who}, whom HR no longer lists.",
                source=src,
                owner_role=owner(src),
                about={"field": f, "role": role, "people": sorted(holders[role])},
                key=(f,),
            )

    vals = {key: r.values for key, r in resolved.items()}

    def ids(v):
        return [] if not v else v if isinstance(v, list) else [v]

    def dec(v):
        try:
            return Decimal(str(v)) if v not in (None, "") else None
        except (InvalidOperation, ValueError):
            return None

    for (t, rid), v in sorted(vals.items()):
        if t != "kpis":
            continue
        cur, thr, better = dec(v.get("currentValue")), dec(v.get("healthThreshold")), v.get("betterWhen")
        if cur is None or thr is None or better not in ("higher", "lower"):
            continue
        if not ((better == "higher" and cur < thr) or (better == "lower" and cur > thr)):
            continue
        for cap in ids(v.get("healthOfCapabilities")):
            cv = vals.get(("capabilities", cap), {})
            src = (
                resolved[("capabilities", cap)].winners.get("ownerRole")
                if ("capabilities", cap) in resolved
                else None
            )
            add(
                "capability-degraded",
                "high",
                {"type": "capabilities", "id": cap},
                f"{cv.get('name') or cap} is degraded: {v.get('name') or rid} is {cur} {v.get('unit') or ''}".rstrip()
                + f", past its health threshold of {thr}. Agents acting on it consult its owner.",
                source=src.source if src else None,
                owner_role=cv.get("ownerRole"),
                about={
                    "kpi": rid,
                    "value": str(cur),
                    "threshold": str(thr),
                    "measuredOn": v.get("measuredOn"),
                },
                key=(rid,),
            )

    def ancestors(oid):
        out, seen = [], {oid}
        while (p := vals.get(("objectives", oid), {}).get("parentObjective")) and p not in seen:
            out.append(p)
            seen.add(p)
            oid = p
        return out

    seen_pairs = set()
    for (t, a), va in sorted(vals.items()):
        if t != "objectives":
            continue
        for b in ids(va.get("conflictsWithObjectives")):
            pair = tuple(sorted((a, b)))
            vb = vals.get(("objectives", b))
            if vb is None or pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            pa, pb = dec(va.get("priority")), dec(vb.get("priority"))
            common = next((x for x in ancestors(a) if x in set(ancestors(b))), None)
            if (pa is not None and pb is not None and pa != pb) or (
                common and vals.get(("objectives", common), {}).get("ownerRole")
            ):
                continue
            for oid, ov in (
                (pair[0], vals[("objectives", pair[0])]),
                (pair[1], vals[("objectives", pair[1])]),
            ):
                add(
                    "objective-conflict-unsettled",
                    "medium",
                    {"type": "objectives", "id": oid},
                    f"{va.get('name') or a} and {vb.get('name') or b} conflict, neither has priority, and no "
                    "parent objective's owner settles it: an agent whose action moves both cannot be told which wins.",
                    owner_role=ov.get("ownerRole"),
                    about={"objectives": list(pair)},
                    key=pair,
                )

    present = {t for t, _ in resolved}
    registered = {c for s, c in cats.items() if s not in (P.STEWARD_SOURCE,)}
    for dname, d in doms.items():
        types = set(d.get("fields") or {}) & present
        if types and not set(d.get("authoritative") or []) & registered:
            add(
                "domain-without-authority",
                "info",
                {"type": sorted(types)[0], "id": f"domain:{dname}"},
                f"No registered source decides {dname} ({d.get('question', '')}); "
                f"register one in {', '.join(d.get('authoritative') or [])} or change the domain.",
                about={"domain": dname},
            )
            out[-1]["subject"] = {"domain": dname}
    return out


def findings(
    store: RDFStore,
    base: str,
    *,
    kind: str | None = None,
    severity: str | None = None,
    owner_role: str | None = None,
    source: str | None = None,
) -> dict:
    found = [dict(f) for f in cached(store, "findings", lambda: _compute(store))]
    reg = P.registry(store)
    for src, rep in sorted(_reported(store).items()):
        for f in rep["findings"]:
            subj = f.get("subject")
            found.append(
                {
                    "id": _fid(f["kind"], subj, src, f.get("about"), f["detail"]),
                    **f,
                    "source": src,
                    "ownerRole": f.get("ownerRole") or (reg.get(src) or {}).get("ownerRole"),
                    "reportedAt": rep.get("reportedAt"),
                }
            )
    if severity and severity not in SEVERITIES:
        raise ValueError(f"Severity is one of {', '.join(SEVERITIES)}.")
    sel = [
        f
        for f in found
        if (not kind or f["kind"] == kind)
        and (not severity or f["severity"] == severity)
        and (not owner_role or f.get("ownerRole") == owner_role)
        and (not source or f.get("source") == source)
    ]
    # Routed: the people in each owner role today.
    people = changesets.shared_entities(store, "people", base)[0] if sel else []
    holders: dict[str, list[dict]] = {}
    for p in people:
        roles = p.get("rolesFilled") or []
        for role in roles if isinstance(roles, list) else [roles]:
            holders.setdefault(role, []).append({"id": p["id"], "name": p.get("name")})
    for f in sel:
        f["routedTo"] = sorted(holders.get(f.get("ownerRole") or "", []), key=lambda x: x["id"])
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    sel.sort(key=lambda f: (rank[f["severity"]], f["kind"], json.dumps(f.get("subject"), sort_keys=True)))
    kinds: dict[str, int] = {}
    for f in sel:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    return {"count": len(sel), "kinds": kinds, "findings": sel}
