"""The proposal step: accountability changes wait for their owner's attestation.

A source that changes who reports to whom, which roles a person fills, who holds an
authority or how far it goes, or whom an agent acts for does not change the effective
model at once (whitepaper: the model is published, not overwritten). The change is a
proposal; the value in force stays until the change is attested, and a dispute keeps it.
New entities are not changes: a new hire's first manager is in force at once.

``P.ACCOUNTABILITY`` lists the fields; ``VEROMESH_PROPOSALS=off`` turns the step off.
Who is asked to attest:

- reports to: the proposed manager (taking the person on) and the manager in force;
- roles filled: the person's manager in force;
- an authority: the holder of its parent authority, or the delegator, else the
  proposed holder's manager;
- an agent's principal: the proposed principal;

and, for every proposal, the people in the proposing source's owner role.
"""

from __future__ import annotations

from . import entities as E
from . import provenance as P
from .rdf_store import RDFStore


def _list(v) -> list:
    if v in (None, "", []):
        return []
    return list(v) if isinstance(v, list) else [v]


def _attest_by(resolved: dict, t: str, rid: str, f: str, proposed, in_force) -> list[str]:
    r = resolved.get((t, rid))
    vals = r.values if r else {}

    def manager(pid):
        p = resolved.get(("people", pid))
        return _list(p.values.get("reportsTo")) if p else []

    def people(x):
        """A holder as people: a person; a role's fillers; an agent's principal."""
        if ("people", x) in resolved:
            return [x]
        if ("roles", x) in resolved:
            return sorted(
                i
                for (t, i), p in resolved.items()
                if t == "people" and x in _list(p.values.get("rolesFilled"))
            )
        if ("agents", x) in resolved:
            return _list(resolved[("agents", x)].values.get("actsFor"))
        return []

    out: list[str] = []
    if t == "people" and f == "reportsTo":
        out = _list(proposed) + _list(in_force)
    elif t == "people":
        out = _list(vals.get("reportsTo"))
    elif t == "authorities":
        parent = resolved.get(("authorities", vals.get("parentAuthority") or ""))
        if parent is not None:
            out = [p for h in _list(parent.values.get("holder")) for p in people(h)]
        out = out or [p for d in _list(vals.get("delegatedBy")) for p in people(d)]
        if not out:
            holder = proposed if f == "holder" else vals.get("holder")
            out = [m for h in _list(holder) for p in people(h) for m in manager(p)]
    elif t == "agents" and f == "actsFor":
        out = _list(proposed)
    elif t == "co-approvals":
        auth = resolved.get(("authorities", vals.get("authority") or ""))
        out = [p for h in _list(auth.values.get("holder")) for p in people(h)] if auth else []
    seen: list[str] = []
    for p in out:
        if p not in seen and ("people", p) in resolved:
            seen.append(p)
    return seen


def pending(store: RDFStore, *, type_: str | None = None, rid: str | None = None) -> list[dict]:
    """Accountability changes waiting for attestation, oldest first."""
    resolved, _ = P._current(store)
    reg = P.registry(store)
    out = []
    for (t, i), r in sorted(resolved.items()):
        if not r.proposals or (type_ and t != type_) or (rid and i != rid):
            continue
        for f, claims in sorted(r.proposals.items()):
            for c in claims:
                in_force = (c.held or {}).get(f)
                proposed = c.values.get(f)
                out.append(
                    {
                        "type": t,
                        "id": i,
                        "name": r.values.get("name") or i,
                        "field": f,
                        "source": c.source,
                        "category": c.category,
                        "proposed": proposed,
                        "inForce": in_force,
                        "effective": r.values.get(f),
                        "proposedAt": c.generated_at,
                        "claim": c.iri[len(P.CLAIM_BASE) :] if c.iri else None,
                        "attestBy": _attest_by(resolved, t, i, f, proposed, in_force),
                        "ownerRole": (reg.get(c.source) or {}).get("ownerRole"),
                    }
                )
    out.sort(key=lambda p: (p["proposedAt"] or "", p["type"], p["id"], p["field"]))
    return out


