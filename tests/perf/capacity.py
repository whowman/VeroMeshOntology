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

With copies > 1 (bench.py --copies), throughput starts more copies as their own
processes on the same store and decision log (python capacity.py copy, driven over
a pipe), spreads the agents over all of them, and adds up what they answer. Edits
land in this process only, so the other copies follow them the way a second
replica of the service does: through the model version kept in the store.
"""

from __future__ import annotations

import argparse
import atexit
import gc
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orggen  # noqa: E402
from veromesh import changesets, decide, decisions, gcsetup  # noqa: E402
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


class GcPauses:
    """Python's garbage-collector pauses while it is installed (each one stops every
    thread): how many full collections, and the longest and total pause."""

    def __init__(self) -> None:
        self.pauses: list[tuple[int, float]] = []
        self._start = 0.0

    def __call__(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._start = time.perf_counter()
        else:
            self.pauses.append((info["generation"], time.perf_counter() - self._start))

    def __enter__(self) -> GcPauses:
        gc.callbacks.append(self)
        return self

    def __exit__(self, *exc) -> None:
        gc.callbacks.remove(self)

    def summary(self) -> dict:
        full = [d for g, d in self.pauses if g == 2]
        every = [d for _, d in self.pauses]
        return {
            "gc_full": len(full),
            "gc_max_ms": round(max(every, default=0.0) * 1000, 1),
            "gc_total_ms": round(sum(every) * 1000, 1),
        }


def throughput(
    store,
    log,
    pool: list[dict],
    concurrency: int,
    seconds: float,
    edit_every: float | None = None,
    copies: int = 1,
    store_spec: str | None = None,
    log_spec: str | None = None,
) -> dict:
    """``concurrency`` agents ask decision checks as fast as they are answered for
    ``seconds``; with ``edit_every``, an edit lands that often meanwhile (each one
    makes the next checks rebuild their cached reads). With ``copies`` > 1 the agents
    are spread over that many copies of the service, this process and copies - 1
    others started on ``store_spec`` and ``log_spec`` (see the module docstring)."""
    if copies <= 1:
        return _summary(_measure(store, log, pool, concurrency, seconds, edit_every), concurrency, 1)
    if not store_spec or not log_spec:
        raise ValueError("more than one copy needs the store and log specs to start the others")
    others = _others(store_spec, log_spec, copies - 1)
    for j, c in enumerate(others, start=1):
        c.prepare(pool, concurrency, seconds, [k for k in range(concurrency) if k % copies == j])
    for c in others:
        c.wait_ready()
    mine = [k for k in range(concurrency) if k % copies == 0]
    here = _measure(
        store,
        log,
        pool,
        concurrency,
        seconds,
        edit_every,
        slots=mine,
        on_start=lambda: [c.go() for c in others],
    )
    return _combine([here] + [c.result() for c in others], concurrency, edit_every)


_OTHERS: dict[tuple[str, str], list] = {}


def _others(store_spec: str, log_spec: str, n: int) -> list:
    """The other copies, started once and kept for every run (each reads the model
    from the store once, as a replica does when it starts), stopped at exit."""
    kept = _OTHERS.setdefault((store_spec, log_spec), [])
    while len(kept) < n:
        kept.append(_Copy(store_spec, log_spec))
    return kept[:n]


def stop_copies() -> None:
    for kept in _OTHERS.values():
        for c in kept:
            c.close()
    _OTHERS.clear()


atexit.register(stop_copies)


class _Copy:
    """Another copy of the service: ``python capacity.py copy`` on the same store and
    log, told over its stdin what to ask and when to start, answering on its stdout."""

    def __init__(self, store_spec: str, log_spec: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "copy", "--store", store_spec, "--log", log_spec],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )

    def _send(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _read(self) -> str:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"a copy of the service ended early (exit {self.proc.wait()})")
        return line.strip()

    def prepare(self, pool: list[dict], concurrency: int, seconds: float, slots: list[int]) -> None:
        self._send(json.dumps({"pool": pool, "concurrency": concurrency, "seconds": seconds, "slots": slots}))

    def wait_ready(self) -> None:
        if (line := self._read()) != "ready":
            raise RuntimeError(f"a copy of the service said {line!r}, not ready")

    def go(self) -> None:
        self._send("go")

    def result(self) -> dict:
        return json.loads(self._read())

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self._send("quit")
                self.proc.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()


def _measure(
    store,
    log,
    pool: list[dict],
    concurrency: int,
    seconds: float,
    edit_every: float | None = None,
    slots: list[int] | None = None,
    on_start=None,
) -> dict:
    """The agents in ``slots`` (default all of 0..concurrency-1) asking checks in this
    process: their latencies, errors, edits and pauses, before summing up."""
    slots = list(range(concurrency)) if slots is None else slots
    latencies: list[float] = []
    errors = [0]
    lock = threading.Lock()
    stop = threading.Event()
    edits = [0]

    started: dict[int, float] = {}  # thread id -> when its current check began (for the stall report)

    def worker(k: int) -> None:
        i = k
        mine = []
        me = threading.get_ident()
        while not stop.is_set():
            a = pool[i % len(pool)]
            i += concurrency
            t = time.perf_counter()
            started[me] = t
            try:
                check_once(store, log, a)
            except Exception:  # noqa: BLE001 — counted, the run goes on
                with lock:
                    errors[0] += 1
                continue
            mine.append(time.perf_counter() - t)
            started.pop(me, None)
        with lock:
            latencies.extend(mine)

    def watchdog() -> None:
        """With VEROMESH_BENCH_STALLS=<seconds>: print where a check is waiting once it
        has taken that long (once per check), to find what a long check waits for."""
        limit = float(os.environ.get("VEROMESH_BENCH_STALLS", "0"))
        reported: set[tuple[int, float]] = set()
        while limit and not stop.wait(0.2):
            now = time.perf_counter()
            try:
                frames = sys._current_frames()
            except RuntimeError:  # pyoxigraph refuses to free its objects on this thread: try again
                continue
            for tid, t0 in list(started.items()):
                if now - t0 > limit and (tid, t0) not in reported and tid in frames:
                    reported.add((tid, t0))
                    stack = "".join(traceback.format_stack(frames[tid])[-14:])
                    others = {
                        th.name: "".join(traceback.format_stack(frames[th.ident])[-3:])
                        .strip()
                        .splitlines()[-1]
                        for th in threading.enumerate()
                        if th.ident in frames and th.ident != tid
                    }
                    print(f"--- a check waiting {now - t0:.1f} s:\n{stack}", file=sys.stderr)
                    for name, line in others.items():
                        print(f"    {name}: {line.strip()}", file=sys.stderr)

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
    threads = [threading.Thread(target=worker, args=(k,), daemon=True) for k in slots]
    if edit_every:
        threads.append(threading.Thread(target=editor, name="editor", daemon=True))
    threads.append(threading.Thread(target=watchdog, name="watchdog", daemon=True))
    with GcPauses() as pauses:
        if on_start:
            on_start()
        start = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - start
    return {
        "latencies": latencies,
        "elapsed": elapsed,
        "edits": edits[0],
        "errors": errors[0],
        **pauses.summary(),
        **memory(),
    }


def _stats(latencies: list[float], elapsed: float) -> dict:
    return {
        "checks": len(latencies),
        "checks_per_s": round(len(latencies) / elapsed, 1) if elapsed else 0.0,
        "p50_ms": round(percentile(latencies, 50) * 1000, 1),
        "p95_ms": round(percentile(latencies, 95) * 1000, 1),
        "p99_ms": round(percentile(latencies, 99) * 1000, 1),
        "max_ms": round(max(latencies, default=0.0) * 1000, 1),
    }


def _summary(m: dict, concurrency: int, copies: int) -> dict:
    return {
        "concurrency": concurrency,
        "copies": copies,
        "edits_per_s": round(m["edits"] / m["elapsed"], 2) if m["edits"] else 0,
        **_stats(m["latencies"], m["elapsed"]),
        "errors": m["errors"],
        "gc_full": m["gc_full"],
        "gc_max_ms": m["gc_max_ms"],
        "gc_total_ms": m["gc_total_ms"],
    }


def _combine(parts: list[dict], concurrency: int, edit_every: float | None) -> dict:
    """What several copies answered together: checks per second added up (each over
    its own run, which start together and last as long), percentiles over every
    check, the worst pause of any copy, and each copy on its own."""
    every = [x for p in parts for x in p["latencies"]]
    per_copy = [
        {**_stats(p["latencies"], p["elapsed"]), "rss_mb": p.get("rss_mb", 0), "gc_max_ms": p["gc_max_ms"]}
        for p in parts
    ]
    out = {
        "concurrency": concurrency,
        "copies": len(parts),
        "edits_per_s": round(parts[0]["edits"] / parts[0]["elapsed"], 2) if edit_every else 0,
        **_stats(every, 1.0),
        "errors": sum(p["errors"] for p in parts),
        "gc_full": sum(p["gc_full"] for p in parts),
        "gc_max_ms": max(p["gc_max_ms"] for p in parts),
        "gc_total_ms": round(sum(p["gc_total_ms"] for p in parts), 1),
        "per_copy": per_copy,
    }
    out["checks_per_s"] = round(sum(c["checks_per_s"] for c in per_copy), 1)
    return out


def serve_copy(store_spec: str, log_spec: str) -> int:
    """``capacity.py copy``: one more copy of the service for ``throughput``. Reads what
    to ask (one JSON line), warms its model, says ready, starts on go, prints its result
    and waits for the next run, until quit or the end of its input."""
    store = make_store(store_spec)
    log = decisions.make_log(log_spec)
    warm = False
    while (line := sys.stdin.readline().strip()) and line != "quit":
        job = json.loads(line)
        pool = job["pool"]
        if not warm:  # the model is read from the store once; checks after that hit the cache
            check_once(store, log, pool[0])
            gcsetup.after_startup()
            warm = True
        print("ready", flush=True)

        def wait_for_go() -> None:
            if sys.stdin.readline().strip() != "go":
                raise SystemExit(0)

        m = _measure(
            store, log, pool, job["concurrency"], job["seconds"], slots=job["slots"], on_start=wait_for_go
        )
        print(json.dumps(m), flush=True)
    return 0


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
    lines = [
        "  concurrency  checks/s   p50 ms   p95 ms   p99 ms   max ms  edits/s  errors"
        "  gc full  gc max ms  gc total ms"
    ]
    for r in runs:
        lines.append(
            f"  {r['concurrency']:>11}  {r['checks_per_s']:>8}  {r['p50_ms']:>7}  {r['p95_ms']:>7}"
            f"  {r['p99_ms']:>7}  {r.get('max_ms', 0):>7}  {r['edits_per_s']:>7}  {r['errors']:>6}"
            f"  {r.get('gc_full', 0):>7}  {r.get('gc_max_ms', 0):>9}  {r.get('gc_total_ms', 0):>11}"
        )
        for j, c in enumerate(r.get("per_copy", [])):
            lines.append(
                f"      copy {j + 1}: {c['checks_per_s']} checks/s, p99 {c['p99_ms']} ms,"
                f" max {c['max_ms']} ms, {c['rss_mb']:.0f} MB, gc max {c['gc_max_ms']} ms"
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
    gcsetup.after_startup()  # as the service does once it has started
    store = make_store(store_spec)
    log = decisions.make_log(log_spec)
    batches = orggen.organization(max_domains, people)
    rows: list[dict] = []
    loaded = 0
    reason = f"reached {max_domains} domains"
    n = [0]

    def edit() -> float:
        n[0] += 1
        item = {
            "type": "people",
            "op": "upsert",
            "entity": {"id": "Person1-d0", "name": f"Grow {n[0]}"},
        }
        t = time.perf_counter()
        changesets.apply(store, source="hr", items=[item], base=BASE, ontology_dir=ONTOLOGY)
        return time.perf_counter() - t

    print(
        f"{'people':>9} {'triples':>10} {'load s':>8} {'edit ms':>8} {'check p95':>9} {'checks/s':>9} {'RSS MB':>8}"
    )
    while loaded < max_domains:
        if len(rows) >= 2:  # stop before a step that would not fit: loading peaks above the steady state
            grew = rows[-1]["peak_rss_mb"] - rows[-2]["peak_rss_mb"]
            if rows[-1]["peak_rss_mb"] + 1.5 * grew > stop_rss_gb * 1024:
                reason = f"the next step would pass {stop_rss_gb} GB (peak {rows[-1]['peak_rss_mb']:.0f} MB, +{grew:.0f} MB a step)"
                break
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
        if max(mem.get("rss_mb", 0), mem.get("peak_rss_mb", 0)) > stop_rss_gb * 1024:
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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["copy"]:
        cp = argparse.ArgumentParser(description="one more copy of the service, driven by throughput()")
        cp.add_argument("--store", required=True)
        cp.add_argument("--log", required=True)
        c = cp.parse_args(argv[1:])
        return serve_copy(c.store, c.log)
    ap = argparse.ArgumentParser(
        description="VeroMesh capacity: grow the organization until the machine runs out"
    )
    ap.add_argument("--step", type=int, default=10, help="domains loaded between measurements")
    ap.add_argument("--people", type=int, default=1000, help="extra people per domain")
    ap.add_argument("--max-domains", type=int, default=1000)
    ap.add_argument("--store", default="memory")
    ap.add_argument(
        "--log",
        default="memory",
        help="memory, or a PostgreSQL URL for the decision log",
    )
    ap.add_argument(
        "--stop-rss-gb",
        type=float,
        default=6.0,
        help="stop when this process holds more",
    )
    ap.add_argument("--seconds", type=float, default=3.0, help="length of each throughput run")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    return grow(
        a.step,
        a.people,
        a.max_domains,
        a.store,
        a.log,
        a.stop_rss_gb,
        a.seconds,
        a.concurrency,
        a.out,
    )


if __name__ == "__main__":
    sys.exit(main())
