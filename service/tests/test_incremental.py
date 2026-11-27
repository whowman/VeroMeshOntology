"""The incremental planner gives exactly what the full path gives.

The same changes go to two stores, one with VEROMESH_INCREMENTAL on and one with
it off, and everything is compared after each: the reports, every source graph,
the stored effective model and the current claims. The state the fast path
carries forward is also compared with one built fresh from the store.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from rdflib import Graph

from veromesh import changesets as C
from veromesh import entities as E
from veromesh import incremental
from veromesh import provenance as P
from veromesh.rdf_store import make_store

ROOT = Path(__file__).resolve().parents[2]
ONT = str(ROOT / "ontology")
BASE = "https://example.com/org#"


def example_items() -> list[dict]:
    g = Graph().parse(ROOT / "ontology" / "examples" / "org-procure-to-pay.ttl")
    items = []
    for t in E.TYPES:
        for subj in E.subjects_of_type(g, t):
            ent = E.to_entity(g, subj, t, BASE)
            if not E.TYPES[t].keyed:
                ent.pop("id", None)
            items.append({"type": t, "op": "upsert", "entity": ent})
    return items


def item(type_: str, rid: str, **changes) -> dict:
    """An example entity with some fields changed (a new ID makes a new entity)."""
    for it in example_items():
        if it["type"] == type_ and it["entity"].get("id") == rid:
            return {"type": type_, "op": "upsert", "entity": {**copy.deepcopy(it["entity"]), **changes}}
    raise KeyError(rid)


def with_changed(type_: str, rid: str, **changes) -> list[dict]:
    """The worked example with one entity's fields changed."""
    return [
        item(type_, rid, **changes) if it["type"] == type_ and it["entity"].get("id") == rid else it
        for it in example_items()
    ]


def person(rid: str, **fields) -> dict:
    ent = {"id": rid, "name": rid, "employeeId": f"E-{rid}", "email": f"{rid.lower()}@example.com"}
    return {"type": "people", "op": "upsert", "entity": {**ent, **fields}}


