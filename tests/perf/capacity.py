"""Capacity: how many decision checks a copy of the service answers, how much
memory it holds, and where a machine runs out.

Used by bench.py (--throughput) and on its own, in grow mode:

    python tests/perf/capacity.py --step 10 --people 1000 --stop-rss-gb 6
    python tests/perf/capacity.py --step 50 --people 1000 --store fuseki:<url> --log postgresql://...

Grow mode loads the synthetic organization a step of domains at a time and, after
each step, measures an edit, a decision check, checks per second under concurrent
load and memory. It stops when the machine is exhausted: memory past --stop-rss-gb,
or the Extra large guarantee broken (an edit over 2 s, a check's 95th percentile
over 100 ms). The last good step is how far this machine carries the tier.

One process is one copy of the service: its threads share one interpreter, as a
copy's request threads do, so checks per second here is what one copy answers.
The checks go straight to the decision check (no HTTP in front), so a real copy
answers somewhat fewer.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orggen  # noqa: E402
from veromesh import changesets, decide, decisions  # noqa: E402
from veromesh import provenance as P  # noqa: E402
from veromesh.rdf_store import make_store  # noqa: E402

ROOT = HERE.parents[1]
ONTOLOGY = str(ROOT / "ontology")
BASE = "https://example.com/org#"

# The Extra large guarantee: past it, this machine is exhausted for the tier.
EDIT_LIMIT_S = 2.0
CHECK_P95_LIMIT_S = 0.1


def memory() -> dict:
    """This process's memory now and at its peak, in MB (Linux)."""
    out = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, value = line.split(":", 1)
                out["rss_mb" if key == "VmRSS" else "peak_rss_mb"] = round(int(value.split()[0]) / 1024, 1)
    except OSError:
        pass
    return out


def agents(batches: list[list[dict]]) -> list[dict]:
    """Every synthetic agent with its action and sign-in, to spread checks over."""
    out = []
    for d, batch in enumerate(batches):
        for it in batch:
            if it["type"] == "agents" and it["entity"]["id"] == f"InvoiceAssistant-d{d}":
                out.append(
                    {
                        "agent": it["entity"]["id"],
                        "action": f"ApproveSupplierInvoice-d{d}",
                        "subject": it["entity"]["signInClientId"],
                    }
                )
    return out


def check_once(store, log, a: dict) -> None:
    decide.check(
        store,
        BASE,
        log,
        agent=a["agent"],
        action=a["action"],
        subject=a["subject"],
        amount=100,
        currency="USD",
        require_attested=False,
    )


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def throughput(
    store, log, pool: list[dict], concurrency: int, seconds: float, edit_every: float | None = None
) -> dict:
    """``concurrency`` threads ask decision checks as fast as they are answered for
    ``seconds``; with ``edit_every``, an edit lands that often meanwhile (each one
    makes the next checks rebuild their cached reads)."""
    latencies: list[float] = []
    errors = [0]
    lock = threading.Lock()
    stop = threading.Event()
    edits = [0]

    def worker(k: int) -> None:
        i = k
        mine = []
        while not stop.is_set():
            a = pool[i % len(pool)]
            i += concurrency
            t = time.perf_counter()
            try:
                check_once(store, log, a)
            except Exception:  # noqa: BLE001 — counted, the run goes on
                with lock:
                    errors[0] += 1
                continue
            mine.append(time.perf_counter() - t)
        with lock:
            latencies.extend(mine)

    def editor() -> None:
        while not stop.wait(edit_every):
            edits[0] += 1
            item = {
                "type": "people",
                "op": "upsert",
                "entity": {"id": "Person3-d0", "name": f"Load {edits[0]}"},
            }
            changesets.apply(store, source="hr", items=[item], base=BASE, ontology_dir=ONTOLOGY)

    check_once(store, log, pool[0])  # warm
    threads = [threading.Thread(target=worker, args=(k,), daemon=True) for k in range(concurrency)]
    if edit_every:
        threads.append(threading.Thread(target=editor, daemon=True))
    start = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    return {
        "concurrency": concurrency,
        "edits_per_s": round(edits[0] / elapsed, 2) if edit_every else 0,
        "checks": len(latencies),
        "checks_per_s": round(len(latencies) / elapsed, 1),
        "p50_ms": round(percentile(latencies, 50) * 1000, 1),
        "p95_ms": round(percentile(latencies, 95) * 1000, 1),
        "p99_ms": round(percentile(latencies, 99) * 1000, 1),
        "errors": errors[0],
    }


def log_growth(store, pool: list[dict], sizes=(0, 1000, 5000)) -> list[dict]:
    """How a decision check slows as the day's decision log grows: a fresh log is
    filled with checks for one action, and a check for that action is timed at each
    size (the action has a per-day threshold, so the check counts today's actions)."""
    log = decisions.make_log("memory")
    a = pool[0]
    out, logged = [], 0
    for size in sizes:
        while logged < size:
            check_once(store, log, a)
            logged += 1
        times = []
        for _ in range(5):
            t = time.perf_counter()
            check_once(store, log, a)
            times.append(time.perf_counter() - t)
            logged += 1
        out.append({"entries": size, "check_ms": round(statistics.median(times) * 1000, 2)})
    return out


