"""The validation worker: ``python -m veromesh.validation_worker <ontology dir>``.

Reads one JSON request per line on stdin and answers one JSON line on stdout:
``{"triples": [[s, p, o], ...], "nodes": [<IRI>...]}`` (terms as
compactgraph.encode writes them) gives ``{"findings": [[severity,
focus node as N3, message], ...]}``; ``{"warm": true}`` loads the shapes and
answers ``{"ready": true}``. It runs pyshacl for the service in its own process
(see validation._Worker), so decision checks keep the service's interpreter
while an edit validates.
"""

from __future__ import annotations

import json
import sys

from . import validation


def main() -> int:
    for line in sys.stdin:
        try:
            req = json.loads(line)
            ontology_dir = req.get("ontology") or sys.argv[1]
            if req.get("warm"):
                validation._warm(ontology_dir)
                out = {"ready": True}
            else:
                out = {"findings": validation._in_worker(req["triples"], ontology_dir, req["nodes"])}
        except Exception as exc:  # noqa: BLE001 — reported to the service, which validates itself
            out = {"error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
