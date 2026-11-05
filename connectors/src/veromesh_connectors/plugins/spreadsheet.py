"""Spreadsheet connector: the VeroMesh model workbook (.xlsx).

Source-side layers only: reading the workbook (presentation: openpyxl) and
mapping rows to entities (semantics: columns are the model's fields, via the
shared TabularMapper). Also builds the workbook template and exports, both
generated from the API's schema, so the plugin has no hard-coded model.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from ..core.contract import BatchConnector, Item, Record
from ..core.mapping import TabularMapper

HEADER_FILL = PatternFill("solid", fgColor="DDE6F2")
REQUIRED_FONT = Font(bold=True, color="1F3864")
OPTIONAL_FONT = Font(bold=True, color="404040")


def _columns(t: dict) -> list[dict]:
    return [f for f in t["fields"] if t["keyed"] or f["key"] != "id"]


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


def _cell(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return "; ".join(_cell(v) for v in value)
    return "" if value is None else str(value)


class SpreadsheetConnector(BatchConnector):
    name = "spreadsheet"
    kind = "spreadsheet"

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._labels: dict[tuple[str, str], str] = {}
        self._mapper: TabularMapper | None = None

    def describe(self) -> str:
        return self.path.name

    def fingerprint(self) -> str:
        return self.path.name

    # -- layers 2-3, source side: open and parse the workbook
    def read(self, schema: list[dict]) -> Iterable[Record]:
        self._mapper = TabularMapper(schema)
        try:
            wb = load_workbook(io.BytesIO(self.path.read_bytes()), read_only=True, data_only=True)
        except Exception as exc:  # openpyxl raises several types for unreadable files
            raise ValueError(f"Not a readable Excel workbook (.xlsx): {exc}") from exc
        records = []
        for t in schema:
            for f in t["fields"]:
                self._labels[(t["type"], f["key"])] = f["label"]
            if t["label"] not in wb.sheetnames:
                continue
            rows = wb[t["label"]].iter_rows(values_only=True)
            header = [_text(h) for h in next(rows, ())]
            for h in self._mapper.unknown_columns(t["type"], header):
                self.problems.append(
                    f"{t['label']} row 1: unknown column '{h}'; check the header against the template."
                )
            for rownum, values in enumerate(rows, start=2):
                data = {header[i]: _text(v) for i, v in enumerate(values) if i < len(header) and header[i]}
                if any(data.values()):
                    records.append(Record(ref=f"{t['label']} row {rownum}", data=data, type=t["type"]))
        return records

    # -- layer 7: row -> entity
    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        mapper = self._mapper or TabularMapper(schema)
        return [mapper.map_row(record.type, record)]

    def locate(self, ref: str | None, type_: str, field_key: str | None) -> str:
        label = self._labels.get((type_, field_key or ""))
        where = ref or type_
        return f"{where}, {label}" if label and field_key != "id" else where


def build_workbook(schema: list[dict], entities: dict[str, list[dict]] | None = None) -> bytes:
    """A workbook from the schema, optionally filled with entities grouped by type."""
    labels = {t["type"]: t["label"] for t in schema}
    wb = Workbook()
    ws = wb.active
    ws.title = "How to use"
    for text, bold in [
        ("VeroMesh model workbook", True),
        ("Fill in one sheet per kind of thing. Rows reference each other by ID.", False),
        ("", False),
        ("Rules", True),
        ("- IDs are unique across the whole model and use letters, digits, '.', '_' or '-'.", False),
        ("- Columns in dark blue are required. Hover over a header for help.", False),
        ("- Put several IDs in one cell separated by ';'.", False),
        ("- References may point at entities already in VeroMesh from other sources.", False),
        ("- Dates are YYYY-MM-DD. Amounts are plain numbers in the stated currency.", False),
        ("", False),
        ("Load it with:  veromesh-connect spreadsheet import <file> --source <name>", False),
        ("Every row is validated against the whole model before anything is written.", False),
        ("", False),
        ("Sheets", True),
    ]:
        ws.append([text])
        if bold:
            ws.cell(ws.max_row, 1).font = Font(bold=True, size=14 if ws.max_row == 1 else 11)
    for t in schema:
        ws.append([t["label"], t["help"]])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 90
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
            ws.append([_cell(ent.get(c["key"])) for c in cols])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
