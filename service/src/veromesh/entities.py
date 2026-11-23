"""The organizational model as REST resources.

``SHEETS`` describes every entity type the API exposes: its fields, their kinds,
which other types a reference may point to, and the help text clients show.
The same spec drives the JSON schema (``/api/v1/schema``), JSON <-> RDF
conversion, and any client that renders forms or spreadsheets from the schema.
(The name ``Sheet`` is historical: each type maps to one sheet in the loader's
workbook.)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, XSD

VMO = Namespace("https://veromesh.ai/ontology/org#")
ORG = Namespace("http://www.w3.org/ns/org#")

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MULTI_SPLIT = re.compile(r"[;\n]")

SENSITIVITY = {
    "Public": VMO.Public,
    "Internal": VMO.Internal,
    "Confidential": VMO.Confidential,
    "Restricted": VMO.Restricted,
}
DATA_CATEGORY = {
    "Personal data": VMO.PersonalData,
    "Health data": VMO.HealthData,
    "Payment card data": VMO.PaymentCardData,
    "Financial reporting data": VMO.FinancialReportingData,
}
POLICY_TYPES = {"Policy": VMO.Policy, "Regulation": VMO.Regulation}


@dataclass(frozen=True)
class Col:
    header: str
    kind: str  # id | label | text | decimal | date | bool | duration | ref | iri_enum | str_enum | class
    prop: URIRef | None = None
    targets: tuple[str, ...] = ()  # sheet names a ref may point to
    multi: bool = False
    required: bool = False
    inverse: bool = False  # value --prop--> this row, instead of this row --prop--> value
    values: dict[str, URIRef] | tuple[str, ...] = ()
    help: str = ""
    # Personal details about one person (contact, HR identifiers, reporting line):
    # answered only to callers who need them (see auth.py, need to know).
    personal: bool = False


@dataclass(frozen=True)
class Sheet:
    name: str
    cls: URIRef | None  # None when the class comes from a 'class' column
    cols: tuple[Col, ...]
    keyed: bool = True  # False: no ID column; rows hang off a parent row (Impacts, Co-approvals)
    help: str = ""

    def col(self, header: str) -> Col:
        return next(c for c in self.cols if c.header == header)


def _id() -> Col:
    return Col(
        "ID",
        "id",
        required=True,
        help="Short unique key, used to reference this row from other sheets. Letters, digits, '.', '_' or '-'.",
    )


def _name(what: str) -> Col:
    return Col("Name", "label", RDFS.label, required=True, help=f"Display name of the {what}.")


def _owner() -> Col:
    return Col(
        "Owner role",
        "ref",
        VMO.ownedBy,
        ("Roles",),
        help="ID of the role that owns this (ownership always sits with a role, not a person).",
    )


SHEETS: tuple[Sheet, ...] = (
    Sheet(
        "Objectives",
        VMO.Objective,
        (
            _id(),
            _name("objective"),
            Col(
                "Definition",
                "text",
                SKOS.definition,
                help="What achieving this objective means, in a sentence or two.",
            ),
            Col(
                "Measured by KPIs",
                "ref",
                VMO.measuredBy,
                ("KPIs",),
                multi=True,
                help="IDs of KPIs that measure progress. Separate several with ';'.",
            ),
        ),
        help="Outcomes the organization intends to achieve.",
    ),
    Sheet(
        "KPIs",
        VMO.KPI,
        (
            _id(),
            _name("KPI"),
            Col(
                "Definition",
                "text",
                SKOS.definition,
                help="How the KPI is calculated, and what a good value looks like.",
            ),
            Col("Unit", "text", VMO.unit, help="e.g. percent, days, USD"),
            _owner(),
            Col(
                "Computed from data products",
                "ref",
                VMO.computedFrom,
                ("Data products",),
                multi=True,
                help="IDs of the data products the KPI is calculated from. Separate several with ';'.",
            ),
        ),
        help="Measures of progress. Used to route impact to their owners, not as targets.",
    ),
    Sheet(
        "Capabilities",
        VMO.BusinessCapability,
        (
            _id(),
            _name("capability"),
            Col("Definition", "text", SKOS.definition, help="What the capability covers, in business terms."),
            _owner(),
            Col(
                "Serves objectives",
                "ref",
                VMO.serves,
                ("Objectives",),
                multi=True,
                help="IDs of the objectives this capability contributes to. Separate several with ';'.",
            ),
            Col(
                "Parent capability",
                "ref",
                VMO.parentCapability,
                ("Capabilities",),
                help="ID of the broader capability this one is part of. Empty for a top-level capability.",
            ),
            Col(
                "Realized by processes",
                "ref",
                VMO.realizedBy,
                ("Processes",),
                multi=True,
                help="IDs of the processes that carry out this capability. Separate several with ';'.",
            ),
            Col(
                "Produces data products",
                "ref",
                VMO.produces,
                ("Data products",),
                multi=True,
                help="IDs of the data products this capability creates and answers for. Separate several with ';'.",
            ),
            Col(
                "Consumes data products",
                "ref",
                VMO.consumes,
                ("Data products",),
                multi=True,
                help="IDs of the data products this capability depends on. Separate several with ';'.",
            ),
        ),
        help="What the organization is able to do; the spine of the model.",
    ),
    Sheet(
        "Processes",
        VMO.Process,
        (
            _id(),
            _name("process"),
            _owner(),
            Col(
                "Runs on systems",
                "ref",
                VMO.runsOn,
                ("Systems",),
                multi=True,
                help="IDs of the systems the process runs on. Separate several with ';'.",
            ),
        ),
        help="Business processes that realize capabilities.",
    ),
    Sheet(
        "Policies",
        None,
        (
            _id(),
            _name("policy or regulation"),
            Col(
                "Type",
                "class",
                values=POLICY_TYPES,
                required=True,
                help="Policy (internal rule) or Regulation (external law).",
            ),
            Col("Jurisdiction", "text", VMO.jurisdiction, help="For regulations, e.g. US, EU."),
            _owner(),
            Col(
                "Bounds",
                "ref",
                VMO.bounds,
                ("Authorities", "Actions", "Data products", "Systems"),
                multi=True,
                help="IDs of authorities, actions, data products or systems this constrains.",
            ),
            Col("Link", "text", RDFS.seeAlso, help="URL of the policy text."),
        ),
        help="Internal policies and external regulations.",
    ),
    Sheet(
        "Roles",
        VMO.Role,
        (
            _id(),
            _name("role"),
            Col(
                "Definition",
                "text",
                SKOS.definition,
                help="What the role is accountable for, as its job description would say it.",
            ),
        ),
        help="Job roles or positions. Not access-control roles.",
    ),
    Sheet(
        "People",
        VMO.Person,
        (
            _id(),
            _name("person"),
            Col(
                "Employee ID",
                "text",
                VMO.employeeId,
                help="From HR; used to match the same person across systems.",
                personal=True,
            ),
            Col(
                "Email",
                "text",
                VMO.email,
                personal=True,
                help="Work email. Used to match the person to their sign-in.",
            ),
            Col(
                "Roles filled",
                "ref",
                VMO.fills,
                ("Roles",),
                multi=True,
                help="IDs of the roles the person holds today. Separate several with ';'.",
            ),
            Col(
                "Reports to",
                "ref",
                ORG.reportsTo,
                ("People",),
                personal=True,
                help="ID of the person's manager.",
            ),
        ),
        help="Only role-relevant facts. Anything else is rejected by the validation rules.",
    ),
    Sheet(
        "Authorities",
        VMO.DelegatedAuthority,
        (
            _id(),
            _name("authority"),
            Col(
                "Holder",
                "ref",
                VMO.holds,
                ("Roles", "People", "Agents"),
                required=True,
                inverse=True,
                help="ID of the role, person or agent that holds this authority.",
            ),
            Col(
                "Actions covered",
                "ref",
                VMO.coversAction,
                ("Actions",),
                multi=True,
                required=True,
                help="IDs of the actions this authority lets the holder take. Separate several with ';'.",
            ),
            Col(
                "Within capability",
                "ref",
                VMO.withinCapability,
                ("Capabilities",),
                help="ID of the capability the authority is limited to. Empty: wherever its actions apply.",
            ),
            Col(
                "Limit amount",
                "decimal",
                VMO.limitAmount,
                help="The largest amount the holder may decide for one action, in the currency given. Empty: no amount limit.",
            ),
            Col("Currency", "text", VMO.limitCurrency, help="ISO 4217 code, e.g. USD."),
            Col(
                "Valid from",
                "date",
                VMO.validFrom,
                help="First day the authority holds (YYYY-MM-DD). Empty: from now.",
            ),
            Col(
                "Valid until",
                "date",
                VMO.validUntil,
                help="Last day the authority holds (YYYY-MM-DD). Empty: until withdrawn.",
            ),
            Col(
                "Delegated by",
                "ref",
                VMO.delegatedBy,
                ("People", "Roles", "Policies"),
                help="Who granted it: a person, a role, or the policy that establishes it.",
            ),
            Col(
                "Parent authority",
                "ref",
                VMO.boundedSubsetOf,
                ("Authorities",),
                help="For narrower copies (e.g. an agent's authority): the authority this is a bounded subset of.",
            ),
        ),
        help="Grants to decide or act, with their limits.",
    ),
    Sheet(
        "Co-approvals",
        VMO.CoApproval,
        (
            Col(
                "Authority",
                "ref",
                VMO.requiresCoApproval,
                ("Authorities",),
                required=True,
                inverse=True,
                help="ID of the authority that requires this co-approval.",
            ),
            Col(
                "Approver role",
                "ref",
                VMO.approverRole,
                ("Roles",),
                required=True,
                help="ID of the role whose second sign-off is needed.",
            ),
            Col(
                "Above amount",
                "decimal",
                VMO.aboveAmount,
                required=True,
                help="Applies to actions above this amount, in the authority's currency.",
            ),
        ),
        keyed=False,
        help="Second sign-offs required above an amount. One row per tier.",
    ),
    Sheet(
        "Agents",
        VMO.AIAgent,
        (
            _id(),
            _name("agent"),
            Col(
                "Acts for",
                "ref",
                VMO.actsFor,
                ("People",),
                required=True,
                help="ID of the one person the agent acts for.",
            ),
            Col(
                "Sign-in client ID",
                "text",
                VMO.signInClientId,
                help="The client (application) ID the agent signs in with at your identity provider; links its sign-in to this agent.",
            ),
            Col(
                "Performs actions",
                "ref",
                VMO.performs,
                ("Actions",),
                multi=True,
                help="IDs of the actions the agent is built to take. Separate several with ';'.",
            ),
            Col(
                "Consumes data products",
                "ref",
                VMO.consumes,
                ("Data products",),
                multi=True,
                help="IDs of the data products the agent reads. Separate several with ';'.",
            ),
            Col(
                "Runs on systems",
                "ref",
                VMO.runsOn,
                ("Systems",),
                multi=True,
                help="IDs of the systems the agent works in. Separate several with ';'.",
            ),
        ),
        help="AI agents. Each acts for exactly one person.",
    ),
    Sheet(
        "AI use cases",
        VMO.AIUseCase,
        (
            _id(),
            _name("approved AI use case"),
            Col(
                "Approved by",
                "ref",
                VMO.approvedBy,
                ("Roles",),
                required=True,
                help="ID of the board or committee (as a role) that approved it, e.g. the AI review board.",
            ),
            Col(
                "Status",
                "str_enum",
                VMO.approvalStatus,
                values=("approved", "conditional", "suspended", "retired"),
                required=True,
                help="approved: may run. conditional: may run under the conditions given. suspended: must not run for now. retired: no longer in use.",
            ),
            Col(
                "Covers agents",
                "ref",
                VMO.coversAgent,
                ("Agents",),
                multi=True,
                required=True,
                help="IDs of the agents this approval applies to. Separate several with ';'.",
            ),
            Col(
                "Allows actions",
                "ref",
                VMO.allowsAction,
                ("Actions",),
                multi=True,
                help="Actions the approved use may take. Empty: the approval does not limit actions.",
            ),
            Col("Approved on", "date", VMO.approvedOn, help="Date of the board's decision (YYYY-MM-DD)."),
            Col("Review by", "date", VMO.reviewBy, help="When the board must review the approval again."),
            Col("Conditions", "text", VMO.conditions, help="Conditions the board attached."),
        ),
        help="Governance board decisions: which AI uses may run, for which agents and actions. From the board's register or minutes.",
    ),
    Sheet(
        "Actions",
        VMO.Action,
        (
            _id(),
            _name("action"),
            Col(
                "Part of process",
                "ref",
                VMO.partOfProcess,
                ("Processes",),
                help="ID of the process the action belongs to.",
            ),
            Col(
                "Affects",
                "ref",
                VMO.affects,
                ("KPIs", "Capabilities"),
                multi=True,
                help="IDs of KPIs or capabilities this action can move. Rows on the Impacts sheet are added here automatically.",
            ),
        ),
        help="Types of consequential action, e.g. 'approve supplier invoice'.",
    ),
    Sheet(
        "Impacts",
        VMO.Impact,
        (
            Col(
                "Action",
                "ref",
                VMO.hasImpact,
                ("Actions",),
                required=True,
                inverse=True,
                help="ID of the action that can move the KPI or capability.",
            ),
            Col(
                "Impact on",
                "ref",
                VMO.impactOn,
                ("KPIs", "Capabilities"),
                required=True,
                help="ID of the KPI or capability the action can move.",
            ),
            Col(
                "Materiality threshold",
                "decimal",
                VMO.materialityThreshold,
                required=True,
                help="Below this, the agent acts and informs the owner; above it, it consults the owner first.",
            ),
            Col(
                "Threshold unit",
                "text",
                VMO.materialityUnit,
                required=True,
                help="e.g. USD per invoice, invoices per day.",
            ),
        ),
        keyed=False,
        help="Materiality thresholds per action and KPI or capability.",
    ),
    Sheet(
        "Data products",
        VMO.DataProduct,
        (
            _id(),
            _name("data product"),
            _owner(),
            Col(
                "Steward role",
                "ref",
                VMO.steward,
                ("Roles",),
                help="ID of the role that looks after the product's quality and definitions day to day.",
            ),
            Col(
                "Runs on systems",
                "ref",
                VMO.runsOn,
                ("Systems",),
                multi=True,
                help="IDs of the systems the data product is stored in or served from. Separate several with ';'.",
            ),
            Col(
                "Feeds data products",
                "ref",
                VMO.feeds,
                ("Data products",),
                multi=True,
                help="IDs of the data products built from this one (downstream). Separate several with ';'.",
            ),
            Col(
                "Contains data elements",
                "ref",
                VMO.containsElement,
                ("Data elements",),
                multi=True,
                help="IDs of the business data elements the product holds. Separate several with ';'.",
            ),
            Col(
                "Sensitivity",
                "iri_enum",
                VMO.sensitivity,
                values=SENSITIVITY,
                help="The highest sensitivity of the data it holds; it decides who may see it.",
            ),
            Col(
                "Catalog URN",
                "text",
                VMO.catalogUrn,
                help="The data product's URN in DataHub, if registered.",
            ),
        ),
        help="Governed, owned data with known producers and consumers.",
    ),
    Sheet(
        "Systems",
        VMO.System,
        (
            _id(),
            _name("system"),
            Col(
                "CMDB ID",
                "text",
                VMO.cmdbId,
                help="The system's ID in your CMDB, so the two records can be matched.",
            ),
            Col(
                "Business owner",
                "ref",
                VMO.businessOwner,
                ("Roles",),
                help="ID of the role accountable for what the system is used for.",
            ),
            Col(
                "Technical owner",
                "ref",
                VMO.technicalOwner,
                ("Roles",),
                help="ID of the role accountable for running and changing it.",
            ),
            Col(
                "Criticality",
                "str_enum",
                VMO.criticality,
                values=("low", "medium", "high", "critical"),
                help="How much the business depends on the system.",
            ),
            Col(
                "Lifecycle",
                "str_enum",
                VMO.lifecycleStatus,
                values=("planned", "active", "retiring", "retired"),
                help="Where the system is in its life.",
            ),
            Col(
                "Sensitivity",
                "iri_enum",
                VMO.sensitivity,
                values=SENSITIVITY,
                help="The highest sensitivity of the data the system holds.",
            ),
            Col(
                "Regulatory scope",
                "ref",
                VMO.regulatoryScope,
                ("Policies",),
                multi=True,
                help="IDs of regulations that apply.",
            ),
        ),
        help="Applications and platforms.",
    ),
    Sheet(
        "Data elements",
        VMO.DataElement,
        (
            _id(),
            _name("data element"),
            Col(
                "Definition",
                "text",
                SKOS.definition,
                help="What the data element means in business terms, as your glossary states it.",
            ),
            Col(
                "Critical",
                "bool",
                VMO.criticalDataElement,
                help="yes or no. Critical elements need an owner, steward and authoritative source.",
            ),
            _owner(),
            Col(
                "Steward role",
                "ref",
                VMO.steward,
                ("Roles",),
                help="ID of the role that maintains its definition and quality day to day.",
            ),
            Col(
                "Authoritative source",
                "ref",
                VMO.authoritativeSource,
                ("Systems",),
                help="ID of the system that is the source of truth for it.",
            ),
            Col(
                "Sensitivity",
                "iri_enum",
                VMO.sensitivity,
                values=SENSITIVITY,
                help="How sensitive the data element is; it decides who may see it.",
            ),
            Col(
                "Data categories",
                "iri_enum",
                VMO.dataCategory,
                multi=True,
                values=DATA_CATEGORY,
                help="Regulatory categories it falls into. Separate several with ';'.",
            ),
            Col("Retention", "duration", VMO.retention, help="ISO 8601 duration, e.g. P7Y for seven years."),
            Col(
                "Catalog terms",
                "text",
                VMO.catalogTerm,
                multi=True,
                help="DataHub glossary term or tag URNs that mark this element's columns; "
                "columns carrying them are suggested as bindings.",
            ),
        ),
        help="Business-level data items. Physical columns in DataHub are bound to these.",
    ),
    Sheet(
        "Column bindings",
        VMO.ColumnBinding,
        (
            Col(
                "Data element",
                "ref",
                VMO.bindsElement,
                ("Data elements",),
                required=True,
                help="ID of the data element the column holds.",
            ),
            Col(
                "Column",
                "text",
                VMO.columnUrn,
                required=True,
                help="DataHub column URN: urn:li:schemaField:(<dataset URN>,<field path>).",
            ),
        ),
        keyed=False,
        help="Which physical columns in DataHub hold each business data element. One row per column.",
    ),
)
SHEET_BY_NAME = {s.name: s for s in SHEETS}


# --------------------------------------------------------------------------- resource names and keys


def type_name(sheet: Sheet) -> str:
    """REST resource name, e.g. 'Data products' -> 'data-products'."""
    return re.sub(r"[^a-z0-9]+", "-", sheet.name.lower()).strip("-")


def field_key(col: Col) -> str:
    """JSON key, e.g. 'Owner role' -> 'ownerRole', 'Measured by KPIs' -> 'measuredByKpis'."""
    if col.kind == "id":
        return "id"
    if col.kind == "label":
        return "name"
    words = [w for w in re.split(r"[^A-Za-z0-9]+", col.header) if w]
    return words[0].lower() + "".join(w[:1].upper() + w[1:].lower() for w in words[1:])


TYPES: dict[str, Sheet] = {type_name(s): s for s in SHEETS}
# Per type, the keys of fields holding personal details (Col.personal).
PERSONAL_FIELDS: dict[str, frozenset[str]] = {
    t: frozenset(field_key(c) for c in s.cols if c.personal)
    for t, s in TYPES.items()
    if any(c.personal for c in s.cols)
}
TYPE_OF_SHEET: dict[str, str] = {s.name: type_name(s) for s in SHEETS}
FIELDS: dict[str, dict[str, Col]] = {t: {field_key(c): c for c in s.cols} for t, s in TYPES.items()}


def check_type(name: str) -> Sheet:
    if name not in TYPES:
        raise LookupError(f"Unknown entity type '{name}'. Known types: {', '.join(TYPES)}.")
    return TYPES[name]


def schema() -> list[dict]:
    """Machine-readable description of every resource type, for clients."""
    out = []
    for t, s in TYPES.items():
        fields = []
        for c in s.cols:
            f = {
                "key": field_key(c),
                "label": c.header,
                "kind": c.kind,
                "required": c.required or c.kind == "id",
                "multi": c.multi,
                "help": c.help,
            }
            if c.personal:
                f["personal"] = True
            if c.kind == "ref":
                f["targets"] = [TYPE_OF_SHEET[x] for x in c.targets]
            if c.kind in ("iri_enum", "class"):
                f["values"] = list(c.values)  # type: ignore[arg-type]
            elif c.kind == "str_enum":
                f["values"] = list(c.values)
            elif c.kind == "bool":
                f["values"] = ["yes", "no"]
            fields.append(f)
        if not s.keyed:
            fields.insert(
                0,
                {
                    "key": "id",
                    "label": "ID",
                    "kind": "id",
                    "required": False,
                    "multi": False,
                    "help": "Optional; generated from the other fields when omitted.",
                },
            )
        out.append({"type": t, "label": s.name, "help": s.help, "keyed": s.keyed, "fields": fields})
    return out


# --------------------------------------------------------------------------- IRIs


def iri_for(base: str, local: str) -> URIRef:
    return URIRef(base + quote(local, safe="._-"))


def local_id(base: str, iri) -> str:
    s = str(iri)
    if base and s.startswith(base):
        return unquote(s[len(base) :])
    return re.split(r"[#/:]", s.rstrip("/#"))[-1]


def generated_id(type_: str, entity: dict) -> str:
    """Stable ID for types without one (impacts, co-approvals, column bindings): parent + content hash.
    The parent is the row's inverse reference, or else its first reference."""
    sheet = TYPES[type_]
    parent_key = next((field_key(c) for c in sheet.cols if c.inverse), None) or next(
        field_key(c) for c in sheet.cols if c.kind == "ref"
    )
    parent = str(entity.get(parent_key) or "item")
    body = json.dumps({k: v for k, v in sorted(entity.items()) if k != "id"}, sort_keys=True, default=str)
    return f"{parent}.{type_}.{hashlib.sha1(body.encode()).hexdigest()[:8]}"


