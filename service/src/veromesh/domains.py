"""Domains: which source decides each question (source weighting, issue #67).

Every fact belongs to a domain, the question it answers. For each domain a source
category is **authoritative** (it decides), **evidence** (it supports a value or raises
a question, ranked after the authoritative ones), or not listed (ranked last). VeroMesh
ships the defaults below, decided 23 November 2026; each organization can change any
domain (``PUT /api/v1/domains/{name}``), without a release. Every change is appended to
the decision log's hash chain, because it changes what agents may do.

What the domains decide today:

- **Which value wins** for each field a domain covers: its authoritative categories in
  order, then its evidence, then anything else (provenance.rank).
- **Who is employed** (decide.Model.employment): the employment domain's authoritative
  categories, and its ``contractors`` category for people with no employee ID.
- **Fields combined across sources** (``union``): a person's roles filled are every
  live source's roles together, so HR's job roles and the identity system's or the
  workbook's approval roles both count, rather than one replacing the other.
"""

from __future__ import annotations

import copy
import json

from .ontology import literal
from .rdf_store import RDFStore

CONFIG_GRAPH = "https://veromesh.ai/graph/configuration"
SETTING = "https://veromesh.ai/ontology/provenance#domain"

#: The shipped defaults. ``fields`` maps a type to the fields this domain covers.
DEFAULTS: dict[str, dict] = {
    "employment": {
        "question": "Is this person employed, from when to when?",
        "authoritative": ["hr"],
        "contractors": "iam",
        "evidence": ["iam", "erp", "cmdb", "spreadsheet"],
        "fields": {"people": ["name", "employeeId", "email"]},
    },
    "organization": {
        "question": "Whom do they report to, and what job role do they fill?",
        "authoritative": ["hr"],
        "evidence": ["iam", "spreadsheet"],
        "fields": {"people": ["reportsTo", "rolesFilled"], "roles": ["name", "definition"]},
        "union": {"people": ["rolesFilled"]},
    },
    "access": {
        "question": "Can they sign in, and to what?",
        "authoritative": ["iam"],
        "evidence": ["hr", "spreadsheet"],
        "fields": {"agents": ["signInClientId", "actsFor"]},
    },
    "authority-granted": {
        "question": "What does policy grant: who may decide what, up to what limit?",
        "authoritative": ["governance"],
        "evidence": ["hr", "spreadsheet"],
        "fields": {"policies": ["name", "type", "ownerRole", "link"]},
    },
    "authority-in-force": {
        "question": "What limit does the workflow that runs the decision enforce?",
        # The authority closest to the decision decides (decided 23 November): the workflow
        # in force first, the granted policy next. Where they differ the effective value is
        # the one in force, and the difference is a conflict: out of sync. The workflow is the
        # ERP's approval rules for money, and HR's business processes (Workday: promotions,
        # pay changes, hires, terminations) for decisions about people (step 6b).
        "authoritative": ["erp", "hr", "governance"],
        "evidence": ["iam", "spreadsheet"],
        "fields": {
            "authorities": [
                "holder",
                "actionsCovered",
                "limitAmount",
                "currency",
                "validFrom",
                "validUntil",
                "delegatedBy",
                "parentAuthority",
                "withinCapability",
                "limitKind",
                "limitValue",
                "scope",
                "scopeUnit",
            ],
            "co-approvals": ["authority", "approverRole", "approverRelation", "step", "aboveAmount"],
        },
    },
    "ai-approval": {
        "question": "Which agents may run, for which actions?",
        "authoritative": ["governance"],
        "evidence": ["spreadsheet"],
        "fields": {
            "ai-use-cases": [
                "approvedBy",
                "status",
                "coversAgents",
                "allowsActions",
                "approvedOn",
                "reviewBy",
                "conditions",
            ],
            "agents": ["performsActions"],
        },
    },
    "systems": {
        "question": "Which systems exist, who owns them, where are they in their life?",
        "authoritative": ["cmdb"],
        "evidence": ["catalog", "spreadsheet"],
        "fields": {
            "systems": ["name", "cmdbId", "businessOwner", "technicalOwner", "criticality", "lifecycle"]
        },
    },
    "capabilities": {
        "question": "Which business capabilities are there, how do they nest, and which role owns each?",
        # Decided 25 November 2026: the enterprise architecture tool (ServiceNow EA, SAP
        # LeanIX) keeps the capability map; the workbook is evidence. An organization whose
        # map lives elsewhere changes this domain (PUT /api/v1/domains/capabilities).
        "authoritative": ["ea"],
        "evidence": ["governance", "spreadsheet"],
        "fields": {"capabilities": ["name", "definition", "ownerRole", "parentCapability"]},
    },
    "objectives": {
        "question": "Which objectives are there, how do they cascade, and which role owns each?",
        # The HR suite's goals (Workday Talent) by default; the governance workbook is
        # evidence, and still the home of priorities and of which objectives pull against
        # each other (fields no domain covers). Changeable like every domain.
        "authoritative": ["hr"],
        "evidence": ["governance", "spreadsheet"],
        "fields": {"objectives": ["name", "definition", "ownerRole", "level", "parentObjective"]},
    },
    "data-meaning": {
        "question": "What does this data mean, and who answers for it?",
        "authoritative": ["catalog", "steward"],
        "evidence": ["spreadsheet"],
        "fields": {
            "data-elements": ["definition", "ownerRole", "stewardRole", "sensitivity"],
            "data-products": ["ownerRole", "stewardRole", "sensitivity"],
        },
    },
}
KEYS = ("authoritative", "evidence", "contractors", "union", "fields")


