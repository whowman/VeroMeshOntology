"""Provenance and governance of facts (docs/provenance.md).

* A **claim** is what one source says about one entity: its field values, when
  the source stated them (observed at) and when they hold (valid from/until).
  Claims are kept as history: a changed or removed entity supersedes its
  claim rather than deleting it.
* An **attestation** is a person, in a role, confirming or disputing fields of
  one source's claim. It pins a hash of each field value it covered, so it
  stops applying as soon as the value changes.
* The **effective model** picks, per entity and field, one source's value:
  disputed values are set aside, then the highest-ranked source category for
  that type and field wins, then the most recently observed claim. It is
  materialized in its own graph and is what validation, reads and agents use.

Governance defaults (rankings, steward-correction policy per field, review
cadence) are below; customers will be able to change them (Phase 2).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from . import clock
from . import entities as E
from .compactgraph import compact_graph
from .modelcache import cached
from .ontology import literal
from .rdf_store import RDFStore, select

VMP = Namespace("https://veromesh.ai/ontology/provenance#")
PROV = Namespace("http://www.w3.org/ns/prov#")
CLAIMS_GRAPH = "https://veromesh.ai/graph/claims"
ATTESTATIONS_GRAPH = "https://veromesh.ai/graph/attestations"
REGISTRY_GRAPH = "https://veromesh.ai/graph/sources"
EFFECTIVE_GRAPH = "https://veromesh.ai/graph/effective"
CLAIM_BASE = "https://veromesh.ai/claim/"
ATTESTATION_BASE = "https://veromesh.ai/attestation/"
SOURCE_BASE = "https://veromesh.ai/source/"
CHANGESET_BASE = "https://veromesh.ai/changeset/"

STEWARD_SOURCE = "steward"
CATEGORIES = ("hr", "iam", "cmdb", "erp", "catalog", "governance", "spreadsheet", "steward", "ended")
#: The system source that keeps a minimal record of what a source dropped while another
#: source still refers to it (issue #67): ranked below every other source, never hr.
ENDED_SOURCE = "ended"
DEFAULT_CATEGORY = "spreadsheet"

# ---------------------------------------------------------------------------- governance defaults

# Source categories by authority, per type (decision of 6 November 2026).
RANKING: dict[str, list[str]] = {
    "people": ["hr", "iam", "spreadsheet"],
    "roles": ["hr", "spreadsheet"],
    "systems": ["cmdb", "catalog", "spreadsheet"],
    "authorities": ["erp", "iam", "spreadsheet"],
    "co-approvals": ["erp", "iam", "spreadsheet"],
    "ai-use-cases": ["governance", "spreadsheet"],
}
DEFAULT_RANKING = ["spreadsheet"]

# Fields where the system of record wins over a steward correction (decision 1).
# Every other field: a steward correction wins, and the disagreement stays a conflict.
SOURCE_WINS: dict[str, set[str]] = {
    "people": {"name", "employeeId", "email", "rolesFilled", "reportsTo"},
    "roles": {"name"},
    "authorities": {
        "holder",
        "actionsCovered",
        "limitAmount",
        "currency",
        "validFrom",
        "validUntil",
        "delegatedBy",
        "parentAuthority",
    },
    "co-approvals": {"authority", "approverRole", "aboveAmount"},
    "systems": {"name", "cmdbId", "lifecycle"},
    # The board decides; a steward can dispute, not overrule.
    "ai-use-cases": {
        "approvedBy",
        "status",
        "coversAgents",
        "allowsActions",
        "approvedOn",
        "reviewBy",
        "conditions",
    },
}

# Re-confirmation cadence in days (decision 4).
CADENCE_DAYS: dict[str, int] = {
    "authorities": 90,
    "co-approvals": 90,
    "people": 180,
    "roles": 180,
    "ai-use-cases": 365,
}
DEFAULT_CADENCE_DAYS = 365
OWNERSHIP_FIELDS = {"ownerRole", "stewardRole", "businessOwner", "technicalOwner"}
OWNERSHIP_CADENCE_DAYS = 180


def governance() -> dict:
    """The governance settings in force, for clients and documentation."""
    return {
        "categories": list(CATEGORIES),
        "ranking": {"default": DEFAULT_RANKING, **RANKING},
        "domains": "GET /api/v1/domains: which category decides each question, per field (these rankings apply only to fields no domain covers)",
        "sourceWins": {t: sorted(f) for t, f in SOURCE_WINS.items()},
        "stewardWins": "every field not listed under sourceWins",
        "cadenceDays": {
            "default": DEFAULT_CADENCE_DAYS,
            "ownershipFields": OWNERSHIP_CADENCE_DAYS,
            **CADENCE_DAYS,
        },
    }


def steward_may_correct(type_: str, key: str) -> bool:
    return key not in SOURCE_WINS.get(type_, set())


def cadence_days(type_: str, key: str) -> int:
    if key in OWNERSHIP_FIELDS:
        return OWNERSHIP_CADENCE_DAYS
    return CADENCE_DAYS.get(type_, DEFAULT_CADENCE_DAYS)


# The domains in force (domains.activate): (type, field) -> its domain's order of categories,
# and the fields combined across sources. Until activated, the shipped defaults.
_FIELD_ORDER: dict[tuple[str, str], list[str]] = {}
_UNION: set[tuple[str, str]] = set()


def use_domains(doms: dict[str, dict]) -> None:
    from . import domains as D

    global _FIELD_ORDER, _UNION
    _FIELD_ORDER = {k: D.order(doms, name) for k, name in D.field_domains(doms).items()}
    _UNION = D.union_fields(doms)


def rank(type_: str, key: str, category: str) -> int:
    """Lower is more authoritative: the field's domain decides (domains.py), else the type's
    ranking. A steward correction ranks first where stewards may correct; what the ended
    source keeps ranks last."""
    if category == "steward":
        return -1 if steward_may_correct(type_, key) else 1000
    if category == "ended":
        return 10_000
    order = _FIELD_ORDER.get((type_, key)) or RANKING.get(type_, DEFAULT_RANKING)
    return order.index(category) if category in order else len(order)


# ---------------------------------------------------------------------------- values and hashes


def empty(value) -> bool:
    return value is None or value == "" or value == []


def norm(value):
    return sorted(value) if isinstance(value, list) else value


def fhash(value) -> str:
    return hashlib.sha256(json.dumps(norm(value), sort_keys=True, default=str).encode()).hexdigest()[:16]


def value_hash(values: dict) -> str:
    return fhash({k: norm(v) for k, v in values.items() if not empty(v)})


def now_ts() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _dt(text: str | None) -> datetime | None:
    if not text:
        return None
    d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _as_of_end(as_of: str) -> datetime:
    """'2026-10-01' means the end of that day; a timestamp means itself."""
    if len(as_of) == 10:
        return clock.end_of_day(date.fromisoformat(as_of))
    return _dt(as_of)  # type: ignore[return-value]


# ---------------------------------------------------------------------------- data


@dataclass(slots=True)
class Claim:
    source: str
    type: str
    id: str
    values: dict
    observed_at: str
    category: str = DEFAULT_CATEGORY
    valid_from: str | None = None
    valid_until: str | None = None
    generated_at: str | None = None
    invalidated_at: str | None = None
    changeset: str | None = None
    iri: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.type, self.id)

    def valid_on(self, day: date) -> bool:
        if self.valid_from and date.fromisoformat(self.valid_from[:10]) > day:
            return False
        if self.valid_until and date.fromisoformat(self.valid_until[:10]) < day:
            return False
        return True

    def summary(self) -> dict:
        return {
            "claim": self.iri[len(CLAIM_BASE) :] if self.iri else None,
            "source": self.source,
            "category": self.category,
            "observedAt": self.observed_at,
            "validFrom": self.valid_from,
            "validUntil": self.valid_until,
            "changeset": self.changeset,
            "recordedAt": self.generated_at,
            "supersededAt": self.invalidated_at,
        }


@dataclass
class Attestation:
    iri: str
    type: str
    id: str
    source: str
    hashes: dict[str, str]
    verdict: str
    at: str
    person: str
    role: str
    expires_at: str | None = None
    note: str | None = None

    def covers(self, key: str, value) -> bool:
        return self.hashes.get(key) == fhash(value)

    def summary(self) -> dict:
        return {
            "attestation": self.iri[len(ATTESTATION_BASE) :],
            "verdict": self.verdict,
            "at": self.at,
            "person": self.person,
            "role": self.role,
            "source": self.source,
            "expiresAt": self.expires_at,
            "note": self.note,
        }


@dataclass(slots=True)
class Resolved:
    """One entity in the effective model."""

    type: str
    id: str
    values: dict
    winners: dict[str, Claim] = field(default_factory=dict)
    candidates: dict[str, list[Claim]] = field(default_factory=dict)
    conflicts: dict[str, list[Claim]] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)


# ---------------------------------------------------------------------------- source registry


def categories(store: RDFStore) -> dict[str, str]:
    return dict(cached(store, "categories", lambda: _categories(store)))


def _categories(store: RDFStore) -> dict[str, str]:
    res = store.query(
        f"""SELECT ?name ?cat WHERE {{ GRAPH <{REGISTRY_GRAPH}> {{
              ?s <{VMP.name}> ?name ; <{VMP.category}> ?cat }} }}"""
    )
    out = {b["name"]["value"]: b["cat"]["value"] for b in res["results"]["bindings"]}
    out[STEWARD_SOURCE] = "steward"
    return out


def registry(store: RDFStore) -> dict[str, dict]:
    res = store.query(
        f"""SELECT ?name ?cat ?owner ?desc ?derived ?access WHERE {{ GRAPH <{REGISTRY_GRAPH}> {{
              ?s <{VMP.name}> ?name ; <{VMP.category}> ?cat .
              OPTIONAL {{ ?s <{VMP.ownerRole}> ?owner }} OPTIONAL {{ ?s <{VMP.description}> ?desc }}
              OPTIONAL {{ ?s <{VMP.derivedFrom}> ?derived }} OPTIONAL {{ ?s <{VMP.grantsAccess}> ?access }} }} }}"""
    )
    return {
        b["name"]["value"]: {
            "category": b["cat"]["value"],
            "ownerRole": b.get("owner", {}).get("value"),
            "description": b.get("desc", {}).get("value"),
            "derivedFrom": json.loads(b["derived"]["value"]) if b.get("derived") else {},
            "grantsAccess": (b["access"]["value"] == "true") if b.get("access") else None,
        }
        for b in res["results"]["bindings"]
    }


def register_source(
    store: RDFStore,
    name: str,
    category: str,
    owner_role: str | None = None,
    description: str | None = None,
    derived_from: dict[str, str] | None = None,
    grants_access: bool | None = None,
) -> dict:
    """Register a source. ``derived_from`` maps a domain to the source this one takes it
    from (an AP workflow whose limits are copied from Workday levels): where the two
    differ the findings report "out of sync" to this source's owner (issue #67)."""
    from .changesets import check_source

    check_source(name)
    if category not in CATEGORIES:
        raise ValueError(f"Category must be one of: {', '.join(CATEGORIES)}.")
    if (name == STEWARD_SOURCE) != (category == "steward"):
        raise ValueError("The 'steward' category belongs to the 'steward' source only.")
    s = SOURCE_BASE + name
    extra = ""
    if owner_role:
        extra += f" ; <{VMP.ownerRole}> {literal(owner_role)}"
    if description:
        extra += f" ; <{VMP.description}> {literal(description)}"
    if derived_from:
        from . import domains

        known = domains.current(store)
        bad = sorted(d for d in derived_from if d not in known)
        if bad:
            raise ValueError(f"derivedFrom: no domain {', '.join(bad)}. Domains: {', '.join(known)}.")
        if name in derived_from.values():
            raise ValueError("derivedFrom: a source cannot be derived from itself.")
        extra += f" ; <{VMP.derivedFrom}> {literal(json.dumps(derived_from, sort_keys=True))}"
    if grants_access is not None:
        extra += f' ; <{VMP.grantsAccess}> "{str(bool(grants_access)).lower()}"^^<http://www.w3.org/2001/XMLSchema#boolean>'
    store.update(
        f"""DELETE {{ GRAPH <{REGISTRY_GRAPH}> {{ <{s}> ?p ?o }} }} WHERE {{ GRAPH <{REGISTRY_GRAPH}> {{ <{s}> ?p ?o }} }} ;
            INSERT DATA {{ GRAPH <{REGISTRY_GRAPH}> {{
              <{s}> a <{VMP.Source}>, <{PROV.SoftwareAgent}> ; <{VMP.name}> {literal(name)} ;
                <{VMP.category}> {literal(category)}{extra} . }} }}"""
    )
    return {
        "source": name,
        "category": category,
        "ownerRole": owner_role,
        "description": description,
        "derivedFrom": derived_from or {},
        "grantsAccess": grants_access,
    }