# --------------------------------------------------------------------------- JSON -> RDF


@dataclass
class FieldError:
    type: str
    id: str
    field: str
    message: str

    def as_dict(self) -> dict:
        return {"type": self.type, "id": self.id, "field": self.field, "message": self.message}


@dataclass
class Converted:
    subject: URIRef
    triples: list[tuple] = field(default_factory=list)
    errors: list[FieldError] = field(default_factory=list)


def _values(raw, split: bool = True) -> list:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [v for v in raw if v is not None and v != ""]
    if isinstance(raw, str):
        if not split:
            return [raw.strip()] if raw.strip() else []
        return [p.strip() for p in MULTI_SPLIT.split(raw) if p.strip()]
    return [raw]


def entity_id(type_: str, entity: dict) -> str:
    rid = entity.get("id")
    if rid in (None, "") and not TYPES[type_].keyed:
        return generated_id(type_, entity)
    return str(rid or "")


def to_triples(type_: str, entity: dict, base: str, resolve, *, partial: bool = False) -> Converted:
    """Convert one JSON entity. ``resolve(id) -> type name | None`` checks references.

    ``partial``: the entity states only some fields (a steward correction, one
    source's view of a field-merged entity); missing required fields are not errors."""
    sheet = check_type(type_)
    rid = entity_id(type_, entity)
    subj = iri_for(base, rid) if rid else URIRef(base + "invalid")
    out = Converted(subject=subj)

    def err(key: str, msg: str):
        out.errors.append(FieldError(type_, rid, key, msg))

    if not rid:
        err("id", "Missing ID.")
        return out
    if not ID_PATTERN.match(rid) and sheet.keyed:
        err("id", f"'{rid}' is not a valid ID: use letters, digits, '.', '_' or '-'.")
        return out
    fields = FIELDS[type_]
    for key in entity:
        if key not in fields and not (key == "id" and not sheet.keyed):
            err(key, f"Unknown field for {type_}.")
    if sheet.cls is not None:
        out.triples.append((subj, RDF.type, sheet.cls))
    for key, col in fields.items():
        if col.kind == "id":
            continue
        # Free text is one value even with a ';' in it; only lists and references split.
        vals = _values(entity.get(key), split=col.multi or col.kind not in ("text", "label"))
        if not col.multi and len(vals) > 1:
            err(key, "Only one value allowed.")
            vals = vals[:1]
        if not vals:
            if col.required and not partial:
                err(key, "Required value missing.")
            continue
        for v in vals:
            obj = _to_node(col, v, base, resolve, lambda m, k=key: err(k, m))
            if obj is None:
                continue
            if col.kind == "class":
                out.triples.append((subj, RDF.type, obj))
            elif col.inverse:
                out.triples.append((obj, col.prop, subj))
            else:
                out.triples.append((subj, col.prop, obj))
            if type_ == "impacts" and key == "impactOn":
                action = entity.get("action")
                if action:
                    out.triples.append((iri_for(base, str(action)), VMO.affects, obj))
    return out