# (source, mode, items): each runs on both stores, in order.
STEPS = [
    ("hr", "replace", example_items()),
    ("hr", "merge", [item("people", "DanaExample", name="Dana Renamed")]),
    ("hr", "merge", [person("NewPerson", rolesFilled=["ApManager"], reportsTo="LeeExample")]),
    ("crm", "merge", [item("people", "DanaExample", name="Dana From CRM")]),  # a second source disagrees
    (
        "hr",
        "merge",
        [item("authorities", "ApApprovalAuthority", limitAmount="900000")],
    ),  # above its parent
    ("hr", "merge", [{**person("Later", rolesFilled=["Controller"]), "validFrom": "2999-01-01"}]),
    ("hr", "merge", [person("ReportsToLater", reportsTo="Later")]),  # refers to someone not valid yet
    ("hr", "merge", [person("Later", name="Later, now", rolesFilled=["Controller"])]),  # valid from today
    (
        "hr",
        "merge",
        [{**person("Expired"), "validUntil": "2000-01-01", "observedAt": "2026-10-01T09:00:00Z"}],
    ),
    (
        "hr",
        "merge",
        [
            {"type": "roles", "op": "upsert", "entity": {"id": "NewRole", "name": "New role"}},
            person("FillsNewRole", rolesFilled=["NewRole"]),
        ],
    ),
    ("hr", "merge", [{"type": "roles", "op": "upsert", "entity": {"id": "DanaExample", "name": "Clash"}}]),
    ("hr", "merge", [person("Dangling", rolesFilled=["NoSuchRole"])]),
    ("hr", "merge", [item("people", "DanaExample", name="Dana Renamed")]),  # unchanged
    ("hr", "merge", [item("actions", "ApproveSupplierInvoice", name="Approve an invoice")]),
    ("hr", "merge", [item("agents", "InvoiceAssistant", actsFor="NewPerson")]),
    (
        "hr",
        "merge",
        [
            {
                "type": "co-approvals",
                "op": "upsert",
                "entity": {
                    "authority": "ApApprovalAuthority",
                    "approverRole": "Controller",
                    "aboveAmount": "20000",
                },
            }
        ],
    ),
    ("hr", "merge", [{"type": "people", "op": "delete", "entity": {"id": "Later"}}]),  # deletes: full path
    ("hr", "merge", [item("kpis", "OnTimePaymentRate", name="On-time payments")]),
    ("hr", "merge", [person(f"Batch{i}", rolesFilled=["ApManager"]) for i in range(25)]),  # a load
    ("crm", "merge", [person("CrmOnly"), item("people", "DanaExample", name="Dana Again")]),
    ("hr", "merge", [{"type": "people", "op": "upsert", "entity": {"id": "", "name": "No ID"}}]),
    # Deletes and removals.
    (
        "hr",
        "merge",
        [{"type": "people", "op": "delete", "entity": {"id": "NewPerson"}}],
    ),  # an agent acts for them
    ("hr", "merge", [{"type": "people", "op": "delete", "entity": {"id": "CrmOnly"}}]),  # not in this source
    (
        "crm",
        "merge",
        [{"type": "people", "op": "delete", "entity": {"id": "DanaExample"}}],
    ),  # hr still has her
    ("hr", "merge", [{"type": "people", "op": "delete", "entity": {"id": "Expired"}}]),
    # A system retired while a capability it supports still names it: a warning on the
    # capability, which points at the system (and gone again when it is active).
    ("hr", "merge", [item("systems", "Erp", lifecycle="retired")]),
    ("hr", "merge", [item("systems", "Erp", lifecycle="active")]),
    # Replace loads: the whole source again with one change, then unchanged.
    ("hr", "replace", with_changed("people", "LeeExample", name="Lee Renamed")),
    ("hr", "replace", with_changed("people", "LeeExample", name="Lee Renamed")),
    (
        "hr",
        "replace",
        example_items()
        + [{"type": "roles", "op": "upsert", "entity": {"id": "NewRole", "name": "New role"}}],
    ),
    ("crm", "merge", [person("CrmFills", rolesFilled=["NewRole"])]),  # another source uses hr's role
    ("hr", "replace", example_items()),  # drops NewRole, which crm still uses: refused
    ("crm", "replace", []),  # empty the source
]


def run(store, monkeypatch, on: bool, source: str, mode: str, items: list[dict], validate_first: bool):
    monkeypatch.setenv("VEROMESH_INCREMENTAL", "on" if on else "off")
    cs = C.create(store, source=source, mode=mode)["id"]
    try:
        C.add_items(store, cs, items)
    except ValueError as exc:
        return {"rejected": str(exc)}
    out = {}
    if validate_first:
        out["validated"] = C.validate(store, cs, base=BASE, ontology_dir=ONT)
    try:
        out["committed"] = C.commit(store, cs, base=BASE, ontology_dir=ONT)
    except C.Conflict as exc:
        out["conflict"] = (str(exc), exc.report)
    return _strip(out)


def _strip(x):
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k not in ("changeset", "version")}
    if isinstance(x, (list, tuple)):
        return [_strip(v) for v in x]
    return x


def snapshot(store) -> dict:
    claims = sorted(
        (
            c.source,
            c.type,
            c.id,
            json.dumps(c.values, sort_keys=True),
            c.valid_from,
            c.valid_until,
            c.category,
        )
        for c in P._read_claims(store, f" FILTER NOT EXISTS {{ ?c <{P.PROV.invalidatedAtTime}> ?x }}")
    )
    history = len(P._read_claims(store, ""))
    sources = {iri: set(g) for iri, g in C._read_sources(store).items()}
    listed = [
        {k: s[k] for k in ("source", "kind", "triples")} for s in C.list_sources(store) if "triples" in s
    ]
    return {
        "claims": claims,
        "history": history,
        "sources": sources,
        "effective": set(P._effective_graph(store)),
        "listed": listed,
    }


