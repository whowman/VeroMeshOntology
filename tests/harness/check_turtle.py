"""Every Turtle file under the given folders parses (pyoxigraph, a strict parser)."""

from __future__ import annotations

import sys
from pathlib import Path

import pyoxigraph as ox


def main(folders: list[str]) -> int:
    bad = 0
    files = sorted(p for f in folders for p in Path(f).rglob("*.ttl"))
    for p in files:
        try:
            ox.Store().load(p.read_bytes(), format=ox.RdfFormat.TURTLE)
        except SyntaxError as exc:
            bad += 1
            print(f"FAIL {p}: {exc}")
    print(f"{len(files) - bad} of {len(files)} Turtle files parse.")
    return 1 if bad or not files else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["ontology"]))