def settle(
    store: RDFStore,
    base: str,
    type_: str,
    rid: str,
    field: str,
    *,
    verdict: str,
    attester: str,
    role: str,
    source: str | None = None,
    note: str | None = None,
    signed_in: bool = False,
    owner_only: bool = False,
    ontology_dir: str | None = None,
    log=None,
) -> dict:
    """Attest (accept) or dispute (keep the value in force) a proposed change. Accepting
    one that would break the model (a limit above its parent authority's) is refused
    (changesets.Conflict, with the violations), as the load would have been."""
    E.check_type(type_)
    if verdict not in ("attested", "disputed"):
        raise ValueError("Verdict must be 'attested' or 'disputed'.")
    resolved, _ = P._current(store)
    r = resolved.get((type_, rid))
    if r is None or field not in r.proposals:
        from .changesets import _not_found

        raise _not_found(f"No change to {field} of {type_} '{rid}' is waiting for attestation.")
    claims = [c for c in r.proposals[field] if not source or c.source == source]
    if not claims:
        raise ValueError(f"Source '{source}' proposes no change to {field} of {type_} '{rid}'.")
    if len(claims) > 1:
        raise ValueError(
            f"More than one source proposes a change to {field}: give the source "
            f"({', '.join(sorted(c.source for c in claims))})."
        )
    claim = claims[0]
    warnings = P._check_attester(resolved, type_, rid, attester, role, signed_in=signed_in, owner_only=False)
    asked = _attest_by(resolved, type_, rid, field, claim.values.get(field), (claim.held or {}).get(field))
    owner_role = (P.registry(store).get(claim.source) or {}).get("ownerRole")
    if attester not in asked and role != owner_role:
        if owner_only:
            raise P.NotYourRole(
                f"This change is attested by {', '.join(asked) or 'nobody named'}"
                + (f" or the {owner_role} role" if owner_role else "")
                + ", unless you have the VeroMesh steward role."
            )
        warnings.append(
            f"{attester} is not among those asked to attest this change ({', '.join(asked) or 'none'})."
        )
    if verdict == "attested" and ontology_dir:
        _check_accepting(store, base, ontology_dir, claim, field, attester, role)
    made = P.write_attestation(
        store,
        type_=type_,
        rid=rid,
        source=claim.source,
        values={field: claim.values.get(field)},
        verdict=verdict,
        person=attester,
        role=role,
        base=base,
        note=note,
        log=log,
    )
    P.refresh_effective(store, base)
    return {
        "attestation": made.summary() | {"fields": [field]},
        "inForce": claim.values.get(field) if verdict == "attested" else (claim.held or {}).get(field),
        "warnings": warnings,
    }


def _check_accepting(store, base, ontology_dir, claim, field, attester, role) -> None:
    from . import validation
    from .changesets import Conflict, _shacl, _tidy

    claims, atts = P.read_claims(store), P.read_attestations(store)
    yes = P.Attestation(
        iri=P.ATTESTATION_BASE + "pending",
        type=claim.type,
        id=claim.id,
        source=claim.source,
        hashes={field: P.fhash(claim.values.get(field))},
        verdict="attested",
        at=P.now_ts(),
        person=attester,
        role=role,
    )
    before, _ = P.to_graph(P.resolve(claims, atts), base)
    after, _ = P.to_graph(P.resolve(claims, [*atts, yes]), base)
    focus = None
    if not validation.full_validation():
        focus = validation.affected([before, after], validation.changed_nodes(before, after, ontology_dir))
    had = {(sev, str(n), m) for sev, n, m in _shacl(before, ontology_dir, focus)}
    broken = [
        {"id": P.E.local_id(base, n), "message": _tidy(m, base)}
        for sev, n, m in _shacl(after, ontology_dir, focus)
        if sev == "Violation" and (sev, str(n), m) not in had
    ]
    if broken:
        raise Conflict(
            f"Accepting this change to {field} would break the model; it stays a proposal.",
            {"valid": False, "violations": broken},
        )