# ---------------------------------------------------------------------------- claims


def extract(graph: Graph, base: str) -> dict[tuple[str, str], dict]:
    """Every entity in one source graph, as field values (without the ID)."""
    out = {}
    for t in E.TYPES:
        for s in E.subjects_of_type(graph, t):
            ent = E.to_entity(graph, s, t, base)
            rid = ent.pop("id")
            out[(t, rid)] = {k: v for k, v in ent.items() if not empty(v)}
    return out


def _claim_rows(
    store: RDFStore, where: str, iri: str | None = None, changeset: str | None = None
) -> list[Claim]:
    """Claims matching ``where``; or the one claim ``iri``, or the claims ``changeset``
    created (each looked up directly, not by reading every claim)."""
    c = f"<{iri}>" if iri else "?c"
    head = f"(<{iri}> AS ?c)" if iri else "?c"
    by_cs = f"?c <{PROV.wasGeneratedBy}> <{CHANGESET_BASE}{changeset}> ." if changeset and not iri else ""
    rows = select(
        store,
        f"""SELECT {head} ?src ?t ?id ?vals ?obs ?vf ?vu ?gen ?inv ?cs WHERE {{ GRAPH <{CLAIMS_GRAPH}> {{
              {by_cs}
              {c} a <{VMP.Claim}> ; <{VMP.source}> ?src ; <{VMP.entityType}> ?t ; <{VMP.entityId}> ?id ;
                 <{VMP.values}> ?vals ; <{VMP.observedAt}> ?obs ; <{PROV.generatedAtTime}> ?gen .
              OPTIONAL {{ {c} <{VMP.validFrom}> ?vf }} OPTIONAL {{ {c} <{VMP.validUntil}> ?vu }}
              OPTIONAL {{ {c} <{PROV.invalidatedAtTime}> ?inv }} OPTIONAL {{ {c} <{PROV.wasGeneratedBy}> ?cs }}
              {where} }} }}""",
    )

    def v(b, k):
        return b[k]["value"] if k in b else None

    return [
        Claim(
            source=v(b, "src"),
            type=v(b, "t"),
            id=v(b, "id"),
            values=json.loads(v(b, "vals")),
            observed_at=v(b, "obs"),
            valid_from=v(b, "vf"),
            valid_until=v(b, "vu"),
            generated_at=v(b, "gen"),
            invalidated_at=v(b, "inv"),
            changeset=v(b, "cs")[len(CHANGESET_BASE) :] if v(b, "cs") else None,
            iri=v(b, "c"),
        )
        for b in rows
    ]


