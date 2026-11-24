"""Versioned publish: every change to the effective model is a publish with a model version.

The whitepaper publishes the model rather than overwriting it. Each commit, steward
correction or settled proposal that changes the model is recorded on the decision log's
hash chain as a ``publish`` entry naming the model version it made (the store's version
ID), the source and the changeset. Every decision quotes the model version it was made on
(``basis.modelVersion``), so an auditor can find the publish behind any decision, and
what the model said then (``asOf``).
"""

from __future__ import annotations

import logging

from .decisions import DecisionLog
from .rdf_store import RDFStore

log = logging.getLogger("veromesh.publish")


def model_version(store: RDFStore) -> str | None:
    return store.version() if hasattr(store, "version") else None


def record(
    dl: DecisionLog | None,
    store: RDFStore,
    subject: str,
    *,
    source: str | None,
    changeset: str | None = None,
    note: str | None = None,
) -> dict:
    """Record a publish; returns {"modelVersion", "publishSeq"} to add to the response."""
    v = model_version(store)
    if v is None or dl is None:
        return {}
    try:
        e = dl.append(
            {
                "kind": "publish",
                "outcome": "published",
                "subject": subject,
                "source": source,
                "changeset": changeset,
                "modelVersion": v,
                "note": note,
            }
        )
    except Exception:
        # The change is written; a log that cannot be written must be noticed, but it does
        # not undo the change (the decision check refuses on its own when the log is down).
        log.exception("publish: could not record model version %s", v)
        return {"modelVersion": v}
    return {"modelVersion": v, "publishSeq": e.get("seq")}


def history(dl: DecisionLog, *, after_seq: int = 0, limit: int = 100) -> list[dict]:
    return dl.find(kind="publish", after_seq=after_seq, limit=limit)
