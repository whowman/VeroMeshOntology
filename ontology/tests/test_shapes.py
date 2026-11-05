"""Validation rules for the organizational meta-model.

Each case starts from the valid procure-to-pay example, breaks it one way with a
SPARQL update, and checks the expected rule fires with the expected severity.
Run: pip install pyshacl pytest && pytest ontology/tests
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pyshacl import validate
from rdflib import Graph, Namespace

ROOT = Path(__file__).resolve().parents[1]
SH = Namespace("http://www.w3.org/ns/shacl#")
PREFIXES = """
PREFIX vmo:  <https://veromesh.ai/ontology/org#>
PREFIX ex:   <https://example.com/org#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
"""


def _graph(*files: str) -> Graph:
    g = Graph()
    for f in files:
        g.parse(ROOT / f)
    return g


ONTOLOGY = _graph("veromesh-core.ttl", "veromesh-org.ttl")
SHAPES = _graph("shapes/veromesh-org-shapes.ttl")


def run(update: str | None = None) -> list[tuple[str, str]]:
    data = _graph("examples/org-procure-to-pay.ttl")
    if update:
        data.update(PREFIXES + update)
    _, results, _ = validate(
        data, shacl_graph=SHAPES, ont_graph=ONTOLOGY, inference="rdfs", advanced=True, allow_warnings=True
    )
    return [
        (str(results.value(r, SH.resultSeverity)).split("#")[1], str(results.value(r, SH.resultMessage)))
        for r in results.subjects(SH.resultSeverity, None)
    ]


def test_ontology_parses_and_defines_twelve_core_types():
    core = [
        "Objective", "KPI", "Policy", "BusinessCapability", "Process", "Person",
        "Role", "DelegatedAuthority", "AIAgent", "Action", "DataProduct", "System",
    ]
    for name in core:
        assert (Namespace("https://veromesh.ai/ontology/org#")[name], None, None) in ONTOLOGY, name


def test_worked_example_is_valid_with_no_findings():
    assert run() == []


CASES = [
    # --- borrowed authority: the agent's grant must be a bounded subset ---
    ("agent limit above parent",
     "DELETE { ex:AssistantApprovalAuthority vmo:limitAmount ?a } INSERT { ex:AssistantApprovalAuthority vmo:limitAmount 75000.00 } WHERE { ex:AssistantApprovalAuthority vmo:limitAmount ?a }",
     "Violation", "exceeds its parent"),
    ("agent authority with no limit under a limited parent",
     "DELETE WHERE { ex:AssistantApprovalAuthority vmo:limitAmount ?a }",
     "Violation", "exceeds its parent"),
    ("agent limit in another currency",
     "DELETE { ex:AssistantApprovalAuthority vmo:limitCurrency ?c } INSERT { ex:AssistantApprovalAuthority vmo:limitCurrency \"EUR\" } WHERE { ex:AssistantApprovalAuthority vmo:limitCurrency ?c }",
     "Violation", "uses currency EUR"),
    ("agent covers an action the parent does not",
     "INSERT DATA { ex:ReleasePayment a vmo:Action ; rdfs:label \"Release payment\" ; vmo:affects ex:OnTimePaymentRate . ex:AssistantApprovalAuthority vmo:coversAction ex:ReleasePayment }",
     "Violation", "which its parent does not"),
    ("agent authority outlives the parent",
     "DELETE { ex:AssistantApprovalAuthority vmo:validUntil ?u } INSERT { ex:AssistantApprovalAuthority vmo:validUntil \"2027-06-30\"^^xsd:date } WHERE { ex:AssistantApprovalAuthority vmo:validUntil ?u }",
     "Violation", "outside its parent's validity"),
    ("agent authority not derived from its principal",
     "DELETE { ex:InvoiceAssistant vmo:actsFor ex:DanaExample } INSERT { ex:InvoiceAssistant vmo:actsFor ex:LeeExample } WHERE {}",
     "Violation", "not a bounded subset of an authority its principal holds"),
    ("agent acts for two people",
     "INSERT DATA { ex:InvoiceAssistant vmo:actsFor ex:LeeExample }",
     "Violation", "exactly one person"),
    ("agent performs an action no authority covers",
     "INSERT DATA { ex:InvoiceAssistant vmo:performs ex:HoldSupplierInvoice }",
     "Violation", "none of its authorities covers"),
    ("authority ends before it starts",
     "DELETE { ex:ApApprovalAuthority vmo:validUntil ?u } INSERT { ex:ApApprovalAuthority vmo:validUntil \"2025-12-31\"^^xsd:date } WHERE { ex:ApApprovalAuthority vmo:validUntil ?u }",
     "Violation", "ends before it starts"),
    ("limit without currency",
     "DELETE WHERE { ex:ApApprovalAuthority vmo:limitCurrency ?c }",
     "Violation", "needs a currency"),
    ("agent authority drops the parent's co-approval",
     "DELETE { ex:AssistantApprovalAuthority vmo:limitAmount ?a } INSERT { ex:AssistantApprovalAuthority vmo:limitAmount 40000.00 } WHERE { ex:AssistantApprovalAuthority vmo:limitAmount ?a }",
     "Violation", "drops the co-approval"),
    ("co-approval without an approver role",
     "DELETE { ?c vmo:approverRole ?r } WHERE { ex:ApApprovalAuthority vmo:requiresCoApproval ?c . ?c vmo:approverRole ?r }",
     "Violation", "exactly one approver role"),
    ("delegator does not hold the authority they granted",
     "DELETE { ex:ApApprovalAuthority vmo:delegatedBy ex:Controller } INSERT { ex:ApApprovalAuthority vmo:delegatedBy ex:ErpPlatformLead } WHERE {}",
     "Warning", "does not hold an authority covering"),
    ("co-approval above the authority's own limit never applies",
     "DELETE { ?c vmo:aboveAmount ?x } INSERT { ?c vmo:aboveAmount 60000.00 } WHERE { ex:ApApprovalAuthority vmo:requiresCoApproval ?c . ?c vmo:aboveAmount ?x }",
     "Warning", "never applies"),
    # --- people: only role-relevant facts ---
    ("performance data about a person is rejected",
     "INSERT DATA { ex:DanaExample <https://example.com/hr#performanceRating> \"3 of 5\" }",
     "Violation", "Only role-relevant facts"),
    # --- ownership and structure ---
    ("KPI with two owners",
     "INSERT DATA { ex:OnTimePaymentRate vmo:ownedBy ex:Controller }",
     "Violation", "exactly one owning role"),
    ("impact listed but not in affects",
     "DELETE DATA { ex:ApproveSupplierInvoice vmo:affects ex:DuplicatePaymentRate }",
     "Violation", "does not list it under 'affects'"),
    ("critical data element without authoritative source",
     "DELETE WHERE { ex:SupplierBankAccount vmo:authoritativeSource ?s }",
     "Violation", "critical data element"),
    ("sensitivity outside the scheme",
     "INSERT DATA { ex:SupplierPayments vmo:sensitivity ex:TopSecret }",
     "Violation", ""),
    # --- completeness gaps are warnings, not errors ---
    ("KPI without owner",
     "DELETE WHERE { ex:DuplicatePaymentRate vmo:ownedBy ?o }",
     "Warning", "no owner"),
    ("role nobody fills",
     "INSERT DATA { ex:Treasurer a vmo:Role ; rdfs:label \"Treasurer\" }",
     "Warning", "Nobody currently fills"),
    ("action without impact map",
     "DELETE WHERE { ex:HoldSupplierInvoice vmo:affects ?k }",
     "Warning", "no impact map"),
]


@pytest.mark.parametrize("name,update,severity,message", CASES, ids=[c[0] for c in CASES])
def test_rule_fires(name, update, severity, message):
    findings = run(update)
    assert any(sev == severity and message in msg for sev, msg in findings), findings
