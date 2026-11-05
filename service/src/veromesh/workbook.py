"""Spreadsheet loader: an Excel workbook <-> the organizational meta-model.

One spec (``SHEETS``) drives three things so they can never drift apart:

* ``build_template``  - a workbook with one sheet per entity type, header help
  and drop-downs, optionally pre-filled from an existing graph (export);
* ``workbook_to_graph`` - rows -> RDF, with row-level errors (unknown IDs,
  duplicates, bad values) reported against sheet, row and column;
* ``graph_to_workbook`` - RDF -> rows, for export and round-trips.

Rows reference each other by ID. IDs are unique across the whole workbook and
become IRIs under a configurable base.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from rdflib import BNode, Graph, Literal, Namespace, URIRef
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
            Col("Definition", "text", SKOS.definition),
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
            Col("Definition", "text", SKOS.definition),
            Col("Unit", "text", VMO.unit, help="e.g. percent, days, USD"),
            _owner(),
            Col("Computed from data products", "ref", VMO.computedFrom, ("Data products",), multi=True),
        ),
        help="Measures of progress. Used to route impact to their owners, not as targets.",
    ),
    Sheet(
        "Capabilities",
        VMO.BusinessCapability,
        (
            _id(),
            _name("capability"),
            Col("Definition", "text", SKOS.definition),
            _owner(),
            Col("Serves objectives", "ref", VMO.serves, ("Objectives",), multi=True),
            Col("Parent capability", "ref", VMO.parentCapability, ("Capabilities",)),
            Col("Realized by processes", "ref", VMO.realizedBy, ("Processes",), multi=True),
            Col("Produces data products", "ref", VMO.produces, ("Data products",), multi=True),
            Col("Consumes data products", "ref", VMO.consumes, ("Data products",), multi=True),
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
            Col("Runs on systems", "ref", VMO.runsOn, ("Systems",), multi=True),
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
            Col("Definition", "text", SKOS.definition),
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
            ),
            Col("Email", "text", VMO.email),
            Col("Roles filled", "ref", VMO.fills, ("Roles",), multi=True),
            Col("Reports to", "ref", ORG.reportsTo, ("People",)),
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
            Col("Actions covered", "ref", VMO.coversAction, ("Actions",), multi=True, required=True),
            Col("Within capability", "ref", VMO.withinCapability, ("Capabilities",)),
            Col("Limit amount", "decimal", VMO.limitAmount),
            Col("Currency", "text", VMO.limitCurrency, help="ISO 4217 code, e.g. USD."),
            Col("Valid from", "date", VMO.validFrom),
            Col("Valid until", "date", VMO.validUntil),
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
            Col("Approver role", "ref", VMO.approverRole, ("Roles",), required=True),
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
            Col("Performs actions", "ref", VMO.performs, ("Actions",), multi=True),
            Col("Consumes data products", "ref", VMO.consumes, ("Data products",), multi=True),
            Col("Runs on systems", "ref", VMO.runsOn, ("Systems",), multi=True),
        ),
        help="AI agents. Each acts for exactly one person.",
    ),
    Sheet(
        "Actions",
        VMO.Action,
        (
            _id(),
            _name("action"),
            Col("Part of process", "ref", VMO.partOfProcess, ("Processes",)),
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
            Col("Action", "ref", VMO.hasImpact, ("Actions",), required=True, inverse=True),
            Col("Impact on", "ref", VMO.impactOn, ("KPIs", "Capabilities"), required=True),
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
            Col("Steward role", "ref", VMO.steward, ("Roles",)),
            Col("Runs on systems", "ref", VMO.runsOn, ("Systems",), multi=True),
            Col("Feeds data products", "ref", VMO.feeds, ("Data products",), multi=True),
            Col("Contains data elements", "ref", VMO.containsElement, ("Data elements",), multi=True),
            Col("Sensitivity", "iri_enum", VMO.sensitivity, values=SENSITIVITY),
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
            Col("CMDB ID", "text", VMO.cmdbId),
            Col("Business owner", "ref", VMO.businessOwner, ("Roles",)),
            Col("Technical owner", "ref", VMO.technicalOwner, ("Roles",)),
            Col("Criticality", "str_enum", VMO.criticality, values=("low", "medium", "high", "critical")),
            Col(
                "Lifecycle",
                "str_enum",
                VMO.lifecycleStatus,
                values=("planned", "active", "retiring", "retired"),
            ),
            Col("Sensitivity", "iri_enum", VMO.sensitivity, values=SENSITIVITY),
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
            Col("Definition", "text", SKOS.definition),
            Col(
                "Critical",
                "bool",
                VMO.criticalDataElement,
                help="yes or no. Critical elements need an owner, steward and authoritative source.",
            ),
            _owner(),
            Col("Steward role", "ref", VMO.steward, ("Roles",)),
            Col("Authoritative source", "ref", VMO.authoritativeSource, ("Systems",)),
            Col("Sensitivity", "iri_enum", VMO.sensitivity, values=SENSITIVITY),
            Col("Data categories", "iri_enum", VMO.dataCategory, multi=True, values=DATA_CATEGORY),
            Col("Retention", "duration", VMO.retention, help="ISO 8601 duration, e.g. P7Y for seven years."),
        ),
        help="Business-level data items. Physical columns in DataHub are bound to these.",
    ),
)
SHEET_BY_NAME = {s.name: s for s in SHEETS}


# --------------------------------------------------------------------------- errors


@dataclass
class RowError:
    sheet: str
    row: int
    column: str
    message: str

    def as_dict(self) -> dict:
        return {"sheet": self.sheet, "row": self.row, "column": self.column, "message": self.message}


@dataclass
class ParseResult:
    graph: Graph
    errors: list[RowError] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    locations: dict[URIRef, tuple[str, int]] = field(default_factory=dict)  # IRI -> (sheet, row)
    ids: dict[URIRef, str] = field(default_factory=dict)  # IRI -> ID as written


# --------------------------------------------------------------------------- IRIs


def iri_for(base: str, local_id: str) -> URIRef:
    return URIRef(base + quote(local_id, safe="._-"))


def local_id(base: str, iri: URIRef) -> str:
    s = str(iri)
    if s.startswith(base):
        return unquote(s[len(base) :])
    return re.split(r"[#/:]", s.rstrip("/#"))[-1]


# --------------------------------------------------------------------------- template / export


HEADER_FILL = PatternFill("solid", fgColor="DDE6F2")
REQUIRED_FONT = Font(bold=True, color="1F3864")
OPTIONAL_FONT = Font(bold=True, color="404040")


def _instructions(ws) -> None:
    lines = [
        ("VeroMesh model workbook", Font(bold=True, size=14)),
        ("Fill in one sheet per kind of thing. Rows reference each other by ID.", None),
        ("", None),
        ("Rules", Font(bold=True)),
        ("- IDs are unique across the whole workbook and use letters, digits, '.', '_' or '-'.", None),
        ("- Columns in dark blue are required. Hover over a header for help.", None),
        ("- Put several IDs in one cell separated by ';'.", None),
        ("- Dates are YYYY-MM-DD. Amounts are plain numbers in the stated currency.", None),
        ("- Ownership always points to a role (Roles sheet), never directly to a person.", None),
        ("", None),
        ("On import, every row is checked against the VeroMesh validation rules.", None),
        ("Errors stop the import; gaps (e.g. a KPI without an owner) are reported as warnings.", None),
        ("", None),
        ("Sheets", Font(bold=True)),
    ]
    for text, font in lines:
        ws.append([text])
        if font:
            ws.cell(ws.max_row, 1).font = font
    for sheet in SHEETS:
        ws.append([sheet.name, sheet.help])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 90


def build_template(rows: dict[str, list[list[str]]] | None = None) -> bytes:
    """Workbook bytes; ``rows`` pre-fills sheets (used by export)."""
    wb = Workbook()
    _instructions(wb.active)
    wb.active.title = "How to use"
    for sheet in SHEETS:
        ws = wb.create_sheet(sheet.name)
        ws.append([c.header for c in sheet.cols])
        for i, c in enumerate(sheet.cols, start=1):
            cell = ws.cell(1, i)
            cell.font = REQUIRED_FONT if c.required else OPTIONAL_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            tip = c.help or ""
            if c.kind == "ref":
                tip = (tip + " " if tip else "") + f"ID from: {', '.join(c.targets)}."
            if c.multi:
                tip += " Several allowed, separated by ';'."
            if tip:
                cell.comment = Comment(tip.strip(), "VeroMesh")
            letter = cell.column_letter
            ws.column_dimensions[letter].width = max(14, min(42, len(c.header) + 6))
            choices: Iterable[str] = ()
            if c.kind in ("iri_enum", "class"):
                choices = c.values.keys()  # type: ignore[union-attr]
            elif c.kind == "str_enum":
                choices = c.values  # type: ignore[assignment]
            elif c.kind == "bool":
                choices = ("yes", "no")
            if choices and not c.multi:
                dv = DataValidation(type="list", formula1='"' + ",".join(choices) + '"', allow_blank=True)
                ws.add_data_validation(dv)
                dv.add(f"{letter}2:{letter}1000")
        ws.freeze_panes = "A2"
        for r in (rows or {}).get(sheet.name, []):
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fmt(value) -> str:
    if isinstance(value, Literal):
        if value.datatype == XSD.boolean:
            return "yes" if value.toPython() else "no"
        return str(value)
    return str(value)


def graph_to_workbook(graph: Graph, base: str) -> bytes:
    """Export: the instances in ``graph`` as a filled-in workbook."""
    rows: dict[str, list[list[str]]] = {}

    def ref_text(node) -> str:
        return local_id(base, node) if isinstance(node, URIRef) else str(node)

    for sheet in SHEETS:
        if sheet.cls is not None:
            subjects = sorted(set(graph.subjects(RDF.type, sheet.cls)), key=str)
        else:  # Policies: class column decides
            subjects = sorted(
                {s for cls in POLICY_TYPES.values() for s in graph.subjects(RDF.type, cls)}, key=str
            )
        out = []
        for s in subjects:
            row = []
            for c in sheet.cols:
                if c.kind == "id":
                    row.append(local_id(base, s))
                    continue
                if c.kind == "class":
                    row.append("Regulation" if (s, RDF.type, VMO.Regulation) in graph else "Policy")
                    continue
                vals = list(graph.subjects(c.prop, s)) if c.inverse else list(graph.objects(s, c.prop))
                if c.kind == "ref":
                    texts = sorted(ref_text(v) for v in vals)
                elif c.kind == "iri_enum":
                    rev = {v: k for k, v in c.values.items()}  # type: ignore[union-attr]
                    texts = sorted(rev.get(v, str(v)) for v in vals)
                else:
                    texts = sorted(_fmt(v) for v in vals)
                row.append("; ".join(texts))
            out.append(row)
        rows[sheet.name] = out
    return build_template(rows)


# --------------------------------------------------------------------------- import


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def workbook_to_graph(data: bytes, base: str) -> ParseResult:
    """Parse a workbook into RDF. Row errors are collected, not raised."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several exception types for bad files
        raise ValueError(f"Not a readable Excel workbook (.xlsx): {exc}") from exc

    result = ParseResult(graph=Graph())
    g = result.graph
    g.bind("vmo", VMO)

    # Pass 1: read rows, register IDs.
    sheet_rows: dict[str, list[tuple[int, dict[str, str]]]] = {}
    id_sheet: dict[str, str] = {}
    for sheet in SHEETS:
        if sheet.name not in wb.sheetnames:
            continue
        ws = wb[sheet.name]
        it = ws.iter_rows(values_only=True)
        header = [_cell_text(h) for h in next(it, ())]
        known = {c.header for c in sheet.cols}
        for h in header:
            if h and h not in known:
                result.errors.append(
                    RowError(sheet.name, 1, h, "Unknown column; check the header against the template.")
                )
        parsed = []
        for rownum, values in enumerate(it, start=2):
            rec = {header[i]: _cell_text(v) for i, v in enumerate(values) if i < len(header) and header[i]}
            if not any(rec.values()):
                continue
            parsed.append((rownum, rec))
            if sheet.keyed:
                rid = rec.get("ID", "")
                if not rid:
                    result.errors.append(RowError(sheet.name, rownum, "ID", "Missing ID."))
                elif not ID_PATTERN.match(rid):
                    result.errors.append(
                        RowError(
                            sheet.name,
                            rownum,
                            "ID",
                            f"'{rid}' is not a valid ID: use letters, digits, '.', '_' or '-'.",
                        )
                    )
                elif rid in id_sheet:
                    result.errors.append(
                        RowError(
                            sheet.name,
                            rownum,
                            "ID",
                            f"ID '{rid}' is already used on the {id_sheet[rid]} sheet.",
                        )
                    )
                else:
                    id_sheet[rid] = sheet.name
        sheet_rows[sheet.name] = parsed

    def resolve(sheet: Sheet, rownum: int, col: Col, text: str) -> URIRef | None:
        where = id_sheet.get(text)
        if where is None:
            result.errors.append(
                RowError(
                    sheet.name,
                    rownum,
                    col.header,
                    f"No row with ID '{text}' (expected one from: {', '.join(col.targets)}).",
                )
            )
            return None
        if where not in col.targets:
            result.errors.append(
                RowError(
                    sheet.name,
                    rownum,
                    col.header,
                    f"'{text}' is on the {where} sheet; expected one from: {', '.join(col.targets)}.",
                )
            )
            return None
        return iri_for(base, text)

    # Pass 2: build triples.
    for sheet in SHEETS:
        rows = sheet_rows.get(sheet.name, [])
        result.counts[sheet.name] = len(rows)
        for n, (rownum, rec) in enumerate(rows, start=1):
            if sheet.keyed:
                rid = rec.get("ID", "")
                if not rid or id_sheet.get(rid) != sheet.name:
                    continue
                subj = iri_for(base, rid)
                result.ids[subj] = rid
            else:
                parent_col = next(c for c in sheet.cols if c.inverse)
                parent = rec.get(parent_col.header, "")
                slug = re.sub(r"[^A-Za-z0-9._-]", "-", sheet.name.lower())
                subj = iri_for(base, f"{parent or 'row'}.{slug}.{n}")
                result.ids[subj] = f"{sheet.name} row {rownum}"
            result.locations[subj] = (sheet.name, rownum)
            if sheet.cls is not None:
                g.add((subj, RDF.type, sheet.cls))
            for col in sheet.cols:
                if col.kind == "id":
                    continue
                raw = rec.get(col.header, "")
                if not raw:
                    if col.required:
                        result.errors.append(
                            RowError(sheet.name, rownum, col.header, "Required value missing.")
                        )
                    continue
                parts = [p.strip() for p in MULTI_SPLIT.split(raw)] if col.multi else [raw]
                for part in filter(None, parts):
                    obj = _convert(sheet, rownum, col, part, resolve, result.errors)
                    if obj is None:
                        continue
                    if col.kind == "class":
                        g.add((subj, RDF.type, obj))
                    elif col.inverse:
                        g.add((obj, col.prop, subj))
                    else:
                        g.add((subj, col.prop, obj))
                    # An impact on a KPI implies the action affects it.
                    if sheet.name == "Impacts" and col.header == "Impact on":
                        action_id = rec.get("Action", "")
                        if action_id and id_sheet.get(action_id) == "Actions":
                            g.add((iri_for(base, action_id), VMO.affects, obj))
    return result