def same_state(kept: incremental.State, fresh: incremental.State) -> None:
    assert {k: set(v) for k, v in kept.graphs.items() if len(v)} == {
        k: set(v) for k, v in fresh.graphs.items() if len(v)
    }
    assert {k: {s: c for s, c in v.items()} for k, v in kept.claims.items() if v} == fresh.claims
    assert set(kept.model.graph) == set(fresh.model.graph)
    assert set(kept.today.graph) == set(fresh.today.graph)
    assert kept.model.conflicts == fresh.model.conflicts
    assert {k: r.values for k, r in kept.model.resolved.items()} == {
        k: r.values for k, r in fresh.model.resolved.items()
    }
    assert {r: {g: t for g, t in v.items() if t} for r, v in kept.typed.items() if v} == {
        r: v for r, v in fresh.typed.items() if v
    }
    assert kept.stored_effective_ok and fresh.stored_effective_ok
    for view in ("model", "today"):
        k, f = getattr(kept, view), getattr(fresh, view)
        assert k.ids == f.ids and set(k.errors) == set(f.errors) and k.count == f.count, view
        assert {r: ks for r, ks in k.referrers.items() if ks} == f.referrers, view


@pytest.mark.parametrize("proposals", ["on", "off"])
@pytest.mark.parametrize("one_request", [True, False])
@pytest.mark.parametrize("validate_first", [False, True])
def test_same_results_as_the_full_path(monkeypatch, validate_first, one_request, proposals):
    # With the proposal step on, accountability changes (a limit above its parent, a new
    # manager) are held for attestation on both paths alike; off, they apply (and are refused).
    monkeypatch.setenv("VEROMESH_PROPOSALS", proposals)
    if not one_request:  # every commit sends its writes as it goes (as large loads do)
        monkeypatch.setattr(incremental, "SMALL_TRIPLES", -1)
    fast, full = make_store("memory"), make_store("memory")
    incremental.STATS.clear()
    primed = 0
    for n, (source, mode, items) in enumerate(STEPS):
        a = run(fast, monkeypatch, True, source, mode, items, validate_first)
        b = run(full, monkeypatch, False, source, mode, items, validate_first)
        assert a == b, f"step {n}: reports differ"
        assert snapshot(fast) == snapshot(full), f"step {n}: stores differ"
        kept = fast.peek(("incremental", BASE))
        if kept is not None:
            same_state(kept, incremental._build(fast, BASE))
        primed += same_reads(fast)
        for t in E.TYPES:  # what readers have cached, for the next commit to carry forward
            C.list_entities(fast, t, BASE)
        P._current(fast)
        P._current_index(fast)
        for t, field in INDEXED:
            C.shared_index(fast, t, BASE, field)
    assert incremental.STATS["fast"] >= 10, incremental.STATS
    assert primed >= 10


INDEXED = (
    ("authorities", "holder"),
    ("co-approvals", "authority"),
    ("impacts", "action"),
    ("people", "rolesFilled"),
)


def same_reads(store) -> int:
    """Lists and beliefs the last commit primed equal what reading the store gives."""
    from veromesh import clock

    effective = P._effective_graph(store)
    n = 0
    for t in E.TYPES:
        rows = store.peek(("entities", t, BASE, None))
        if rows is not None:
            fresh_rows = C.entity_rows(effective, t, BASE)
            assert rows == fresh_rows, t
            n += 1
            shared = store.peek(("shared-entities", t, BASE))
            if shared is not None:
                assert shared[0] == [e for _, e in fresh_rows], t
                assert shared[1] == {e["id"]: e for _, e in fresh_rows if "id" in e}, t
                for e in shared[0]:  # the lookup holds the very objects in the list
                    assert shared[1][e["id"]] is e
    for t, field in INDEXED:
        index = store.peek(("shared-index", t, BASE, field))
        if index is not None:
            ents = [e for _, e in C.entity_rows(effective, t, BASE)]
            fresh: dict = {}
            for e in ents:
                v = e.get(field)
                for value in v if isinstance(v, list) else [v]:
                    if value:
                        fresh.setdefault(value, []).append(e)
            assert index == fresh, (t, field)
            n += 1
    ci = store.peek(("current-index", clock.today()))
    if ci is not None:
        resolved = P.resolve(P.read_claims(store), P.read_attestations(store))
        fillers: dict = {}
        for (t, rid), r in sorted(resolved.items()):
            if t == "people":
                for role in r.values.get("rolesFilled", []) or []:
                    fillers.setdefault(role, []).append(rid)
        assert {role: [r.id for r in rs] for role, rs in ci[2].items()} == fillers
        assert set(ci[0]) == set(resolved)
        n += 1
    current = store.peek(("current", clock.today()))
    if current is not None:
        fresh = P.resolve(P.read_claims(store), P.read_attestations(store))
        assert {k: r.values for k, r in current[0].items()} == {k: r.values for k, r in fresh.items()}
        assert {k: {f: c.source for f, c in r.winners.items()} for k, r in current[0].items()} == {
            k: {f: c.source for f, c in r.winners.items()} for k, r in fresh.items()
        }
    return n


