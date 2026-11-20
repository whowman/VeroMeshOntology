"""Where a process held Python's interpreter lock for a long time.

Reads a file of ``py-spy dump`` outputs taken one after another (separated by
lines starting with ``=== dump``), and prints the longest runs of consecutive
dumps in which the same thread held the lock (py-spy marks it ``+gil``): with many
threads taking turns, one thread holding it dump after dump is what kept every
other thread waiting. Each run is shown with the fullest stack seen in it (a
non-blocking dump sometimes reads a stack only partly).

    python tests/perf/gil_holds.py dumps.txt [min_dumps]
"""

from __future__ import annotations

import re
import sys

THREAD = re.compile(r'^Thread (0x[0-9A-Fa-f]+|\d+) \(([^)]*)\)(?:: "([^"]*)")?')


def holders(text: str, min_threads: int = 3) -> list[tuple[str, str, list[str]] | None]:
    """Per dump: (thread, name, its frames) for the thread holding the lock, or None (also
    when fewer than ``min_threads`` threads ran: one thread alone keeps nobody waiting)."""
    out: list[tuple[str, str, list[str]] | None] = []
    for dump in re.split(r"^=== dump.*$", text, flags=re.M)[1:]:
        held = None
        current = None
        threads = 0
        for line in dump.splitlines():
            m = THREAD.match(line)
            if m:
                threads += 1
                current = (m.group(1), m.group(3) or "", [], "gil" in m.group(2))
                if current[3]:
                    held = current
                continue
            if current is not None and line.startswith("    ") and line.strip():
                current[2].append(line.strip())
        out.append((held[0], held[1], held[2]) if held and threads >= min_threads else None)
    return out


def main() -> int:
    path = sys.argv[1]
    min_dumps = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    seq = holders(open(path, encoding="utf-8", errors="replace").read())
    runs: list[tuple[int, int, tuple[str, str, list[str]]]] = []
    i = 0
    while i < len(seq):
        h = seq[i]
        j = i + 1
        if h is not None:
            while j < len(seq) and seq[j] is not None and seq[j][0] == h[0]:
                j += 1
            if j - i >= min_dumps:
                fullest = max((seq[k] for k in range(i, j)), key=lambda x: len(x[2]))
                runs.append((j - i, i, fullest))
        i = j
    print(f"{len(seq)} dumps; {sum(1 for h in seq if h)} with a thread holding the lock")
    if not runs:
        print(f"No thread held the lock for {min_dumps} dumps in a row.")
        return 0
    for n, start, (tid, name, frames) in sorted(runs, reverse=True)[:5]:
        print(f"\n--- {n} dumps in a row from dump {start}: thread {tid} {name}")
        for f in frames[:25]:
            print("    " + f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
