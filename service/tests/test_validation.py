"""The fast validation finds exactly what a full pyshacl run with RDFS inference finds,
and validating only the affected nodes finds every new problem a change causes."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pyshacl import validate as shacl_validate
from rdflib import RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SH

from veromesh import validation

ROOT = Path(__file__).resolve().parents[2]
ONT = str(ROOT / "ontology")
EX = Namespace("https://example.com/org#")
VMO = Namespace("https://veromesh.ai/ontology/org#")


def example() -> Graph:
    return Graph().parse(ROOT / "ontology" / "examples" / "org-procure-to-pay.ttl")


def reference(data: Graph) -> set[tuple[str, str, str]]:
    """What the service found before: pyshacl with its own RDFS reasoner."""
    sch = validation.schema(ONT)
    _, results, _ = shacl_validate(
        data, shacl_graph=sch.shapes, ont_graph=sch.ont, inference="rdfs", advanced=True, allow_warnings=True
    )
    return {
        (
            str(results.value(r, SH.resultSeverity)).split("#")[-1],
            str(results.value(r, SH.focusNode)),
            str(results.value(r, SH.resultMessage)),
        )
        for r in results.subjects(SH.resultSeverity, None)
    }


def fast(data: Graph, focus=None) -> set[tuple[str, str, str]]:
    return {(sev, str(f), m) for sev, f, m in validation.validate(data, ONT, focus)}


def broken(g: Graph, change) -> Graph:
    g = Graph() + g
    change(g)
    return g


CASES = {
    "the example": lambda g: None,
    "an agent without a name": lambda g: g.remove((EX.InvoiceAssistant, RDFS.label, None)),
    "a delegated limit above its parent's": lambda g: g.set(
        (EX.ApApprovalAuthority, VMO.limitAmount, Literal(Decimal("900000.00")))
    ),
    "two agents with one sign-in": lambda g: (
        g.add((EX.Copycat, VMO.signInClientId, Literal("11111111-2222-3333-4444-555555555555")))
        or g.add((EX.Copycat, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), VMO.AIAgent))
        or g.add((EX.Copycat, RDFS.label, Literal("Copycat")))
    ),
    "a role nobody fills": lambda g: (
        g.add((EX.EmptyRole, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), VMO.Role))
        or g.add((EX.EmptyRole, RDFS.label, Literal("Empty")))
    ),
    "an authority whose holder is a KPI": lambda g: g.add(
        (EX.OnTimePaymentRate, VMO.holds, EX.ControllerAuthority)
    ),
    # Followed backwards: the role's own check (someone fills it) changes.
    "a role loses its only person": lambda g: g.remove((None, VMO.fills, EX.ErpPlatformLead)),
    # Typed by a range: filling something makes it a role, which must then have a name.
    "something unnamed is filled as a role": lambda g: g.add((EX.DanaExample, VMO.fills, EX.Mystery)),
}


@pytest.mark.parametrize("case", CASES)
def test_same_findings_as_pyshacl_with_rdfs_inference(case):
    data = broken(example(), CASES[case])
    assert fast(data) == reference(data)


@pytest.mark.parametrize("case", [c for c in CASES if c != "the example"])
def test_validating_the_affected_nodes_finds_every_new_problem(case):
    before = example()
    after = broken(before, CASES[case])
    new_full = reference(after) - reference(before)
    assert new_full, "each case breaks something"
    focus = validation.affected([before, after], validation.changed_nodes(before, after, ONT))
    new_focused = fast(after, focus) - fast(before, focus)
    assert new_full == new_focused


def test_a_change_far_away_is_not_revalidated():
    before = example()
    after = broken(before, lambda g: g.set((EX.DanaExample, RDFS.label, Literal("Dana Renamed"))))
    changed = validation.changed_nodes(before, after, ONT)
    assert changed == {EX.DanaExample}
    focus = validation.affected([before, after], changed)
    assert EX.DanaExample in focus and EX.InvoiceAssistant in focus  # acts for Dana
    assert EX.Sox404 not in focus and EX.PaySuppliersCorrectly not in focus


def test_a_shared_value_does_not_pull_in_everything_that_uses_it():
    before = example()
    after = broken(
        before,
        lambda g: g.add((EX.SupplierInvoices, VMO.sensitivity, VMO.Restricted)),
    )
    changed = validation.changed_nodes(before, after, ONT)
    assert changed == {EX.SupplierInvoices}  # Restricted's own checks did not change
    focus = validation.affected([before, after], changed)
    assert EX.SupplierPayments not in focus and EX.SupplierBankAccount not in focus  # also Restricted
