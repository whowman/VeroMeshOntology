"""CSV connector: one file, or a folder of files.

Only the source-side presentation is new here (reading CSV); semantics come
from the shared mappers:

* no mapping file: each file is one entity type, named by the file
  ('kpis.csv', 'Data products.csv') or by --type, with columns named after
  the model's fields (TabularMapper);
* with --mapping: any columns from any system (an HR export, a CMDB extract),
  translated by a mapping file (DeclarativeMapper).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from pathlib import Path

from ..core.contract import BatchConnector, Item, MappingError, Record
from ..core.mapping import DeclarativeMapper, TabularMapper, find_type


class CsvConnector(BatchConnector):
    name = "csv"

    def __init__(self, path: str | Path, *, type_: str | None = None, mapping: dict | None = None) -> None:
        super().__init__()
        self.path = Path(path)
        self.type_name = type_
        self.spec = mapping
        self._tabular: TabularMapper | None = None
        self._declarative: DeclarativeMapper | None = None

    def describe(self) -> str:
        return self.path.name + ("/" if self.path.is_dir() else "")

    def fingerprint(self) -> str:
        return self.path.name

    def _files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(p for p in self.path.iterdir() if p.suffix.lower() == ".csv")
        return [self.path]

    @staticmethod
    def _rows(path: Path) -> tuple[list[str], list[tuple[int, dict]]]:
        text = path.read_text(encoding="utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows = [
            (n, row) for n, row in enumerate(reader, start=2) if any((v or "").strip() for v in row.values())
        ]
        return [h.strip() for h in (reader.fieldnames or [])], rows

    # -- layers 2-3, source side
    def read(self, schema: list[dict]) -> Iterable[Record]:
        if self.spec is not None:
            self._declarative = DeclarativeMapper(self.spec, schema)
        else:
            self._tabular = TabularMapper(schema)
        files = self._files()
        if not files:
            self.problems.append(f"{self.path}: no .csv files found.")
        records = []
        for f in files:
            type_ = None
            headers, rows = self._rows(f)
            if self.spec is None:
                t = find_type(schema, self.type_name or f.stem)
                if t is None:
                    self.problems.append(
                        f"{f.name}: file name does not match an entity type; name it after one "
                        f"({', '.join(x['type'] for x in schema)}) or pass --type."
                    )
                    continue
                type_ = t["type"]
                for h in self._tabular.unknown_columns(type_, headers):
                    self.problems.append(f"{f.name} line 1: unknown column '{h}' for {type_}.")
            records += [
                Record(ref=f"{f.name} line {n}", data={k.strip(): v for k, v in row.items() if k}, type=type_)
                for n, row in rows
            ]
        return records

    # -- layer 7
    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        if self._declarative is not None:
            return self._declarative.map(record)
        if record.type is None:
            raise MappingError("no entity type for this record", record.ref)
        return [self._tabular.map_row(record.type, record)]