def _to_node(col: Col, value, base: str, resolve, bad):
    k = col.kind
    text = ("yes" if value else "no") if isinstance(value, bool) else str(value).strip()
    if k in ("label", "text"):
        return Literal(text)
    if k == "ref":
        found = resolve(text)
        allowed = [TYPE_OF_SHEET[x] for x in col.targets]
        if found is None:
            bad(f"No entity with ID '{text}' (expected one of: {', '.join(allowed)}).")
            return None
        if found not in allowed:
            bad(f"'{text}' is a {found} entity; expected one of: {', '.join(allowed)}.")
            return None
        return iri_for(base, text)
    if k == "decimal":
        try:
            return Literal(Decimal(text.replace(",", "")), datatype=XSD.decimal)
        except InvalidOperation:
            bad(f"'{text}' is not a number.")
            return None
    if k == "date":
        try:
            return Literal(date.fromisoformat(text[:10]), datatype=XSD.date)
        except ValueError:
            bad(f"'{text}' is not a date (use YYYY-MM-DD).")
            return None
    if k == "bool":
        low = text.lower()
        if low in ("yes", "y", "true", "1"):
            return Literal(True)
        if low in ("no", "n", "false", "0"):
            return Literal(False)
        bad(f"'{text}' is not yes or no.")
        return None
    if k == "duration":
        if not re.fullmatch(r"P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+S)?)?", text):
            bad(f"'{text}' is not an ISO 8601 duration such as P7Y.")
            return None
        return Literal(text, datatype=XSD.duration)
    if k in ("iri_enum", "class"):
        match = next((v for key, v in col.values.items() if key.lower() == text.lower()), None)  # type: ignore[union-attr]
        if match is None:
            bad(f"'{text}' is not one of: {', '.join(col.values)}.")
        return match
    if k == "str_enum":
        if text.lower() in col.values:
            return Literal(text.lower())
        bad(f"'{text}' is not one of: {', '.join(col.values)}.")
        return None
    raise AssertionError(f"unknown field kind {k}")


