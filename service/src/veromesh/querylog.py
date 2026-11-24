"""Every read by an agent is logged with the agent, its principal and the purpose it gave.

The white paper's interface controls: "Log every query with the agent, principal and
purpose, so that context requests are as auditable as actions." Reads go on the decision
log's hash chain as ``query`` entries, beside the decisions they lead to, so a steward can
verify them the same way (``GET /api/v1/decisions/verify``).

Agents state a purpose with the ``X-VeroMesh-Purpose`` header on REST, or the ``purpose``
argument of each MCP read tool. A read with no purpose is still answered, and logged
without one, so the share of reads with a stated purpose can be measured.

Setting ``VEROMESH_QUERY_LOG``: ``agents`` (default; callers signed in with the agent role),
``all`` (every signed-in caller's reads, for an audit period), or ``off``.
"""

from __future__ import annotations

import logging

from . import auth
from . import provenance as P
from .changesets import get_entity
from .decisions import DecisionLog
from .modelcache import cached
from .rdf_store import RDFStore

HEADER = "X-VeroMesh-Purpose"
MODES = ("agents", "all", "off")
log = logging.getLogger(__name__)


def wanted(who: auth.Caller, mode: str) -> bool:
    if mode not in MODES:
        raise ValueError(f"VEROMESH_QUERY_LOG is one of {', '.join(MODES)}, not {mode!r}.")
    if mode == "off":
        return False
    return mode == "all" or "agent" in who.roles


def _agent(store: RDFStore, base: str, client_id: str | None) -> tuple[str | None, str | None]:
    """(agent, principal) for a client ID, from the model (cached until the model changes)."""
    if not client_id:
        return None, None

    def find():
        hits = P.matching(store, "agents", "signInClientId", client_id)
        if len(hits) != 1:
            return None, None
        return hits[0], (get_entity(store, "agents", hits[0], base) or {}).get("actsFor")

    return cached(store, f"querylog-agent:{client_id}", find)


def record(
    dl: DecisionLog, store: RDFStore, base: str, who: auth.Caller, *, query: str, purpose: str | None
) -> None:
    agent, principal = _agent(store, base, who.client_id)
    try:
        dl.append(
            {
                "kind": "query",
                "subject": who.subject,
                "agent": agent,
                "principal": principal,
                "query": query[:2000],
                "purpose": (purpose or "").strip()[:500] or None,
                "outcome": "answered",
            }
        )
    except Exception:
        # The read is the agent's work; a log that cannot be written must be noticed, not
        # silently dropped, but it does not stop the answer (the decision check, which writes
        # the same log, refuses on its own if the log is down).
        log.exception("query log: could not record %s for %s", query, who.subject)
