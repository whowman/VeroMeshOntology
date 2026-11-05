"""Workbook <-> API items. Everything about the model comes from the API's schema.

The loader has no knowledge of entity types: sheets, columns, help notes and
drop-downs are all generated from ``GET /api/v1/schema``, so a new field in the
model appears in the next template without a loader release.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

MULTI_SPLIT = re.compile(r"[;\n]")
HEADER_FILL = PatternFill("solid", fgColor="DDE6F2")
REQUIRED_FONT = Font(bold=True, color="1F3864")
OPTIONAL_FONT = Font(bold=True, color="404040")


def _columns(t: dict) -> list[dict]:
    """Workbook columns for a type: types without their own ID get no ID column."""
    return [f for f in t["fields"] if t["keyed"] or f["key"] != "id"]


def _labels(schema: list[dict]) -> dict[str, str]:
    return {t["type"]: t["label"] for t in schema}


def _instructions(ws, schema: list[dict]) -> None:
    rows = [
        ("VeroMesh model workbook", Font(bold=True, size=14)),
        ("Fill in one sheet per kind of thing. Rows reference each other by ID.", None),
        ("", None),
        ("Rules", Font(bold=True)),
        ("- IDs are unique across the whole model and use letters, digits, '.', '_' or '-'.", None),
        ("- Columns in dark blue are required. Hover over a header for help.", None),
        ("- Put several IDs in one cell separated by ';'.", None),
        ("- References may point at entities already in VeroMesh from other sources.", None),
        ("- Dates are YYYY-MM-DD. Amounts are plain numbers in the stated currency.", None),
        ("", None),
        ("Load it with:  veromesh-loader import <file> --source <name>", None),
        ("Every row is validated against the whole model before anything is written.", None),
        ("", None),
        ("Sheets", Font(bold=True)),
    ]
    for text, font in rows:
        ws.append([text])
        if font:
            ws.cell(ws.max_row, 1).font = font
    for t in schema:
        ws.append([t["label"], t["help"]])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 90


def _cell_value(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return "; ".join(_cell_value(v) for v in value)
    return "" if value is None else str(value)


def build(schema: list[dict], entities: dict[str, list[dict]] | None = None) -> bytes:
    """A workbook from the schema, optionally filled with entities grouped by type."""
    labels = _labels(schema)
    wb = Workbook()
    wb.active.title = "How to use"
    _instructions(wb.active, schema)
    for t in schema:
        ws = wb.create_sheet(t["label"])
        cols = _columns(t)
        ws.append([c["label"] for c in cols])
        for i, c in enumerate(cols, start=1):
            cell = ws.cell(1, i)
            cell.font = REQUIRED_FONT if c["required"] else OPTIONAL_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            tip = c.get("help") or ""
            if c.get("targets"):
                tip += f" ID from: {', '.join(labels[x] for x in c['targets'])}."
            if c.get("multi"):
                tip += " Several allowed, separated by ';'."
            if tip.strip():
                cell.comment = Comment(tip.strip(), "VeroMesh")
            letter = cell.column_letter
            ws.column_dimensions[letter].width = max(14, min(42, len(c["label"]) + 6))
            if c.get("values") and not c.get("multi"):
                dv = DataValidation(type="list", formula1='"' + ",".join(c["values"]) + '"', allow_blank=True)
                ws.add_data_validation(dv)
                dv.add(f"{letter}2:{letter}1000")
        ws.freeze_panes = "A2"
        for ent in (entities or {}).get(t["type"], []):
            ws.append([_cell_value(ent.get(c["key"])) for c in cols])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@dataclass
class ReadResult:
    items: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # (type, id) -> (sheet, row), and (type, field key) -> column label, for reporting
    rows: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    columns: dict[tuple[str, str], str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read(data: bytes, schema: list[dict]) -> ReadResult:
    """Workbook -> changeset items. Only structure is checked here; the API validates content."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several types for unreadable files
        raise ValueError(f"Not a readable Excel workbook (.xlsx): {exc}") from exc
    out = ReadResult()
    for t in schema:
        if t["label"] not in wb.sheetnames:
            continue
        cols = {c["label"]: c for c in _columns(t)}
        for c in cols.values():
            out.columns[(t["type"], c["key"])] = c["label"]
        parent = next((c for c in t["fields"] if c.get("kind") == "ref" and not t["keyed"]), None)
        rows = wb[t["label"]].iter_rows(values_only=True)
        header = [_text(h) for h in next(rows, ())]
        for h in header:
            if h and h not in cols:
                out.errors.append(
                    f"{t['label']} row 1: unknown column '{h}'; check the header against the template."
                )
        n = 0
        for rownum, values in enumerate(rows, start=2):
            rec = {header[i]: _text(v) for i, v in enumerate(values) if i < len(header) and header[i] in cols}
            if not any(rec.values()):
                continue
            n += 1
            entity: dict = {}
            for label, raw in rec.items():
                if not raw:
                    continue
                c = cols[label]
                entity[c["key"]] = (
                    [p.strip() for p in MULTI_SPLIT.split(raw) if p.strip()] if c["multi"] else raw
                )
            if t["keyed"]:
                rid = entity.get("id", "")
                if not rid:
                    out.errors.append(f"{t['label']} row {rownum}: missing ID.")
                    continue
            else:
                rid = f"{entity.get(parent['key'], 'row') if parent else 'row'}.{t['type']}.{rownum}"
                entity["id"] = rid
            if (t["type"], rid) in out.rows:
                sheet, first = out.rows[(t["type"], rid)]
                out.errors.append(f"{t['label']} row {rownum}: ID '{rid}' is already used on row {first}.")
                continue
            out.rows[(t["type"], rid)] = (t["label"], rownum)
            out.items.append({"type": t["type"], "op": "upsert", "entity": entity})
        out.counts[t["label"]] = n
    return out