def read_claims(
    store: RDFStore, *, current: bool = True, source: str | None = None, entity: tuple[str, str] | None = None
) -> list[Claim]:
    where = ""
    if current:
        where += f" FILTER NOT EXISTS {{ ?c <{PROV.invalidatedAtTime}> ?x }}"
    if source:
        where += f" FILTER(?src = {literal(source)})"
    if entity:
        where += f" FILTER(?t = {literal(entity[0])} && ?id = {literal(entity[1])})"
    if current and not source and not entity:
        return list(cached(store, "claims", lambda: _read_claims(store, where)))
    return _read_claims(store, where)


def _read_claims(store: RDFStore, where: str) -> list[Claim]:
    cats = categories(store)
    out = _claim_rows(store, where)
    for c in out:
        c.category = cats.get(c.source, DEFAULT_CATEGORY)
    return out


def read_written(store: RDFStore, changes: dict, cats: dict[str, str] | None = None) -> list[Claim]:
    """The claims ``record_source`` wrote (``changes``), as the store gives them back
    (it may normalise times). ``cats``: the sources' categories, when the caller has them."""
    written = changes["written"]
    back: list[Claim] = []
    if len(written) > 20 and changes.get("changeset"):  # a load: the changeset's claims, in one query
        wanted = set(written)
        back = [c for c in _claim_rows(store, "", changeset=changes["changeset"]) if c.iri in wanted]
        found = {c.iri for c in back}
        written = [iri for iri in written if iri not in found]  # refreshed ones are older claims
    for iri in written:
        back += _claim_rows(store, "", iri=iri)
    cats = categories(store) if cats is None else cats
    for c in back:
        c.category = cats.get(c.source, DEFAULT_CATEGORY)
    return back


def has_claims(store: RDFStore) -> bool:
    return bool(store.query(f"ASK {{ GRAPH <{CLAIMS_GRAPH}> {{ ?c a <{VMP.Claim}> }} }}").get("boolean"))