def test_a_write_by_another_replica_is_never_missed(monkeypatch):
    """Two service replicas over one store: each sees the other's commits."""
    from veromesh.modelcache import VersionedStore
    from veromesh.rdf_store import MemoryStore

    shared = MemoryStore()
    one, two = VersionedStore(shared), VersionedStore(shared)
    monkeypatch.setenv("VEROMESH_INCREMENTAL", "on")
    C.apply(one, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)
    C.apply(two, source="hr", items=[item("people", "DanaExample", name="Two")], base=BASE, ontology_dir=ONT)
    C.apply(one, source="hr", items=[person("FromOne")], base=BASE, ontology_dir=ONT)
    C.apply(two, source="hr", items=[person("FromTwo")], base=BASE, ontology_dir=ONT)
    names = {e["id"]: e["name"] for e in C.list_entities(one, "people", BASE)}
    assert names["DanaExample"] == "Two" and "FromOne" in names and "FromTwo" in names
    for s in (one, two):
        kept = s.peek(("incremental", BASE))
        if kept is not None:
            same_state(kept, incremental._build(s, BASE))


def test_run_history_is_newest_first_within_one_second(monkeypatch):
    """Fast commits put several changesets in one second; the history still lists the newest first."""
    store = make_store("memory")
    monkeypatch.setattr(C, "now", lambda: "2026-11-19T12:00:00+00:00")
    ids = [C.create(store, source="hr", mode="merge")["id"] for _ in range(5)]
    assert [c["id"] for c in C.list_changesets(store, source="hr")] == ids[::-1]


def test_checks_share_the_planners_model_and_it_is_the_same(monkeypatch):
    """What a check believes today, taken from the planner's state, is what resolving the
    stored claims gives; and while a change holds the planner's lock past the wait, it
    is not taken."""
    from veromesh import clock

    store = make_store("memory")
    C.apply(store, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)
    C.apply(store, source="hr", items=[person("Zed")], base=BASE, ontology_dir=ONT)
    day = clock.today()
    shared = P._from_planner(store, day)
    assert shared is not None
    fresh = P.resolve(
        P._read_claims(store, f" FILTER NOT EXISTS {{ ?c <{P.PROV.invalidatedAtTime}> ?x }}"), []
    )
    assert {k: r.values for k, r in shared[0].items()} == {k: r.values for k, r in fresh.items()}
    monkeypatch.setattr(P, "PLANNER_WAIT", 0.1)  # a change that does not end in time: the store is read
    with incremental._LOCK:
        import threading

        seen = []
        t = threading.Thread(target=lambda: seen.append(P._from_planner(store, day)))
        t.start()
        t.join()
    assert seen == [None]


def test_entity_lists_from_the_planner_match_the_store():
    store = make_store("memory")
    C.apply(store, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)
    C.apply(store, source="hr", items=[person("Zed")], base=BASE, ontology_dir=ONT)
    for t in E.TYPES:
        kept = P.from_planner_effective(store, BASE, lambda g, t=t: C.entity_rows(g, t, BASE))
        assert kept is not None
        assert kept == C.entity_rows(P._effective_graph(store), t, BASE), t