def _overrides(store: RDFStore) -> dict[str, dict]:
    res = store.query(
        f"""SELECT ?name ?value WHERE {{ GRAPH <{CONFIG_GRAPH}> {{ ?s <{SETTING}> ?name ; <{SETTING}Value> ?value }} }}"""
    )
    return {b["name"]["value"]: json.loads(b["value"]["value"]) for b in res["results"]["bindings"]}


def current(store: RDFStore) -> dict[str, dict]:
    """The domains in force: the defaults, with this organization's changes applied."""
    out = copy.deepcopy(DEFAULTS)
    for name, change in _overrides(store).items():
        if name in out:
            out[name].update(change)
            out[name]["changed"] = True
    return out


def set_domain(store: RDFStore, name: str, change: dict) -> dict:
    """Change one domain (only the keys given). Returns the domain as it now stands."""
    if name not in DEFAULTS:
        raise KeyError(f"No domain '{name}'. Domains: {', '.join(DEFAULTS)}.")
    from .provenance import CATEGORIES

    unknown = sorted(set(change) - set(KEYS))
    if unknown:
        raise ValueError(f"Unknown key(s): {', '.join(unknown)}. A domain has {', '.join(KEYS)}.")
    for k in ("authoritative", "evidence"):
        bad = [c for c in change.get(k, []) if c not in CATEGORIES]
        if bad:
            raise ValueError(f"{k}: unknown categor{'y' if len(bad) == 1 else 'ies'} {', '.join(bad)}.")
    if "contractors" in change and change["contractors"] not in (None, *CATEGORIES):
        raise ValueError("contractors is a source category, or null for none.")
    merged = {**_overrides(store).get(name, {}), **change}
    s = f"https://veromesh.ai/configuration/domain/{name}"
    store.update(
        f"""DELETE {{ GRAPH <{CONFIG_GRAPH}> {{ <{s}> ?p ?o }} }} WHERE {{ GRAPH <{CONFIG_GRAPH}> {{ <{s}> ?p ?o }} }} ;
            INSERT DATA {{ GRAPH <{CONFIG_GRAPH}> {{
              <{s}> <{SETTING}> {literal(name)} ; <{SETTING}Value> {literal(json.dumps(merged, sort_keys=True))} . }} }}"""
    )
    return current(store)[name]


def reset_domain(store: RDFStore, name: str) -> dict:
    if name not in DEFAULTS:
        raise KeyError(f"No domain '{name}'.")
    s = f"https://veromesh.ai/configuration/domain/{name}"
    store.update(f"DELETE WHERE {{ GRAPH <{CONFIG_GRAPH}> {{ <{s}> ?p ?o }} }}")
    return current(store)[name]


def field_domains(domains: dict[str, dict]) -> dict[tuple[str, str], str]:
    """(type, field) -> the domain that covers it."""
    out = {}
    for name, d in domains.items():
        for type_, fields in (d.get("fields") or {}).items():
            for f in fields:
                out[(type_, f)] = name
    return out


def order(domains: dict[str, dict], name: str) -> list[str]:
    d = domains[name]
    seen: list[str] = []
    for c in list(d.get("authoritative") or []) + list(d.get("evidence") or []):
        if c not in seen:
            seen.append(c)
    return seen


def union_fields(domains: dict[str, dict]) -> set[tuple[str, str]]:
    return {(t, f) for d in domains.values() for t, fs in (d.get("union") or {}).items() for f in fs}


# ---------------------------------------------------------------------------- in force


def activate(store: RDFStore) -> dict[str, dict]:
    """Make the store's domains the ones precedence uses (cheap: cached per store version)."""
    from . import provenance as P
    from .modelcache import cached

    doms = cached(store, "domains", lambda: current(store))
    P.use_domains(doms)
    return doms
