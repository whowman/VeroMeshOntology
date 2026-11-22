"""The replicated knowledge graph under load: how far the read replicas fall behind,
whether decision checks go on while the writer is down, and how long a new replica
takes to rebuild from Kafka. Run after bench.py has loaded a size into the writer
(tests/perf/replicated_fuseki.sh starts the writer, replicas and Kafka):

    python tests/perf/replication.py --writer http://localhost:3030/veromesh \\
        --replicas http://localhost:3031/veromesh,http://localhost:3032/veromesh \\
        --log postgresql://... --script tests/perf/replicated_fuseki.sh

1. Catch-up after the load: when every replica holds the writer's model version, and
   the same number of triples.
2. Lag under edits: edits one a second through the service; for each, how long until
   every replica holds its version.
3. Failover: agents ask decision checks throughout; the writer is stopped, then
   started again. Checks answered and failed while it is down, how long the writer
   takes to come back and take an edit, and that an edit while it is down is refused.
4. Rebuild: a new replica from an empty volume; how long until it matches the writer.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capacity  # noqa: E402
from veromesh import changesets, decisions  # noqa: E402
from veromesh.modelcache import VERSION_GRAPH  # noqa: E402
from veromesh.rdf_store import make_store  # noqa: E402

VERSION = f"""SELECT ?v WHERE {{ GRAPH <{VERSION_GRAPH}> {{ <urn:veromesh:model>
    <https://veromesh.ai/ontology/core#modelVersion> ?v }} }}"""
COUNT = "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }"
HTTP = httpx.Client(timeout=120)


def ask(dataset: str, sparql: str) -> list[dict]:
    r = HTTP.post(
        f"{dataset}/query", data={"query": sparql}, headers={"Accept": "application/sparql-results+json"}
    )
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def version(dataset: str) -> str | None:
    try:
        rows = ask(dataset, VERSION)
    except httpx.HTTPError:
        return None
    return rows[0]["v"]["value"] if rows else None


def triples(dataset: str) -> int:
    return int(ask(dataset, COUNT)[0]["n"]["value"])


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else 0.0


def catch_up(writer: str, replicas: list[str], timeout: float = 1800) -> dict:
    """Seconds until every replica holds the writer's version, and the triple counts."""
    want, t0 = version(writer), time.perf_counter()
    waited = {}
    for r in replicas:
        while version(r) != want and time.perf_counter() - t0 < timeout:
            time.sleep(0.2)
        waited[r] = round(time.perf_counter() - t0, 1)
    counts = {"writer": triples(writer), **{r: triples(r) for r in replicas}}
    return {
        "version": want,
        "seconds": waited,
        "triples": counts,
        "same": len(set(counts.values())) == 1 and all(version(r) == want for r in replicas),
    }


def edit_lag(store, writer: str, replicas: list[str], n: int, base: str, ontology: str) -> dict:
    """``n`` edits, one a second: each edit's time, and how long until each replica has it."""
    lags: list[float] = []
    edits: list[float] = []
    for i in range(n):
        item = {"type": "people", "op": "upsert", "entity": {"id": "Person3-d0", "name": f"Replicated {i}"}}
        t = time.perf_counter()
        changesets.apply(store, source="hr", items=[item], base=base, ontology_dir=ontology)
        done = time.perf_counter()
        edits.append(done - t)
        want = version(writer)
        for r in replicas:
            while version(r) != want and time.perf_counter() - done < 60:
                time.sleep(0.01)
            lags.append(time.perf_counter() - done)
        time.sleep(max(0.0, 1.0 - (time.perf_counter() - t)))
    ms = lambda xs, p: round(pct(xs, p) * 1000, 1)  # noqa: E731
    return {
        "edits": n,
        "edit_p50_ms": ms(edits, 50),
        "edit_max_ms": ms(edits, 100),
        "lag_p50_ms": ms(lags, 50),
        "lag_p95_ms": ms(lags, 95),
        "lag_max_ms": ms(lags, 100),
    }


def failover(store, log, writer: str, pool: list[dict], container: str, *, agents: int = 8) -> dict:
    """Checks throughout; the writer stopped at 10 s and started at 25 s."""
    stop = threading.Event()
    per_second: dict[int, list[int]] = {}  # second -> [answered, failed]
    lock = threading.Lock()
    t0 = time.perf_counter()

    def worker(k: int) -> None:
        i = k
        while not stop.is_set():
            ok = True
            try:
                capacity.check_once(store, log, pool[i % len(pool)])
            except Exception:  # noqa: BLE001 — counted
                ok = False
            i += agents
            s = int(time.perf_counter() - t0)
            with lock:
                per_second.setdefault(s, [0, 0])[0 if ok else 1] += 1

    threads = [threading.Thread(target=worker, args=(k,), daemon=True) for k in range(agents)]
    for t in threads:
        t.start()
    time.sleep(10)
    subprocess.run(["docker", "stop", "-t", "30", container], check=True, capture_output=True)
    down_at = time.perf_counter() - t0
    refused = None
    try:
        changesets.apply(
            store,
            source="hr",
            items=[{"type": "people", "op": "upsert", "entity": {"id": "Person3-d0", "name": "While down"}}],
            base=capacity.BASE,
            ontology_dir=capacity.ONTOLOGY,
        )
        refused = False
    except Exception as exc:  # noqa: BLE001 — expected: the writer is down
        refused = type(exc).__name__
    time.sleep(max(0.0, 25 - (time.perf_counter() - t0)))
    subprocess.run(["docker", "start", container], check=True, capture_output=True)
    up_at = time.perf_counter() - t0
    back = None
    while time.perf_counter() - t0 < 600:
        try:
            changesets.apply(
                store,
                source="hr",
                items=[{"type": "people", "op": "upsert", "entity": {"id": "Person3-d0", "name": "Back"}}],
                base=capacity.BASE,
                ontology_dir=capacity.ONTOLOGY,
            )
            back = round(time.perf_counter() - t0 - up_at, 1)
            break
        except Exception:  # noqa: BLE001 — still starting
            time.sleep(1)
    time.sleep(5)
    stop.set()
    for t in threads:
        t.join()
    down = [v for s, v in per_second.items() if down_at <= s < up_at]
    up = [v for s, v in per_second.items() if s < down_at - 1]
    return {
        "writer_down_s": round(up_at - down_at, 1),
        "checks_per_s_before": round(statistics.mean(a for a, _ in up), 1) if up else 0,
        "checks_per_s_while_down": round(statistics.mean(a for a, _ in down), 1) if down else 0,
        "failed_while_down": sum(f for _, f in down),
        "failed_in_all": sum(f for _, f in per_second.values()),
        "edit_while_down_refused": refused,
        "writer_back_and_editing_after_s": back,
    }


def rebuild(writer: str, script: str, n: int = 3) -> dict:
    """A new replica from an empty volume: seconds until it holds the writer's version."""
    want = version(writer)
    t0 = time.perf_counter()
    subprocess.run([script, "replica", str(n)], check=True, capture_output=True)
    url = f"http://localhost:303{n}/veromesh"
    while version(url) != want and time.perf_counter() - t0 < 3600:
        time.sleep(1)
    secs = round(time.perf_counter() - t0, 1)
    same = triples(url) == triples(writer)
    subprocess.run(["docker", "rm", "-f", f"fuseki-r{n}"], capture_output=True)
    return {"seconds": secs, "same_triples": same}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the replicated knowledge graph under load")
    ap.add_argument("--writer", required=True)
    ap.add_argument("--replicas", required=True, help="comma-separated dataset URLs")
    ap.add_argument("--log", required=True, help="the decision log (postgresql://...)")
    ap.add_argument("--script", default=str(HERE / "replicated_fuseki.sh"))
    ap.add_argument("--edits", type=int, default=30)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    replicas = a.replicas.split(",")
    store = make_store(f"fuseki:{a.writer}", read_fallback=replicas[0])
    log = decisions.make_log(a.log)
    out: dict = {}

    out["catch_up"] = c = catch_up(a.writer, replicas)
    print(
        f"Catch-up after the load: replicas at the writer's version after {c['seconds']} s; triples {c['triples']}; same: {c['same']}",
        flush=True,
    )
    out["edit_lag"] = e = edit_lag(store, a.writer, replicas, a.edits, capacity.BASE, capacity.ONTOLOGY)
    print(
        f"Edits ({e['edits']}, one a second): edit p50 {e['edit_p50_ms']} ms, max {e['edit_max_ms']} ms; "
        f"replica lag p50 {e['lag_p50_ms']} ms, p95 {e['lag_p95_ms']} ms, max {e['lag_max_ms']} ms",
        flush=True,
    )
    pool = [
        {
            "agent": r["id"],
            "action": "ApproveSupplierInvoice-d" + r["id"].rsplit("-d", 1)[1],
            "subject": r["signInClientId"],
        }
        for r in changesets.list_entities(store, "agents", capacity.BASE)
        if r["id"].startswith("InvoiceAssistant-d") and r.get("signInClientId")
    ]
    out["failover"] = f = failover(store, log, a.writer, pool, "fuseki")
    print(
        f"Failover: writer down {f['writer_down_s']} s; checks a second {f['checks_per_s_before']} before, "
        f"{f['checks_per_s_while_down']} while down ({f['failed_while_down']} failed); an edit while down: "
        f"{'refused (' + f['edit_while_down_refused'] + ')' if f['edit_while_down_refused'] else 'ACCEPTED'}; "
        f"writer taking edits {f['writer_back_and_editing_after_s']} s after it started",
        flush=True,
    )
    out["catch_up_after_failover"] = c2 = catch_up(a.writer, replicas)
    print(
        f"Replicas after the failover: at the writer's version after {c2['seconds']} s; same: {c2['same']}",
        flush=True,
    )
    out["rebuild"] = r = rebuild(a.writer, a.script)
    print(
        f"A new replica from an empty volume: matches the writer after {r['seconds']} s (same triples: {r['same_triples']})",
        flush=True,
    )
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=2) + "\n")
    ok = (
        c["same"]
        and c2["same"]
        and r["same_triples"]
        and f["edit_while_down_refused"]
        and f["writer_back_and_editing_after_s"] is not None
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