def record_source(
    store: RDFStore,
    source: str,
    entities: dict[tuple[str, str], dict],
    meta: dict[tuple[str, str], dict],
    *,
    changeset: str | None,
    at: str,
    current: dict[tuple[str, str], Claim] | None = None,
    changes: dict | None = None,
    read_back: bool = True,
    cats: dict[str, str] | None = None,
) -> dict:
    """Bring the source's claims in line with what it now states: supersede changed or
    removed entities, add claims for new values, refresh the observed time of unchanged ones.

    ``current``: the source's current claims for just the entities being changed
    (the caller knows them); only those entities are compared. ``changes``, when
    given, receives what was written: ``created`` (Claims, as they read back),
    ``superseded`` (claim IRIs) and ``refreshed`` ((claim IRI, observed time))."""
    if current is None:
        current = {c.key: c for c in read_claims(store, source=source)}
    created: list[Claim] = []
    g = Graph()
    supersede, refresh = [], []
    added = unchanged = 0
    for key, values in entities.items():
        m = meta.get(key, {})
        old = current.get(key)
        same = (
            old is not None
            and value_hash(old.values) == value_hash(values)
            and old.valid_from == m.get("validFrom", old.valid_from)
            and old.valid_until == m.get("validUntil", old.valid_until)
        )
        if same:
            unchanged += 1
            if m.get("observedAt") and m["observedAt"] != old.observed_at:
                refresh.append((old.iri, m["observedAt"]))
            continue
        if old is not None:
            supersede.append(old.iri)
        added += 1
        c = URIRef(CLAIM_BASE + uuid.uuid4().hex[:16])
        g.add((c, RDF.type, VMP.Claim))
        g.add((c, RDF.type, PROV.Entity))
        g.add((c, VMP.source, Literal(source)))
        g.add((c, VMP.entityType, Literal(key[0])))
        g.add((c, VMP.entityId, Literal(key[1])))
        g.add((c, VMP.values, Literal(json.dumps(values, sort_keys=True, default=str))))
        g.add((c, VMP.valueHash, Literal(value_hash(values))))
        g.add((c, VMP.observedAt, Literal(m.get("observedAt") or at, datatype=XSD.dateTime)))
        g.add((c, PROV.generatedAtTime, Literal(at, datatype=XSD.dateTime)))
        if changeset:
            g.add((c, PROV.wasGeneratedBy, URIRef(CHANGESET_BASE + changeset)))
        if m.get("validFrom"):
            g.add((c, VMP.validFrom, Literal(m["validFrom"][:10], datatype=XSD.date)))
        if m.get("validUntil"):
            g.add((c, VMP.validUntil, Literal(m["validUntil"][:10], datatype=XSD.date)))
        created.append(c)
    supersede += [c.iri for key, c in current.items() if key not in entities]
    for i in range(0, len(supersede), 200):
        values = " ".join(f"<{iri}>" for iri in supersede[i : i + 200])
        store.update(
            f"""INSERT {{ GRAPH <{CLAIMS_GRAPH}> {{ ?c <{PROV.invalidatedAtTime}> "{at}"^^<{XSD.dateTime}> }} }}
                WHERE {{ VALUES ?c {{ {values} }} }}"""
        )
    for iri, observed in refresh:
        store.update(
            f"""DELETE {{ GRAPH <{CLAIMS_GRAPH}> {{ <{iri}> <{VMP.observedAt}> ?o }} }}
                INSERT {{ GRAPH <{CLAIMS_GRAPH}> {{ <{iri}> <{VMP.observedAt}> "{observed}"^^<{XSD.dateTime}> }} }}
                WHERE {{ GRAPH <{CLAIMS_GRAPH}> {{ <{iri}> <{VMP.observedAt}> ?o }} }}"""
        )
    if len(g):
        store.load_turtle(g.serialize(format="nt"), CLAIMS_GRAPH, replace=False)
    if changes is not None:
        written = [str(c) for c in created] + [iri for iri, _ in refresh]
        changes.update(superseded=supersede, written=written, changeset=changeset)
        if read_back:
            changes["claims"] = read_written(store, changes)
        else:  # the store has not been written yet (a deferred batch): as written, for now
            cats = categories(store) if cats is None else cats
            refreshed = dict(refresh)
            mine = []
            for c in created:
                mine.append(
                    Claim(
                        source=source,
                        type=str(g.value(c, VMP.entityType)),
                        id=str(g.value(c, VMP.entityId)),
                        values=json.loads(str(g.value(c, VMP.values))),
                        observed_at=str(g.value(c, VMP.observedAt)),
                        category=cats.get(source, DEFAULT_CATEGORY),
                        valid_from=str(g.value(c, VMP.validFrom)) if g.value(c, VMP.validFrom) else None,
                        valid_until=str(g.value(c, VMP.validUntil)) if g.value(c, VMP.validUntil) else None,
                        generated_at=str(g.value(c, PROV.generatedAtTime)),
                        changeset=changeset,
                        iri=str(c),
                    )
                )
            for old in current.values():
                if old.iri in refreshed:
                    mine.append(replace(old, observed_at=refreshed[old.iri]))
            changes["claims"] = mine
    return {"added": added, "superseded": len(supersede), "unchanged": unchanged}


# ---------------------------------------------------------------------------- attestations


def read_attestations(store: RDFStore, entity: tuple[str, str] | None = None) -> list[Attestation]:
    if entity is None:
        return list(cached(store, "attestations", lambda: _read_attestations(store, None)))
    return _read_attestations(store, entity)


def _read_attestations(store: RDFStore, entity: tuple[str, str] | None) -> list[Attestation]:
    where = f" FILTER(?t = {literal(entity[0])} && ?id = {literal(entity[1])})" if entity else ""
    rows = select(
        store,
        f"""SELECT ?a ?t ?id ?src ?h ?verdict ?at ?person ?role ?exp ?note WHERE {{ GRAPH <{ATTESTATIONS_GRAPH}> {{
              ?a a <{VMP.Attestation}> ; <{VMP.entityType}> ?t ; <{VMP.entityId}> ?id ; <{VMP.source}> ?src ;
                 <{VMP.fieldHashes}> ?h ; <{VMP.verdict}> ?verdict ; <{PROV.endedAtTime}> ?at ;
                 <{VMP.attestedBy}> ?person ; <{VMP.inRole}> ?role .
              OPTIONAL {{ ?a <{VMP.expiresAt}> ?exp }} OPTIONAL {{ ?a <{VMP.note}> ?note }} {where} }} }}
            ORDER BY ?at""",
    )
    return [
        Attestation(
            iri=b["a"]["value"],
            type=b["t"]["value"],
            id=b["id"]["value"],
            source=b["src"]["value"],
            hashes=json.loads(b["h"]["value"]),
            verdict=b["verdict"]["value"],
            at=b["at"]["value"],
            person=b["person"]["value"],
            role=b["role"]["value"],
            expires_at=b.get("exp", {}).get("value"),
            note=b.get("note", {}).get("value"),
        )
        for b in rows
    ]


def write_attestation(
    store: RDFStore,
    *,
    type_: str,
    rid: str,
    source: str,
    values: dict,
    verdict: str,
    person: str,
    role: str,
    base: str,
    note: str | None = None,
    at: str | None = None,
    log=None,
) -> Attestation:
    """Record an attestation: first on the decision log's hash chain (when a log is
    given), then in the attestations graph the model reads."""
    at = at or now_ts()
    days = min(cadence_days(type_, k) for k in values) if values else DEFAULT_CADENCE_DAYS
    expires = (
        (_dt(at) + timedelta(days=days)).replace(microsecond=0).isoformat() if verdict == "attested" else None
    )
    a = Attestation(
        iri=ATTESTATION_BASE + uuid.uuid4().hex[:16],
        type=type_,
        id=rid,
        source=source,
        hashes={k: fhash(v) for k, v in values.items()},
        verdict=verdict,
        at=at,
        person=person,
        role=role,
        expires_at=expires,
        note=note,
    )
    if log is not None:
        log.append(attestation_entry(a))
    g = Graph()
    s = URIRef(a.iri)
    g.add((s, RDF.type, VMP.Attestation))
    g.add((s, RDF.type, PROV.Activity))
    g.add((s, VMP.entityType, Literal(type_)))
    g.add((s, VMP.entityId, Literal(rid)))
    g.add((s, VMP.entity, E.iri_for(base, rid)))
    g.add((s, VMP.source, Literal(source)))
    g.add((s, VMP.fields, Literal(json.dumps(sorted(values)))))
    g.add((s, VMP.fieldHashes, Literal(json.dumps(a.hashes, sort_keys=True))))
    g.add((s, VMP.verdict, Literal(verdict)))
    g.add((s, PROV.endedAtTime, Literal(at, datatype=XSD.dateTime)))
    g.add((s, VMP.attestedBy, Literal(person)))
    g.add((s, VMP.inRole, Literal(role)))
    if expires:
        g.add((s, VMP.expiresAt, Literal(expires, datatype=XSD.dateTime)))
    if note:
        g.add((s, VMP.note, Literal(note)))
    store.load_turtle(g.serialize(format="nt"), ATTESTATIONS_GRAPH, replace=False)
    return a