def test_an_edit_builds_the_lists_readers_use_even_when_none_is_cached():
    store = make_store("memory")
    C.apply(store, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)
    C.shared_entities(store, "people", BASE)  # a reader uses the people list
    with store._lock:
        store._cache.clear()  # ...but none is cached when the next edit comes
    C.apply(store, source="hr", items=[person("Zed")], base=BASE, ontology_dir=ONT)
    held = store.entries_at(store.version()).get(("entities", "people", BASE, None))
    assert held is not None, "the edit carried no people list forward"
    assert held == C.entity_rows(P._effective_graph(store), "people", BASE)
    assert any(e.get("id") == "Zed" for _, e in held)


def test_a_check_in_one_replica_follows_an_edit_made_in_another(monkeypatch):
    """Two copies of the service over one store (what capacity.py --copies runs): an
    authority narrowed through one changes the other's decision once its version ages out."""
    import time
    from datetime import date

    from veromesh import decide
    from veromesh import decisions as D
    from veromesh.modelcache import VersionedStore
    from veromesh.rdf_store import MemoryStore

    shared = MemoryStore()
    one, two = VersionedStore(shared), VersionedStore(shared)
    two.max_age = 0.1
    monkeypatch.setenv("VEROMESH_INCREMENTAL", "on")
    C.apply(one, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)

    agent = item("agents", "InvoiceAssistant")["entity"]

    def ask() -> dict:
        return decide.check(
            two,
            BASE,
            D.MemoryLog(),
            agent="InvoiceAssistant",
            action="ApproveSupplierInvoice",
            amount=3000,
            currency="USD",
            subject=agent["signInClientId"],
            on=date(2026, 11, 16),
            require_attested=False,
        )

    before = ask()
    assert not any("above its limit" in r for r in before["reasons"])
    narrowed = item("authorities", "AssistantApprovalAuthority", limitAmount="1000")
    C.apply(one, source="hr", items=[narrowed], base=BASE, ontology_dir=ONT)
    time.sleep(0.15)
    after = ask()
    assert after["outcome"] == "consult"  # above the narrowed limit: the next authority up
    assert any("above its limit of 1,000 USD" in r for r in after["reasons"])


def test_a_replica_follows_every_change_without_reading_the_model(monkeypatch):
    """Copy two follows copy one's changes from the records they leave, and after every
    step holds the state reading the whole model gives. Changes that leave no record
    (replace loads, deletes, the full path) make it build its state again."""
    from veromesh.modelcache import VersionedStore
    from veromesh.rdf_store import MemoryStore

    shared = MemoryStore()
    one, two = VersionedStore(shared), VersionedStore(shared)
    two.max_age = 0
    builds = []
    real_build = incremental._build

    def counted(store, base):
        if store is two:
            builds.append(1)
        return real_build(store, base)

    monkeypatch.setattr(incremental, "_build", counted)
    followed = incremental.STATS["followed"]
    for source, mode, items in STEPS:
        run(one, monkeypatch, True, source, mode, items, validate_first=False)
        incremental.warm(two, BASE)
        fresh = real_build(shared_store(shared), BASE)
        kept = two.peek(("incremental", BASE))
        if fresh is not None:
            assert kept is not None
            same_state(kept, fresh)
    assert incremental.STATS["followed"] - followed >= 10  # most changes were followed
    assert len(builds) < len(STEPS) / 2


def test_a_change_with_no_record_makes_a_replica_read_the_model_again(monkeypatch):
    """A write that leaves no record (here a plain update) cannot be followed: the copy
    builds its state again, and it is right."""
    from veromesh.modelcache import VersionedStore
    from veromesh.rdf_store import MemoryStore

    shared = MemoryStore()
    one, two = VersionedStore(shared), VersionedStore(shared)
    two.max_age = 0
    monkeypatch.setenv("VEROMESH_INCREMENTAL", "on")
    C.apply(one, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)
    incremental.warm(two, BASE)
    C.apply(one, source="hr", items=[person("Recorded")], base=BASE, ontology_dir=ONT)
    one.update('INSERT DATA { GRAPH <https://example.com/other> { <a:s> <a:p> "unrecorded" } }')
    C.apply(one, source="hr", items=[person("AfterIt")], base=BASE, ontology_dir=ONT)
    followed = incremental.STATS["followed"]
    incremental.warm(two, BASE)
    assert incremental.STATS["followed"] == followed  # the chain is broken: nothing replayed
    kept = two.peek(("incremental", BASE))
    same_state(kept, incremental._build(shared_store(shared), BASE))
    assert {"Recorded", "AfterIt"} <= {e["id"] for e in C.list_entities(two, "people", BASE)}