def _convert(sheet: Sheet, rownum: int, col: Col, text: str, resolve, errors: list[RowError]):
    def bad(msg: str):
        errors.append(RowError(sheet.name, rownum, col.header, msg))
        return None

    k = col.kind
    if k in ("label", "text"):
        return Literal(text)
    if k == "ref":
        return resolve(sheet, rownum, col, text)
    if k == "decimal":
        try:
            return Literal(Decimal(text.replace(",", "")), datatype=XSD.decimal)
        except InvalidOperation:
            return bad(f"'{text}' is not a number.")
    if k == "date":
        try:
            return Literal(date.fromisoformat(text[:10]), datatype=XSD.date)
        except ValueError:
            return bad(f"'{text}' is not a date (use YYYY-MM-DD).")
    if k == "bool":
        low = text.lower()
        if low in ("yes", "y", "true", "1"):
            return Literal(True)
        if low in ("no", "n", "false", "0"):
            return Literal(False)
        return bad(f"'{text}' is not yes or no.")
    if k == "duration":
        if not re.fullmatch(r"P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+S)?)?", text):
            return bad(f"'{text}' is not an ISO 8601 duration such as P7Y.")
        return Literal(text, datatype=XSD.duration)
    if k in ("iri_enum", "class"):
        values = col.values  # type: ignore[assignment]
        match = next((v for key, v in values.items() if key.lower() == text.lower()), None)  # type: ignore[union-attr]
        return match if match is not None else bad(f"'{text}' is not one of: {', '.join(values)}.")
    if k == "str_enum":
        if text.lower() in col.values:
            return Literal(text.lower())
        return bad(f"'{text}' is not one of: {', '.join(col.values)}.")
    raise AssertionError(f"unknown column kind {k}")


def blank_free(graph: Graph) -> bool:
    return not any(isinstance(t, BNode) for triple in graph for t in triple)