def attestation_entry(a: Attestation) -> dict:
    """The decision log entry for an attestation: everything the model relies on."""
    return {
        "kind": "attestation",
        "outcome": a.verdict,
        "subject": a.person,
        "attestation": a.iri,
        "entityType": a.type,
        "entityId": a.id,
        "source": a.source,
        "role": a.role,
        "fieldHashes": dict(sorted(a.hashes.items())),
        "attestedAt": a.at,
        "expiresAt": a.expires_at,
        "note": a.note,
    }


def _same_instant(x: str | None, y: str | None) -> bool:
    if x is None or y is None:
        return x == y
    return _dt(x) == _dt(y)


def verify_attestations(store: RDFStore, log) -> dict:
    """Check the attestations the model relies on against the decision log's chain:
    the chain itself, then each attestation in the graph against its entry. Reports
    attestations whose recorded values differ (altered), that are in the graph with
    no entry (unrecorded: written around the service), and entries whose attestation
    is no longer in the graph (missing)."""
    chain = log.verify()
    entries: dict[str, dict] = {}
    after = 0
    while True:
        page = log.find(kind="attestation", after_seq=after, limit=1000)
        if not page:
            break
        for e in page:
            entries[e["attestation"]] = e
        after = page[-1]["seq"]
    altered, unrecorded = [], []
    seen = set()
    for a in _read_attestations(store, None):
        e = entries.get(a.iri)
        if e is None:
            unrecorded.append(a.iri[len(ATTESTATION_BASE) :])
            continue
        seen.add(a.iri)
        differs = [
            name
            for name, same in (
                ("verdict", e["outcome"] == a.verdict),
                ("person", e["subject"] == a.person),
                ("role", e["role"] == a.role),
                ("entity", (e["entityType"], e["entityId"]) == (a.type, a.id)),
                ("source", e["source"] == a.source),
                ("fields", e["fieldHashes"] == a.hashes),
                ("at", _same_instant(e["attestedAt"], a.at)),
                ("expiresAt", _same_instant(e.get("expiresAt"), a.expires_at)),
            )
            if not same
        ]
        if differs:
            altered.append({"attestation": a.iri[len(ATTESTATION_BASE) :], "differs": differs})
    missing = [iri[len(ATTESTATION_BASE) :] for iri in entries if iri not in seen]
    return {
        "ok": chain.ok and not (altered or unrecorded or missing),
        "chain": chain.as_dict(),
        "attestations": len(seen) + len(unrecorded),
        "recorded": len(entries),
        "altered": altered,
        "unrecorded": unrecorded,
        "missing": missing,
    }


def disputed(atts: list[Attestation], claim: Claim, key: str) -> bool:
    """Is this claim's current value for ``key`` under dispute (latest verdict on it)?"""
    latest = None
    for a in atts:
        if (
            a.source == claim.source
            and a.type == claim.type
            and a.id == claim.id
            and a.covers(key, claim.values.get(key))
        ):
            latest = a
    return latest is not None and latest.verdict == "disputed"


def field_status(
    atts: list[Attestation], claim: Claim, key: str, at: datetime | None = None
) -> tuple[str, Attestation | None]:
    """unconfirmed | attested | disputed | expired, and the attestation that decided it."""
    at = at or datetime.now(UTC)
    latest = None
    for a in atts:
        if _dt(a.at) > at:
            continue
        if (
            a.source == claim.source
            and a.type == claim.type
            and a.id == claim.id
            and a.covers(key, claim.values.get(key))
        ):
            latest = a
    if latest is None:
        return "unconfirmed", None
    if latest.verdict == "disputed":
        return "disputed", latest
    if latest.expires_at and _dt(latest.expires_at) < at:
        return "expired", latest
    return "attested", latest


# ---------------------------------------------------------------------------- the effective model


def resolve(
    claims: list[Claim], atts: list[Attestation], day: date | None = None, *, ignore_validity: bool = False
) -> dict[tuple[str, str], Resolved]:
    """Per entity and field, the value VeroMesh believes, by the precedence rules.
    Claims outside their valid period on ``day`` (default today) do not count."""
    day = day or clock.today()
    by_entity: dict[tuple[str, str], list[Claim]] = {}
    for c in claims:
        if ignore_validity or c.valid_on(day):
            by_entity.setdefault(c.key, []).append(c)
    by_type_atts: dict[tuple[str, str], list[Attestation]] = {}
    for a in atts:
        by_type_atts.setdefault((a.type, a.id), []).append(a)
    out: dict[tuple[str, str], Resolved] = {}
    for key, cs in by_entity.items():
        type_, rid = key
        r = Resolved(type=type_, id=rid, values={}, claims=cs)
        ea = by_type_atts.get(key, [])
        fields = sorted({k for c in cs for k, v in c.values.items() if not empty(v)})
        for f in fields:
            cands = [c for c in cs if not empty(c.values.get(f))]
            if len(cands) == len(cs):
                cands = cs  # every claim states it (the usual case): share the list, one fewer object to keep
            live = [c for c in cands if not disputed(ea, c, f)] or list(cands)
            live.sort(key=lambda c: (rank(type_, f, c.category), -_dt(c.observed_at).timestamp(), c.source))
            win = live[0]
            r.winners[f] = win
            r.candidates[f] = cands
            if (type_, f) in _UNION:
                # Combined across sources (a person's job roles from HR and approval roles from
                # identity groups or the workbook); what the ended source keeps does not count.
                vals: list = []
                for c in live:
                    if c.category == "ended":
                        continue
                    for v in c.values[f] if isinstance(c.values[f], list) else [c.values[f]]:
                        if v not in vals:
                            vals.append(v)
                r.values[f] = sorted(vals, key=str) if vals else win.values[f]
                continue
            r.values[f] = win.values[f]
            if len({fhash(c.values[f]) for c in cands}) > 1:
                r.conflicts[f] = cands
        out[key] = r
    return out


def to_graph(resolved: dict[tuple[str, str], Resolved], base: str) -> tuple[Graph, list[dict]]:
    """The effective model as triples, plus references that point nowhere."""
    index = {rid: t for (t, rid) in resolved}
    g = Graph()
    errors = []
    for (t, rid), r in sorted(resolved.items()):
        conv = E.to_triples(t, {"id": rid, **r.values}, base, index.get, partial=True)
        for s, p, o in conv.triples:
            g.add((s, p, o))
        for e in conv.errors:
            errors.append(e.as_dict())
    return g, errors