def shared_store(inner):
    """A fresh copy's view of the shared store (nothing cached)."""
    from veromesh.modelcache import VersionedStore

    return VersionedStore(inner)


def test_a_reader_in_a_replica_finds_its_list_carried_forward(monkeypatch):
    """Copy two has read the people list; copy one adds a person. Copy two's next read
    at the new version follows first and finds the list carried forward with the new
    person, instead of rebuilding it from the model."""
    from veromesh.modelcache import VersionedStore
    from veromesh.rdf_store import MemoryStore

    shared = MemoryStore()
    one, two = VersionedStore(shared), VersionedStore(shared)
    two.max_age = 0
    C.apply(one, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)
    incremental.warm(two, BASE)
    before = C.list_entities(two, "people", BASE)
    C.apply(one, source="hr", items=[person("Newcomer")], base=BASE, ontology_dir=ONT)
    rebuilt = []
    real = C.entity_rows
    monkeypatch.setattr(C, "entity_rows", lambda *a: rebuilt.append(a[1]) or real(*a))
    followed = incremental.STATS["followed"]
    after = C.list_entities(two, "people", BASE)
    assert {e["id"] for e in after} == {e["id"] for e in before} | {"Newcomer"}
    assert incremental.STATS["followed"] == followed + 1 and "people" not in rebuilt


def test_a_store_that_respells_values_does_not_force_a_rewrite(monkeypatch):
    """Fuseki's TDB2 hands typed values back in its own spelling (xsd:decimal "1000" as
    "1000.0"). The stored effective model is the same facts, so a fresh copy's first edit
    changes only what it changes, not the whole effective graph."""
    from decimal import Decimal

    from rdflib import XSD, Literal

    from veromesh.modelcache import VersionedStore

    store = make_store("memory")
    C.apply(store, source="hr", items=example_items(), base=BASE, ontology_dir=ONT)
    read = P._effective_graph

    def as_tdb2_returns_it(st):
        out, changed = Graph(), 0
        for s, p, o in read(st):
            if isinstance(o, Literal) and o.datatype == XSD.decimal and "." not in str(o):
                o, changed = Literal(f"{Decimal(str(o)):.1f}", datatype=XSD.decimal), changed + 1
            out.add((s, p, o))
        assert changed, "the worked example has decimals to respell"
        return out

    monkeypatch.setattr(P, "_effective_graph", as_tdb2_returns_it)
    fresh = VersionedStore(store.inner)  # a freshly started copy: nothing held
    st = incremental.state(fresh, BASE)
    assert st is not None and st.stored_effective_ok

    def whole_rewrite(*_a, **_k):
        raise AssertionError("the whole effective graph was rewritten")

    monkeypatch.setattr(P, "store_effective", whole_rewrite)
    C.apply(
        fresh,
        source="hr",
        items=[item("people", "DanaExample", name="Dana Again")],
        base=BASE,
        ontology_dir=ONT,
    )


def test_a_capability_supported_by_a_retired_system_is_warned_about(monkeypatch):
    store = make_store("memory")
    run(store, monkeypatch, True, "hr", "replace", example_items(), False)
    out = run(store, monkeypatch, True, "hr", "merge", [item("systems", "Erp", lifecycle="retired")], False)
    found = json.dumps(out)
    assert "supported by a retired system" in found and "AccountsPayable" in found
    out = run(store, monkeypatch, True, "hr", "merge", [item("systems", "Erp", lifecycle="active")], False)
    assert "supported by a retired system" not in json.dumps(out)
