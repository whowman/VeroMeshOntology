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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from . import entities as E
from .ontology import literal
from .rdf_store import RDFStore

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
CATEGORIES = ("hr", "iam", "cmdb", "erp", "catalog", "spreadsheet", "steward")
DEFAULT_CATEGORY = "spreadsheet"

# ---------------------------------------------------------------------------- governance defaults

# Source categories by authority, per type (decision of 6 November 2026).
RANKING: dict[str, list[str]] = {
    "people": ["hr", "iam", "spreadsheet"],
    "roles": ["hr", "spreadsheet"],
    "systems": ["cmdb", "catalog", "spreadsheet"],
    "authorities": ["erp", "iam", "spreadsheet"],
    "co-approvals": ["erp", "iam", "spreadsheet"],
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
}

# Re-confirmation cadence in days (decision 4).
CADENCE_DAYS: dict[str, int] = {"authorities": 90, "co-approvals": 90, "people": 180, "roles": 180}
DEFAULT_CADENCE_DAYS = 365
OWNERSHIP_FIELDS = {"ownerRole", "stewardRole", "businessOwner", "technicalOwner"}
OWNERSHIP_CADENCE_DAYS = 180


def governance() -> dict:
    """The governance settings in force, for clients and documentation."""
    return {
        "categories": list(CATEGORIES),
        "ranking": {"default": DEFAULT_RANKING, **RANKING},
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


def rank(type_: str, key: str, category: str) -> int:
    """Lower is more authoritative."""
    order = RANKING.get(type_, DEFAULT_RANKING)
    if category == "steward":
        return -1 if steward_may_correct(type_, key) else 1000
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
        return datetime.fromisoformat(as_of).replace(tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)
    return _dt(as_of)  # type: ignore[return-value]


# ---------------------------------------------------------------------------- data


@dataclass
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


@dataclass
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
    res = store.query(
        f"""SELECT ?name ?cat WHERE {{ GRAPH <{REGISTRY_GRAPH}> {{
              ?s <{VMP.name}> ?name ; <{VMP.category}> ?cat }} }}"""
    )
    out = {b["name"]["value"]: b["cat"]["value"] for b in res["results"]["bindings"]}
    out[STEWARD_SOURCE] = "steward"
    return out


def registry(store: RDFStore) -> dict[str, dict]:
    res = store.query(
        f"""SELECT ?name ?cat ?owner ?desc WHERE {{ GRAPH <{REGISTRY_GRAPH}> {{
              ?s <{VMP.name}> ?name ; <{VMP.category}> ?cat .
              OPTIONAL {{ ?s <{VMP.ownerRole}> ?owner }} OPTIONAL {{ ?s <{VMP.description}> ?desc }} }} }}"""
    )
    return {
        b["name"]["value"]: {
            "category": b["cat"]["value"],
            "ownerRole": b.get("owner", {}).get("value"),
            "description": b.get("desc", {}).get("value"),
        }
        for b in res["results"]["bindings"]
    }


def register_source(
    store: RDFStore, name: str, category: str, owner_role: str | None = None, description: str | None = None
) -> dict:
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
    store.update(
        f"""DELETE {{ GRAPH <{REGISTRY_GRAPH}> {{ <{s}> ?p ?o }} }} WHERE {{ GRAPH <{REGISTRY_GRAPH}> {{ <{s}> ?p ?o }} }} ;
            INSERT DATA {{ GRAPH <{REGISTRY_GRAPH}> {{
              <{s}> a <{VMP.Source}>, <{PROV.SoftwareAgent}> ; <{VMP.name}> {literal(name)} ;
                <{VMP.category}> {literal(category)}{extra} . }} }}"""
    )
    return {"source": name, "category": category, "ownerRole": owner_role, "description": description}


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


def _claim_rows(store: RDFStore, where: str) -> list[Claim]:
    res = store.query(
        f"""SELECT ?c ?src ?t ?id ?vals ?obs ?vf ?vu ?gen ?inv ?cs WHERE {{ GRAPH <{CLAIMS_GRAPH}> {{
              ?c a <{VMP.Claim}> ; <{VMP.source}> ?src ; <{VMP.entityType}> ?t ; <{VMP.entityId}> ?id ;
                 <{VMP.values}> ?vals ; <{VMP.observedAt}> ?obs ; <{PROV.generatedAtTime}> ?gen .
              OPTIONAL {{ ?c <{VMP.validFrom}> ?vf }} OPTIONAL {{ ?c <{VMP.validUntil}> ?vu }}
              OPTIONAL {{ ?c <{PROV.invalidatedAtTime}> ?inv }} OPTIONAL {{ ?c <{PROV.wasGeneratedBy}> ?cs }}
              {where} }} }}"""
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
        for b in res["results"]["bindings"]
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
    cats = categories(store)
    out = _claim_rows(store, where)
    for c in out:
        c.category = cats.get(c.source, DEFAULT_CATEGORY)
    return out


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
) -> dict:
    """Bring the source's claims in line with what it now states: supersede changed or
    removed entities, add claims for new values, refresh the observed time of unchanged ones."""
    current = {c.key: c for c in read_claims(store, source=source)}
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
        store.load_turtle(g.serialize(format="turtle"), CLAIMS_GRAPH, replace=False)
    return {"added": added, "superseded": len(supersede), "unchanged": unchanged}


# ---------------------------------------------------------------------------- attestations


def read_attestations(store: RDFStore, entity: tuple[str, str] | None = None) -> list[Attestation]:
    where = f" FILTER(?t = {literal(entity[0])} && ?id = {literal(entity[1])})" if entity else ""
    res = store.query(
        f"""SELECT ?a ?t ?id ?src ?h ?verdict ?at ?person ?role ?exp ?note WHERE {{ GRAPH <{ATTESTATIONS_GRAPH}> {{
              ?a a <{VMP.Attestation}> ; <{VMP.entityType}> ?t ; <{VMP.entityId}> ?id ; <{VMP.source}> ?src ;
                 <{VMP.fieldHashes}> ?h ; <{VMP.verdict}> ?verdict ; <{PROV.endedAtTime}> ?at ;
                 <{VMP.attestedBy}> ?person ; <{VMP.inRole}> ?role .
              OPTIONAL {{ ?a <{VMP.expiresAt}> ?exp }} OPTIONAL {{ ?a <{VMP.note}> ?note }} {where} }} }}
            ORDER BY ?at"""
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
        for b in res["results"]["bindings"]
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
) -> Attestation:
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
    store.load_turtle(g.serialize(format="turtle"), ATTESTATIONS_GRAPH, replace=False)
    return a


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
    day = day or datetime.now(UTC).date()
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
            live = [c for c in cands if not disputed(ea, c, f)] or cands
            live.sort(key=lambda c: (rank(type_, f, c.category), -_dt(c.observed_at).timestamp(), c.source))
            win = live[0]
            r.values[f] = win.values[f]
            r.winners[f] = win
            r.candidates[f] = cands
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
    store.load_turtle(graph.serialize(format="turtle"), EFFECTIVE_GRAPH)


def effective_graph(store: RDFStore) -> Graph:
    from .changesets import _term

    res = store.query(f"SELECT ?s ?p ?o WHERE {{ GRAPH <{EFFECTIVE_GRAPH}> {{ ?s ?p ?o }} }}")
    g = Graph()
    for b in res["results"]["bindings"]:
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
    atts = read_attestations(store)
    return resolve(read_claims(store), atts), atts


def _check_attester(resolved: dict, type_: str, rid: str, attester: str, role: str) -> list[str]:
    """Attester identity comes from the request until single sign-on (Phase 1): check it
    names a real person and role, and warn when they are not the owner or steward."""
    if ("people", attester) not in resolved:
        raise ValueError(f"No person '{attester}' in the model.")
    if ("roles", role) not in resolved:
        raise ValueError(f"No role '{role}' in the model.")
    warnings = []
    if role not in resolved[("people", attester)].values.get("rolesFilled", []):
        warnings.append(f"{attester} does not fill role {role}.")
    target = resolved.get((type_, rid))
    owners = {v for k in OWNERSHIP_FIELDS for v in [target.values.get(k)] if v} if target else set()
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
    warnings = _check_attester(resolved, type_, rid, attester, role)
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
    warnings = _check_attester(resolved, type_, rid, attester, role)
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
    )
    return {"report": report, "attestation": a.summary() | {"fields": sorted(a.hashes)}, "warnings": warnings}


def authority_basis(store: RDFStore, rid: str) -> dict:
    """Decision 3: an agent's authority check may rely only on attested, current facts.
    Checks the delegated authority, the role holding it, the people filling that role,
    and the ownership of every KPI its actions affect."""
    resolved, atts = _current(store)
    auth = resolved.get(("authorities", rid))
    if auth is None:
        from .changesets import _not_found

        raise _not_found(f"No authorities with ID '{rid}'.")
    by_entity: dict[tuple[str, str], list[Attestation]] = {}
    for a in atts:
        by_entity.setdefault((a.type, a.id), []).append(a)
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
        fillers = [
            r
            for (t, _), r in sorted(resolved.items())
            if t == "people" and holder in r.values.get("rolesFilled", [])
        ]
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