def store_effective(store: RDFStore, graph: Graph) -> None:
    store.load_turtle(graph.serialize(format="nt"), EFFECTIVE_GRAPH)


def effective_graph(store: RDFStore) -> Graph:
    """The effective model (read-only: the same Graph is shared until the model changes)."""
    return cached(store, "effective", lambda: _effective_graph(store))


def _effective_graph(store: RDFStore) -> Graph:
    from .changesets import _term

    rows = select(store, f"SELECT ?s ?p ?o WHERE {{ GRAPH <{EFFECTIVE_GRAPH}> {{ ?s ?p ?o }} }}")
    g = compact_graph()  # cached until the model changes
    for b in rows:
        g.add((_term(b["s"]), _term(b["p"]), _term(b["o"])))
    return g


def migrate(store: RDFStore, base: str) -> list[str]:
    """Create claims for sources that have none yet (data loaded before provenance existed),
    observed at the source's last update and unconfirmed."""
    from .changesets import list_sources, read_sources

    with_claims = {c.source for c in read_claims(store)}
    todo = [s for s in list_sources(store) if s["updatedAt"] and s["source"] not in with_claims]
    if not todo:
        return []
    graphs = read_sources(store)
    migrated = []
    for s in todo:
        ents = extract(graphs.get(s["graph"], Graph()), base)
        if ents:
            record_source(store, s["source"], ents, {}, changeset=s.get("changeset"), at=s["updatedAt"])
            migrated.append(s["source"])
    return migrated


def rebuild(store: RDFStore, base: str) -> dict:
    """Bring claims up to date with every source, then recompute and store the effective model."""
    from . import domains

    domains.activate(store)
    migrated = migrate(store, base)
    resolved = resolve(read_claims(store), read_attestations(store))
    g, _ = to_graph(resolved, base)
    store_effective(store, g)
    return {"migrated": migrated, "entities": len(resolved), "facts": len(g)}


def ensure_effective(store: RDFStore, base: str) -> None:
    """Build the effective model on first use after an upgrade."""
    exists = store.query(f"ASK {{ GRAPH <{EFFECTIVE_GRAPH}> {{ ?s ?p ?o }} }}").get("boolean")
    if not exists:
        any_source = store.query(
            'ASK { GRAPH ?g { ?s ?p ?o } FILTER(STRSTARTS(STR(?g), "https://veromesh.ai/graph/source/")) }'
        ).get("boolean")
        if any_source:
            rebuild(store, base)


# ---------------------------------------------------------------------------- views


def explain(store: RDFStore, type_: str, rid: str) -> dict:
    """Why we believe what we believe about one entity."""
    E.check_type(type_)
    claims = read_claims(store, entity=(type_, rid))
    if not claims:
        from .changesets import _not_found

        raise _not_found(f"No {type_} with ID '{rid}'.")
    atts = read_attestations(store, entity=(type_, rid))
    r = resolve(claims, atts).get((type_, rid))
    history = read_claims(store, current=False, entity=(type_, rid))
    fields = {}
    for f, win in (r.winners if r else {}).items():
        status, att = field_status(atts, win, f)
        fields[f] = {
            "value": win.values[f],
            "source": win.source,
            "status": status,
            "attestation": att.summary() if att else None,
            "stewardMayCorrect": steward_may_correct(type_, f),
            "conflict": f in r.conflicts,
            "claims": [
                {**c.summary(), "value": c.values[f], "status": field_status(atts, c, f)[0], "wins": c is win}
                for c in r.candidates[f]
            ],
        }
    return {
        "type": type_,
        "id": rid,
        "values": r.values if r else {},
        "fields": fields,
        "claims": [c.summary() for c in claims],
        "history": [
            c.summary() | {"values": c.values} for c in sorted(history, key=lambda c: c.generated_at or "")
        ],
    }


def as_of(store: RDFStore, type_: str, rid: str, when: str) -> dict:
    """The entity as VeroMesh believed it at a point in time (a date means the end of that day)."""
    E.check_type(type_)
    t = _as_of_end(when)
    claims = [
        c
        for c in read_claims(store, current=False, entity=(type_, rid))
        if _dt(c.generated_at) <= t and (c.invalidated_at is None or _dt(c.invalidated_at) > t)
    ]
    atts = [a for a in read_attestations(store, entity=(type_, rid)) if _dt(a.at) <= t]
    r = resolve(claims, atts, day=t.date()).get((type_, rid))
    if r is None:
        from .changesets import _not_found

        raise _not_found(f"No {type_} with ID '{rid}' as of {when}.")
    return {"id": rid, **r.values, "asOf": when, "sources": sorted({c.source for c in r.claims})}


def conflicts(store: RDFStore, type_: str | None = None) -> list[dict]:
    if type_:
        E.check_type(type_)
    resolved = resolve(read_claims(store), read_attestations(store))
    out = []
    for (t, rid), r in sorted(resolved.items()):
        if type_ and t != type_:
            continue
        for f, cands in sorted(r.conflicts.items()):
            out.append(
                {
                    "type": t,
                    "id": rid,
                    "field": f,
                    "value": r.values[f],
                    "winner": r.winners[f].source,
                    "claims": [
                        {
                            "source": c.source,
                            "category": c.category,
                            "value": c.values[f],
                            "observedAt": c.observed_at,
                        }
                        for c in cands
                    ],
                }
            )
    return out


