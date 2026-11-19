"""Performance harness: how the ontology service behaves as the model grows.

Builds a synthetic organization (tests/perf/orggen.py) in a store, then times
what users and agents do: load it, edit one fact, read, look up a person, ask
the decision check, read coverage. Prints a table and writes JSON; with
--check, fails when a timing is over the budget for that size in targets.json.

    python tests/perf/bench.py --size small
    python tests/perf/bench.py --domains 40 --people 500 --store fuseki:http://localhost:3030/veromesh
    python tests/perf/bench.py --size smoke --check          # what CI runs

Sizes (domains x extra people per domain; each domain also brings the worked
example's own people, roles, agents and the rest):
    smoke    2 x 100       about 200 people    seconds; CI
    small    10 x 200      about 2,000         the Small tier
    medium   40 x 500      about 20,000        the Medium tier
    large    100 x 1,000   about 100,000       the Large tier
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orggen  # noqa: E402
from veromesh import changesets, coverage, decide, decisions  # noqa: E402
from veromesh import provenance as P  # noqa: E402
from veromesh.rdf_store import make_store  # noqa: E402

ROOT = HERE.parents[1]
ONTOLOGY = str(ROOT / "ontology")
BASE = "https://example.com/org#"
SIZES = {"smoke": (2, 100), "small": (10, 200), "medium": (40, 500), "large": (100, 1000)}


def timed(fn, repeat: int = 1) -> tuple[float, object]:
    """Median seconds over `repeat` runs, and the last result."""
    times, out = [], None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t)
    return statistics.median(times), out


def run(domains: int, people: int, store_spec: str, repeat: int) -> dict:
    store = make_store(store_spec)
    batches = orggen.organization(domains, people)
    counts = orggen.counts(batches)
    log = decisions.make_log("memory")

    # Load: one changeset per domain, as a connector would send them.
    load_times = []
    for batch in batches:
        t, _ = timed(
            lambda b=batch: changesets.apply(store, source="hr", items=b, base=BASE, ontology_dir=ONTOLOGY)
        )
        load_times.append(t)
    triples = len(P.effective_graph(store))

    agent = "InvoiceAssistant-d0"
    client_id = next(
        it["entity"]["signInClientId"]
        for it in batches[0]
        if it["type"] == "agents" and it["entity"]["id"] == agent
    )
    n = [0]

    def edit():
        n[0] += 1
        item = {"type": "people", "op": "upsert", "entity": {"id": "Person1-d0", "name": f"Renamed {n[0]}"}}
        return changesets.apply(store, source="hr", items=[item], base=BASE, ontology_dir=ONTOLOGY)

    def check():
        return decide.check(
            store,
            BASE,
            log,
            agent=agent,
            action="ApproveSupplierInvoice-d0",
            subject=client_id,
            amount=100,
            currency="USD",
            require_attested=False,
        )

    results = {
        "load_total_s": sum(load_times),
        "load_last_domain_s": load_times[-1],
        "edit_s": timed(edit, repeat)[0],
        "read_people_s": timed(lambda: changesets.list_entities(store, "people", BASE), repeat)[0],
        "find_person_s": timed(lambda: P.person_matching(store, "email", "person7.d0@example.com"), repeat)[
            0
        ],
        "check_s": timed(check, repeat)[0],
        "coverage_s": timed(lambda: coverage.inventory(store, BASE), repeat)[0],
    }
    outcome = check()["outcome"]
    return {
        "domains": domains,
        "peoplePerDomain": people,
        "people": counts.get("people", 0),
        "entities": sum(counts.values()),
        "triples": triples,
        "store": store_spec.split(":")[0],
        "checkOutcome": outcome,
        "timings": results,
    }


def report(r: dict) -> str:
    lines = [
        f"{r['people']:,} people, {r['entities']:,} entities, {r['triples']:,} triples "
        f"({r['domains']} domains, store: {r['store']})",
    ]
    for k, v in r["timings"].items():
        lines.append(f"  {k:<20} {v * 1000:>10.0f} ms" if v < 1 else f"  {k:<20} {v:>10.2f} s")
    return "\n".join(lines)


def over_budget(r: dict, budget: dict) -> list[str]:
    return [
        f"{k}: {r['timings'][k]:.3f} s, budget {limit} s"
        for k, limit in budget.items()
        if r["timings"].get(k, 0) > limit
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="VeroMesh performance harness")
    ap.add_argument("--size", choices=sorted(SIZES), help="a named size (see the module docstring)")
    ap.add_argument("--domains", type=int)
    ap.add_argument("--people", type=int, help="extra people per domain")
    ap.add_argument("--store", default="memory", help="memory, or fuseki:<dataset URL> (an empty dataset)")
    ap.add_argument("--repeat", type=int, default=3, help="runs per timing (median)")
    ap.add_argument("--out", help="write the result as JSON here")
    ap.add_argument("--check", action="store_true", help="fail when over the budget in targets.json")
    a = ap.parse_args(argv)
    size = a.size or ("custom" if a.domains else "smoke")
    domains, people = SIZES.get(size, (a.domains, a.people or 100))
    if a.domains:
        domains, people = a.domains, a.people or people

    r = run(domains, people, a.store, a.repeat)
    r["size"] = size
    print(report(r))
    if a.out:
        Path(a.out).write_text(json.dumps(r, indent=2) + "\n")
    if a.check:
        targets = json.loads((HERE / "targets.json").read_text())
        budget = targets.get("budgets", {}).get(size)
        if budget is None:
            print(f"no budget for size {size!r} in targets.json", file=sys.stderr)
            return 2
        problems = over_budget(r, budget)
        if problems:
            print("Over budget:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        print(f"Within the {size} budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
