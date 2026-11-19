"""Synthetic organizations of any size, for the performance harness.

An organization is built from domains: each domain is a copy of the worked
example (one procure-to-pay value stream: objectives, KPIs, capabilities,
processes, roles, authorities, agents, systems, data products, data elements,
policies, regulations), with its IDs suffixed so domains do not collide, plus
extra people filling that domain's roles. The result is a list of changeset
items, valid against the meta-model, connected like a real model, and the same
for the same arguments (no randomness).

    python tests/perf/orggen.py --domains 10 --people 200 > org.json
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from rdflib import Graph
from veromesh import entities as E

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "ontology" / "examples" / "org-procure-to-pay.ttl"
EXAMPLE_BASE = "https://example.com/org#"
NAMESPACE = uuid.UUID("6f6b2a3e-5d0a-4a43-9b8e-0c9e1f0a7a11")


def example_items() -> list[dict]:
    """The worked example as changeset items (as GET /api/v1/examples/procure-to-pay gives them)."""
    g = Graph().parse(EXAMPLE)
    items = []
    for t in E.TYPES:
        for subj in E.subjects_of_type(g, t):
            ent = E.to_entity(g, subj, t, EXAMPLE_BASE)
            if not E.TYPES[t].keyed:
                ent.pop("id", None)
            items.append({"type": t, "op": "upsert", "entity": ent})
    return items


def _unique(field: str, value: str, d: int) -> str:
    """Values that must differ between domains (identities, catalog URNs)."""
    if field == "email":
        user, _, host = value.partition("@")
        return f"{user}+d{d}@{host}"
    if field == "employeeId":
        return f"{value}-D{d}"
    if field == "signInClientId":
        return str(uuid.uuid5(NAMESPACE, f"{value}/{d}"))
    if value.startswith("urn:li:"):
        # urn:li:dataProduct:x -> x-d3; a dataset or column URN gets its dataset name suffixed
        if ",PROD)" in value:
            return value.replace(",PROD)", f"_d{d},PROD)", 1)
        return f"{value}-d{d}"
    return value


def domain_items(template: list[dict], d: int) -> list[dict]:
    """One domain: the template with every ID and reference suffixed with -d<d>."""
    ids = {it["entity"]["id"] for it in template if "id" in it["entity"]}

    def ref(v):
        return f"{v}-d{d}" if isinstance(v, str) and v in ids else v

    out = []
    for it in template:
        ent = {}
        for k, v in it["entity"].items():
            if k == "id":
                ent[k] = f"{v}-d{d}"
            elif isinstance(v, list):
                ent[k] = [ref(x) for x in v]
            elif isinstance(v, str):
                ent[k] = _unique(k, ref(v), d) if v not in ids else ref(v)
            else:
                ent[k] = v
        if "name" in ent and isinstance(ent["name"], str):
            ent["name"] = f"{ent['name']} ({d})"
        out.append({**it, "entity": ent})
    return out


def extra_people(template: list[dict], d: int, n: int) -> list[dict]:
    """n more people in domain d, each filling one of its roles and reporting to its first person."""
    roles = [it["entity"]["id"] for it in template if it["type"] == "roles"]
    boss = next(it["entity"]["id"] for it in template if it["type"] == "people")
    return [
        {
            "type": "people",
            "op": "upsert",
            "entity": {
                "id": f"Person{i}-d{d}",
                "name": f"Person {i} ({d})",
                "employeeId": f"E-{d:04d}-{i:06d}",
                "email": f"person{i}.d{d}@example.com",
                "rolesFilled": [f"{roles[i % len(roles)]}-d{d}"],
                "reportsTo": f"{boss}-d{d}",
            },
        }
        for i in range(n)
    ]


def organization(domains: int, people_per_domain: int) -> list[list[dict]]:
    """The organization as one batch of items per domain (each a changeset)."""
    template = example_items()
    return [domain_items(template, d) + extra_people(template, d, people_per_domain) for d in range(domains)]


def counts(batches: list[list[dict]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for batch in batches:
        for it in batch:
            out[it["type"]] = out.get(it["type"], 0) + 1
    return dict(sorted(out.items()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--domains", type=int, default=1)
    ap.add_argument("--people", type=int, default=100, help="extra people per domain")
    a = ap.parse_args()
    batches = organization(a.domains, a.people)
    print(json.dumps({"counts": counts(batches), "batches": batches}))