def review_queue(
    store: RDFStore,
    *,
    status: str = "unconfirmed",
    type_: str | None = None,
    owner_role: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Entities with fields in the given status, for the steward review screen."""
    if status not in ("unconfirmed", "attested", "disputed", "expired"):
        raise ValueError("Status must be one of: unconfirmed, attested, disputed, expired.")
    if type_:
        E.check_type(type_)
    atts = read_attestations(store)
    by_entity: dict[tuple[str, str], list[Attestation]] = {}
    for a in atts:
        by_entity.setdefault((a.type, a.id), []).append(a)
    out = []
    for (t, rid), r in sorted(resolve(read_claims(store), atts).items()):
        if type_ and t != type_:
            continue
        owner = next((r.values[k] for k in ("ownerRole", "businessOwner") if k in r.values), None)
        if owner_role and owner != owner_role:
            continue
        ea = by_entity.get((t, rid), [])
        hits = {f: field_status(ea, w, f)[0] for f, w in r.winners.items()}
        hits = {f: s for f, s in hits.items() if s == status}
        if hits:
            out.append(
                {"type": t, "id": rid, "name": r.values.get("name"), "ownerRole": owner, "fields": hits}
            )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------- confirming and correcting


def _current(store: RDFStore) -> tuple[dict[tuple[str, str], Resolved], list[Attestation]]:
    """What VeroMesh believes today (shared and read-only until the model or the day changes)."""

    day = clock.today()

    def compute():
        kept = _from_planner(store, day)
        if kept is not None:
            return kept
        atts = read_attestations(store)
        return resolve(read_claims(store), atts), atts

    return cached(store, ("current", day), compute)


# How long a read waits for a change in progress before reading the store itself.
PLANNER_WAIT = 5.0


def _from_planner(store: RDFStore, day: date):
    """Today's model as the change planner already holds it at this version (the same
    entities, shared rather than resolved a second time), or None. See ``_planner``."""
    with _planner(store) as st:
        if st is not None and st.day == day:
            return dict(st.today.resolved), [a for atts in st.atts.values() for a in atts]
    return None


def from_planner_effective(store: RDFStore, base: str, read):
    """``read(graph)`` on the effective model as the change planner holds it in memory at
    this version (no read from the store), or None when it does not hold it. ``read``
    must not keep the graph: the planner changes it at the next commit."""
    with _planner(store) as st:
        if st is not None and st.base == base and st.stored_effective_ok and st.day == clock.today():
            return read(st.today.graph)
    return None


@contextmanager
def _planner(store: RDFStore):
    """The change planner's state, or None.

    A commit changes the planner's state before the version moves on, under the
    planner's lock. While a change is in progress this waits for it (up to
    PLANNER_WAIT seconds: far less than reading the whole model from the store, which
    on a large organization holds every other thread up for seconds). The state is
    the one for the version the caller reads at, or else the one the change just
    made: the caller then gets the model as of that change, as reading the store
    would have given it."""
    from . import incremental
    from .modelcache import VersionedStore

    if not isinstance(store, VersionedStore) or not incremental._LOCK.acquire(timeout=PLANNER_WAIT):
        yield None
        return
    try:
        found = None
        for version in dict.fromkeys((store.version(), store._newest)):
            for key, st in store.entries_at(version).items():
                if isinstance(key, tuple) and key[0] == "incremental" and st is not None:
                    found = st
                    break
            if found is not None:
                break
        if found is None:  # another copy changed the model: follow it if it left a record
            found = incremental.follow(store)
        yield found
    finally:
        incremental._LOCK.release()


def _current_index(
    store: RDFStore,
) -> tuple[
    dict[tuple[str, str], Resolved], dict[tuple[str, str], list[Attestation]], dict[str, list[Resolved]]
]:
    """What VeroMesh believes today, with its attestations by entity and the people filling
    each role (in ID order), built once per model version instead of on every check."""

    def compute():
        resolved, atts = _current(store)
        by_entity: dict[tuple[str, str], list[Attestation]] = {}
        for a in atts:
            by_entity.setdefault((a.type, a.id), []).append(a)
        fillers: dict[str, list[tuple[str, Resolved]]] = {}
        for (t, rid), r in resolved.items():
            if t == "people":
                for role in r.values.get("rolesFilled", []) or []:
                    fillers.setdefault(role, []).append((rid, r))
        return (
            resolved,
            by_entity,
            {k: [r for _, r in sorted(v, key=lambda x: x[0])] for k, v in fillers.items()},
        )

    return cached(store, ("current-index", clock.today()), compute)


def matching(store: RDFStore, type_: str, field: str, value: str, *, ignore_case: bool = False) -> list[str]:
    """IDs of the entities of ``type_`` whose ``field`` is ``value``, as VeroMesh believes it now."""

    def norm(v: str) -> str:
        return v.strip().casefold() if ignore_case else v.strip()

    def build() -> dict[str, list[str]]:
        resolved, _ = _current(store)
        index: dict[str, list[str]] = {}
        for (t, rid), r in resolved.items():
            v = r.values.get(field)
            if t == type_ and isinstance(v, str):
                index.setdefault(norm(v), []).append(rid)
        return {k: sorted(ids) for k, ids in index.items()}

    # One index per field, kept until the model (or the day) changes.
    index = cached(store, ("matching", type_, field, ignore_case, clock.today()), build)
    return list(index.get(norm(value), []))


def person_matching(store: RDFStore, field: str, value: str) -> str | None:
    """The ID of the one person whose ``field`` (email or employeeId) is ``value``, as VeroMesh
    believes it now; None when no person, or more than one, matches."""
    hits = matching(store, "people", field, value, ignore_case=field == "email")
    return hits[0] if len(hits) == 1 else None


class NotYourRole(PermissionError):
    pass


def _check_attester(
    resolved: dict,
    type_: str,
    rid: str,
    attester: str,
    role: str,
    *,
    signed_in: bool = False,
    owner_only: bool = False,
) -> list[str]:
    """Check the attester names a real person and role, and warn when they are not the
    owner or steward. With sign-in, the attester is the signed-in person, and acting in a
    role they do not fill is refused rather than warned about. ``owner_only`` (an owner
    attesting without the VeroMesh steward role): the role must be one of the entity's
    owner or steward roles, or the attestation is refused."""
    if ("people", attester) not in resolved:
        raise ValueError(f"No person '{attester}' in the model.")
    if ("roles", role) not in resolved:
        raise ValueError(f"No role '{role}' in the model.")
    warnings = []
    if role not in resolved[("people", attester)].values.get("rolesFilled", []):
        if signed_in:
            raise NotYourRole(f"You ({attester}) do not fill role {role} in the model.")
        warnings.append(f"{attester} does not fill role {role}.")
    target = resolved.get((type_, rid))
    owners = {v for k in OWNERSHIP_FIELDS for v in [target.values.get(k)] if v} if target else set()
    if owner_only and role not in owners:
        named = f" ({', '.join(sorted(owners))})" if owners else " (it names none)"
        raise NotYourRole(
            f"Only the owner or steward roles of {type_} '{rid}'{named} may attest it, "
            "unless you have the VeroMesh steward role."
        )
    if owners and role not in owners:
        warnings.append(
            f"Role {role} is not an owner or steward of {type_} '{rid}' ({', '.join(sorted(owners))})."
        )
    return warnings


def attest(
    store: RDFStore,
    base: str,
    type_: str,
    rid: str,
    *,
    verdict: str,
    attester: str,
    role: str,
    fields: list[str] | None = None,
    source: str | None = None,
    note: str | None = None,
    signed_in: bool = False,
    owner_only: bool = False,
    log=None,
) -> dict:
    """Confirm or dispute fields of an entity: by default the values VeroMesh currently
    believes (each field's winning source); with ``source``, that source's values."""
    E.check_type(type_)
    if verdict not in ("attested", "disputed"):
        raise ValueError("Verdict must be 'attested' or 'disputed'.")
    resolved, _ = _current(store)
    r = resolved.get((type_, rid))
    if r is None:
        from .changesets import _not_found

        raise _not_found(f"No {type_} with ID '{rid}'.")
    warnings = _check_attester(
        resolved, type_, rid, attester, role, signed_in=signed_in, owner_only=owner_only
    )
    wanted = fields or sorted(r.winners)
    by_source: dict[str, dict] = {}
    for f in wanted:
        if source:
            claim = next((c for c in r.candidates.get(f, []) if c.source == source), None)
            if claim is None:
                raise ValueError(f"Source '{source}' states no value for {type_} '{rid}' field '{f}'.")
        else:
            claim = r.winners.get(f)
            if claim is None:
                raise ValueError(f"{type_} '{rid}' has no value for field '{f}'.")
        by_source.setdefault(claim.source, {})[f] = claim.values[f]
    made = [
        write_attestation(
            store,
            type_=type_,
            rid=rid,
            source=src,
            values=vals,
            verdict=verdict,
            person=attester,
            role=role,
            base=base,
            note=note,
            log=log,
        )
        for src, vals in sorted(by_source.items())
    ]
    if verdict == "disputed":  # a disputed value stops winning
        refresh_effective(store, base)
    return {"attestations": [a.summary() | {"fields": sorted(a.hashes)} for a in made], "warnings": warnings}


def refresh_effective(store: RDFStore, base: str) -> None:
    resolved, _ = _current(store)
    g, _ = to_graph(resolved, base)
    store_effective(store, g)


def correct(
    store: RDFStore,
    base: str,
    ontology_dir: str,
    type_: str,
    rid: str,
    *,
    values: dict,
    attester: str,
    role: str,
    note: str | None = None,
    signed_in: bool = False,
    log=None,
) -> dict:
    """A steward correction: recorded as the 'steward' source's claim, validated like any
    change, and attested by the steward in the same step."""
    from .changesets import Conflict, apply, read_sources, source_graph

    E.check_type(type_)
    if not values:
        raise ValueError("Nothing to correct: give the field values.")
    unknown = [k for k in values if k not in E.FIELDS[type_] or k == "id"]
    if unknown:
        raise ValueError(f"Unknown field(s) for {type_}: {', '.join(unknown)}.")
    governed = [k for k in values if not steward_may_correct(type_, k)]
    if governed:
        raise ValueError(
            f"{', '.join(governed)} on {type_} is governed by its source system; dispute the value instead "
            "(POST .../attestations with verdict 'disputed')."
        )
    resolved, _ = _current(store)
    if (type_, rid) not in resolved:
        from .changesets import _not_found

        raise _not_found(f"No {type_} with ID '{rid}'.")
    warnings = _check_attester(resolved, type_, rid, attester, role, signed_in=signed_in)
    existing = extract(read_sources(store).get(source_graph(STEWARD_SOURCE), Graph()), base).get(
        (type_, rid), {}
    )
    merged = {**existing, **values}
    try:
        report = apply(
            store,
            source=STEWARD_SOURCE,
            items=[{"type": type_, "op": "upsert", "entity": {"id": rid, **merged}}],
            base=base,
            ontology_dir=ontology_dir,
        )
    except Conflict:
        raise
    resolved, _ = _current(store)
    claim = next(c for c in resolved[(type_, rid)].claims if c.source == STEWARD_SOURCE)
    a = write_attestation(
        store,
        type_=type_,
        rid=rid,
        source=STEWARD_SOURCE,
        values={k: claim.values[k] for k in values if k in claim.values},
        verdict="attested",
        person=attester,
        role=role,
        base=base,
        note=note,
        log=log,
    )
    return {"report": report, "attestation": a.summary() | {"fields": sorted(a.hashes)}, "warnings": warnings}


def authority_basis(store: RDFStore, rid: str) -> dict:
    """Decision 3: an agent's authority check may rely only on attested, current facts.
    Checks the delegated authority, the role holding it, the people filling that role,
    and the ownership of every KPI its actions affect."""
    resolved, by_entity, fillers_of = _current_index(store)
    auth = resolved.get(("authorities", rid))
    if auth is None:
        from .changesets import _not_found

        raise _not_found(f"No authorities with ID '{rid}'.")
    checks = []

    def check(r: Resolved, fields: list[str], why: str):
        for f in fields:
            if f not in r.winners:
                continue
            status, _ = field_status(by_entity.get((r.type, r.id), []), r.winners[f], f)
            checks.append(
                {
                    "type": r.type,
                    "id": r.id,
                    "field": f,
                    "status": status,
                    "ok": status == "attested",
                    "why": why,
                }
            )

    check(auth, sorted(auth.winners), "the delegated authority")
    holder = auth.values.get("holder")
    if holder and ("roles", holder) in resolved:
        role = resolved[("roles", holder)]
        check(role, sorted(role.winners), "the role holding the authority")
        fillers = fillers_of.get(holder, [])
        if not fillers:
            checks.append(
                {
                    "type": "roles",
                    "id": holder,
                    "field": "rolesFilled",
                    "status": "missing",
                    "ok": False,
                    "why": "nobody fills the role",
                }
            )
        for p in fillers:
            check(p, ["rolesFilled"], "a person filling the role")
    elif holder and ("agents", holder) in resolved:
        agent = resolved[("agents", holder)]
        check(agent, ["actsFor", "performsActions"], "the agent holding the authority")
        person = agent.values.get("actsFor")
        if person and ("people", person) in resolved:
            check(resolved[("people", person)], ["rolesFilled"], "the person the agent acts for")
    elif holder and ("people", holder) in resolved:
        check(resolved[("people", holder)], ["rolesFilled", "name"], "the person holding the authority")
    kpis = sorted(
        {
            k
            for a in auth.values.get("actionsCovered", [])
            for k in resolved.get(("actions", a), Resolved("actions", a, {})).values.get("affects", [])
        }
    )
    for k in kpis:
        if ("kpis", k) in resolved:
            check(resolved[("kpis", k)], ["ownerRole"], "ownership of an affected KPI")
    failing = [c for c in checks if not c["ok"]]
    return {
        "authority": rid,
        "confirmed": not failing,
        "checks": checks,
        "reasons": [f"{c['type']} '{c['id']}' {c['field']}: {c['status']} ({c['why']})" for c in failing],
    }


def _default_domains() -> None:
    from . import domains

    use_domains(domains.DEFAULTS)


_default_domains()