# --------------------------------------------------------------------------- RDF -> JSON


def types_of(graph: Graph, subject) -> list[str]:
    classes = set(graph.objects(subject, RDF.type))
    found = []
    for t, s in TYPES.items():
        if s.cls is not None and s.cls in classes:
            found.append(t)
        elif s.cls is None and classes & set(POLICY_TYPES.values()):
            found.append(t)
    return found


def subjects_of_type(graph: Graph, type_: str) -> list:
    sheet = TYPES[type_]
    classes = [sheet.cls] if sheet.cls is not None else list(POLICY_TYPES.values())
    return sorted({s for c in classes for s in graph.subjects(RDF.type, c)}, key=str)


def to_entity(graph: Graph, subject, type_: str, base: str) -> dict:
    ent: dict = {}
    for key, col in FIELDS[type_].items():
        if col.kind == "id":
            continue
        if col.kind == "class":
            ent[key] = "Regulation" if (subject, RDF.type, VMO.Regulation) in graph else "Policy"
            continue
        vals = (
            list(graph.subjects(col.prop, subject)) if col.inverse else list(graph.objects(subject, col.prop))
        )
        if col.kind == "ref":
            out = sorted(local_id(base, v) for v in vals)
        elif col.kind == "iri_enum":
            rev = {v: k for k, v in col.values.items()}  # type: ignore[union-attr]
            out = sorted(rev.get(v, str(v)) for v in vals)
        elif col.kind == "bool":
            out = [bool(v.toPython()) for v in vals]
        elif col.kind == "decimal":
            # One canonical form: stores may round-trip 50000.00 as 50000.
            out = sorted(format(Decimal(str(v)).normalize(), "f") for v in vals)
        else:
            out = sorted(str(v) for v in vals)
        if col.multi:
            ent[key] = out
        elif out:
            ent[key] = out[0]
    return {"id": local_id(base, subject), **ent}
