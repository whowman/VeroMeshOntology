"""Performance harness: how the ontology service behaves as the model grows.

Builds a synthetic organization (tests/perf/orggen.py) in a store, then times
what users and agents do: load it, edit one fact, read, look up a person, ask
the decision check, read coverage, and re-sync the whole source as one replace
load with one change (what a nightly connector run does). Prints a table and writes JSON; with
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
    xl       500 x 1,000   about 500,000       the Extra large tier

--throughput 1,4,16 adds decision checks per second at each concurrency (one
process = one copy of the service), and --edits-every 1 adds a run with an edit
landing each second meanwhile. Memory is reported with every run.
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

import capacity  # noqa: E402
import orggen  # noqa: E402

from veromesh import changesets, coverage, decide, decisions, incremental  # noqa: E402
from veromesh import provenance as P  # noqa: E402
from veromesh.rdf_store import make_store  # noqa: E402

ROOT = HERE.parents[1]
ONTOLOGY = str(ROOT / "ontology")
BASE = "https://example.com/org#"
SIZES = {"smoke": (2, 100), "small": (10, 200), "medium": (40, 500), "large": (100, 1000), "xl": (500, 1000)}


def timed(fn, repeat: int = 1) -> tuple[float, object]:
    """Median seconds over `repeat` runs, and the last result."""
    times, out = [], None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t)
    return statistics.median(times), out


def run(
    domains: int,
    people: int,
    store_spec: str,
    repeat: int,
    log_spec: str = "memory",
    concurrency: list[int] | None = None,
    seconds: float = 5.0,
    edits_every: float | None = None,
) -> dict:
    store = make_store(store_spec)
    batches = orggen.organization(domains, people)
    counts = orggen.counts(batches)
    log = decisions.make_log(log_spec)

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

    whole = [it for batch in batches for it in batch]

    def resync():
        """A nightly sync: the whole source again as one replace load, one person changed."""
        n[0] += 1
        items = [
            {**it, "entity": {**it["entity"], "name": f"Synced {n[0]}"}}
            if it["type"] == "people" and it["entity"].get("id") == "Person2-d0"
            else it
            for it in whole
        ]
        cs = changesets.create(store, source="hr", mode="replace", kind="connector")["id"]
        changesets.add_items(store, cs, items)
        return changesets.commit(store, cs, base=BASE, ontology_dir=ONTOLOGY)

    def cold(fn):
        """Median time of fn right after an edit, when nothing read from the model is cached."""
        times = []
        for _ in range(repeat):
            edit()
            times.append(timed(fn)[0])
        return statistics.median(times)

    results = {
        "load_total_s": sum(load_times),
        "load_last_domain_s": load_times[-1],
        "edit_s": timed(edit, repeat)[0],
        "read_people_s": timed(lambda: changesets.list_entities(store, "people", BASE), repeat)[0],
        "find_person_s": timed(lambda: P.person_matching(store, "email", "person7.d0@example.com"), repeat)[
            0
        ],
        "check_s": timed(check, repeat)[0],
        "check_after_edit_s": cold(check),
        "read_people_after_edit_s": cold(lambda: changesets.list_entities(store, "people", BASE)),
        "coverage_s": timed(lambda: coverage.inventory(store, BASE), repeat)[0],
        "resync_s": timed(resync, 1)[0],
    }
    outcome = check()["outcome"]
    runs, growth = [], []
    if concurrency:
        pool = capacity.agents(batches)

        def fresh():  # each run starts the day with an empty in-memory log
            return decisions.make_log("memory") if log_spec == "memory" else log

        runs = [capacity.throughput(store, fresh(), pool, c, seconds) for c in concurrency]
        if edits_every:
            runs.append(
                capacity.throughput(store, fresh(), pool, max(concurrency), seconds, edit_every=edits_every)
            )
        growth = capacity.log_growth(store, pool)
    return {
        "memory": capacity.memory(),
        "throughput": runs,
        "log_growth": growth,
        "log": log_spec.split(":")[0],
        "plans": dict(incremental.STATS),
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
        f"({r['domains']} domains, store: {r['store']}; changesets planned fast/full: "
        f"{r['plans'].get('fast', 0)}/{r['plans'].get('full', 0)})",
    ]
    for k, v in r["timings"].items():
        lines.append(f"  {k:<26} {v * 1000:>10.0f} ms" if v < 1 else f"  {k:<26} {v:>10.2f} s")
    mem = r.get("memory") or {}
    if mem:
        lines.append(
            f"  {'memory_mb':<26} {mem.get('rss_mb', 0):>10.0f}    (peak {mem.get('peak_rss_mb', 0):.0f})"
        )
    if r.get("throughput"):
        lines.append(f"Decision checks under concurrent load (one copy; log: {r['log']}):")
        lines.append(capacity.throughput_table(r["throughput"]))
    if r.get("log_growth"):
        lines.append(
            "A check as the day's decision log grows: "
            + ", ".join(f"{g['entries']:,} entries {g['check_ms']} ms" for g in r["log_growth"])
        )
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
    ap.add_argument(
        "--repeat",
        type=int,
        default=5,
        help="runs per timing (median; an odd number, so one garbage collection does not decide it)",
    )
    ap.add_argument("--out", help="write the result as JSON here")
    ap.add_argument("--check", action="store_true", help="fail when over the budget in targets.json")
    ap.add_argument("--log", default="memory", help="the decision log: memory, or a postgresql:// URL")
    ap.add_argument("--throughput", help="concurrencies to measure checks per second at, e.g. 1,4,16")
    ap.add_argument("--seconds", type=float, default=5.0, help="length of each throughput run")
    ap.add_argument("--edits-every", type=float, help="also measure with an edit every this many seconds")
    ap.add_argument(
        "--clear",
        action="store_true",
        help="empty the store first (a throwaway Fuseki shared with other tests)",
    )
    a = ap.parse_args(argv)
    size = a.size or ("custom" if a.domains else "smoke")
    domains, people = SIZES.get(size, (a.domains, a.people or 100))
    if a.domains:
        domains, people = a.domains, a.people or people

    if a.clear:
        make_store(a.store).update("DROP ALL")
    conc = [int(c) for c in a.throughput.split(",")] if a.throughput else None
    r = run(domains, people, a.store, a.repeat, a.log, conc, a.seconds, a.edits_every)
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