def throughput_table(runs: list[dict]) -> str:
    lines = ["  concurrency  checks/s   p50 ms   p95 ms   p99 ms  edits/s  errors"]
    for r in runs:
        lines.append(
            f"  {r['concurrency']:>11}  {r['checks_per_s']:>8}  {r['p50_ms']:>7}  {r['p95_ms']:>7}"
            f"  {r['p99_ms']:>7}  {r['edits_per_s']:>7}  {r['errors']:>6}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- grow mode


def grow(
    step: int,
    people: int,
    max_domains: int,
    store_spec: str,
    log_spec: str,
    stop_rss_gb: float,
    seconds: float,
    concurrency: int,
    out: str | None,
) -> int:
    store = make_store(store_spec)
    log = decisions.make_log(log_spec)
    batches = orggen.organization(max_domains, people)
    rows: list[dict] = []
    loaded = 0
    reason = f"reached {max_domains} domains"
    n = [0]

    def edit() -> float:
        n[0] += 1
        item = {"type": "people", "op": "upsert", "entity": {"id": "Person1-d0", "name": f"Grow {n[0]}"}}
        t = time.perf_counter()
        changesets.apply(store, source="hr", items=[item], base=BASE, ontology_dir=ONTOLOGY)
        return time.perf_counter() - t

    print(
        f"{'people':>9} {'triples':>10} {'load s':>8} {'edit ms':>8} {'check p95':>9} {'checks/s':>9} {'RSS MB':>8}"
    )
    while loaded < max_domains:
        t = time.perf_counter()
        try:
            for batch in batches[loaded : loaded + step]:
                changesets.apply(store, source="hr", items=batch, base=BASE, ontology_dir=ONTOLOGY)
        except MemoryError:
            reason = "out of memory while loading"
            break
        load_s = time.perf_counter() - t
        loaded = min(max_domains, loaded + step)
        edit_s = statistics.median(edit() for _ in range(3))
        pool = agents(batches[:loaded])
        step_log = decisions.make_log("memory") if log_spec == "memory" else log  # the day starts empty
        run = throughput(store, step_log, pool, concurrency, seconds)
        mem = memory()
        row = {
            "domains": loaded,
            "people": orggen.counts(batches[:loaded]).get("people", 0),
            "triples": len(P.effective_graph(store)),
            "load_step_s": round(load_s, 2),
            "edit_s": round(edit_s, 3),
            "throughput": run,
            **mem,
        }
        rows.append(row)
        print(
            f"{row['people']:>9,} {row['triples']:>10,} {load_s:>8.1f} {edit_s * 1000:>8.0f}"
            f" {run['p95_ms']:>8.1f}  {run['checks_per_s']:>8.1f} {mem.get('rss_mb', 0):>8.0f}",
            flush=True,
        )
        if mem.get("rss_mb", 0) > stop_rss_gb * 1024:
            reason = f"memory past {stop_rss_gb} GB"
            break
        if edit_s > EDIT_LIMIT_S:
            reason = f"an edit took {edit_s:.2f} s (guarantee {EDIT_LIMIT_S} s)"
            break
        if run["p95_ms"] / 1000 > CHECK_P95_LIMIT_S:
            reason = f"a check's p95 was {run['p95_ms']} ms (guarantee {CHECK_P95_LIMIT_S * 1000:.0f} ms)"
            break
    good = [
        r
        for r in rows
        if r["edit_s"] <= EDIT_LIMIT_S and r["throughput"]["p95_ms"] / 1000 <= CHECK_P95_LIMIT_S
    ]
    summary = {
        "stopped": reason,
        "carried_people": good[-1]["people"] if good else 0,
        "store": store_spec.split(":")[0],
        "log": log_spec.split(":")[0],
        "steps": rows,
    }
    print(f"Stopped: {reason}. Within the guarantee up to {summary['carried_people']:,} people.")
    if out:
        Path(out).write_text(json.dumps(summary, indent=2) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="VeroMesh capacity: grow the organization until the machine runs out"
    )
    ap.add_argument("--step", type=int, default=10, help="domains loaded between measurements")
    ap.add_argument("--people", type=int, default=1000, help="extra people per domain")
    ap.add_argument("--max-domains", type=int, default=1000)
    ap.add_argument("--store", default="memory")
    ap.add_argument("--log", default="memory", help="memory, or a PostgreSQL URL for the decision log")
    ap.add_argument("--stop-rss-gb", type=float, default=6.0, help="stop when this process holds more")
    ap.add_argument("--seconds", type=float, default=3.0, help="length of each throughput run")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    return grow(
        a.step, a.people, a.max_domains, a.store, a.log, a.stop_rss_gb, a.seconds, a.concurrency, a.out
    )


if __name__ == "__main__":
    sys.exit(main())
