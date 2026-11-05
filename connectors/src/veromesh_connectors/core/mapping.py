"""Layer 7, semantics: turning source records into model entities.

Two reusable mappers:

* ``TabularMapper``: rows whose columns ARE the model's fields (by label or
  key), e.g. the VeroMesh workbook or CSV files exported from it.
* ``DeclarativeMapper``: rows from any other system, translated by a mapping
  file. Templates build values from source columns, value maps translate
  vocabularies, and one source row can produce several entities:

      rules:
        - type: people
          id: "{Worker ID}"
          fields:
            name: "{First Name} {Last Name}"
            employeeId: "{Worker ID}"
            email: "{Work Email}"
            rolesFilled: "{Position ID}"
            reportsTo: "{Manager ID}"
        - type: roles
          id: "{Position ID}"
          fields:
            name: "{Job Title}"
      values:
        systems.criticality: {"Tier 1": critical, "Tier 2": high}
"""

from __future__ import annotations

import re
from typing import Any

from .contract import Item, MappingError, Record

MULTI_SPLIT = re.compile(r"[;\n]")
TEMPLATE_FIELD = re.compile(r"\{([^{}]+)\}")
ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def schema_index(schema: list[dict]) -> dict[str, dict]:
    return {t["type"]: t for t in schema}


def find_type(schema: list[dict], name: str) -> dict | None:
    """Match a type by REST name ('data-products') or label ('Data products')."""
    low = name.strip().lower()
    return next((t for t in schema if t["type"] == low or t["label"].lower() == low), None)


def split_multi(raw: str) -> list[str]:
    return [p.strip() for p in MULTI_SPLIT.split(raw) if p.strip()]


class TabularMapper:
    """Columns named after the model's fields (label or key, any case)."""

    def __init__(self, schema: list[dict]) -> None:
        self.types = schema_index(schema)

    def columns(self, type_: str) -> dict[str, dict]:
        """Column header (lower-cased label or key) -> field."""
        out = {}
        for f in self.types[type_]["fields"]:
            out[f["label"].lower()] = f
            out[f["key"].lower()] = f
        return out

    def unknown_columns(self, type_: str, headers: list[str]) -> list[str]:
        cols = self.columns(type_)
        return [h for h in headers if h and h.lower() not in cols]

    def map_row(self, type_: str, record: Record) -> Item:
        t = self.types[type_]
        cols = self.columns(type_)
        entity: dict[str, Any] = {}
        for header, raw in record.data.items():
            f = cols.get(str(header).lower())
            if f is None or raw in (None, ""):
                continue
            text = str(raw).strip()
            if not text:
                continue
            entity[f["key"]] = split_multi(text) if f["multi"] else text
        if t["keyed"]:
            if not entity.get("id"):
                raise MappingError("missing ID", record.ref)
        else:
            parent = next((f["key"] for f in t["fields"] if f["kind"] == "ref"), None)
            entity["id"] = (
                f"{entity.get(parent, 'row') if parent else 'row'}.{type_}.{ID_SAFE.sub('-', record.ref)}"
            )
        return Item(type=type_, entity=entity, ref=record.ref)


class DeclarativeMapper:
    """Mapping-file driven translation from any source vocabulary."""

    def __init__(self, spec: dict, schema: list[dict]) -> None:
        self.types = schema_index(schema)
        self.rules = spec.get("rules") or []
        self.values: dict[str, dict] = spec.get("values") or {}
        if not self.rules:
            raise ValueError("Mapping file has no 'rules'.")
        for n, rule in enumerate(self.rules, start=1):
            t = self.types.get(rule.get("type", ""))
            if t is None:
                raise ValueError(
                    f"Mapping rule {n}: unknown type '{rule.get('type')}'. Known: {', '.join(self.types)}."
                )
            if t["keyed"] and not rule.get("id"):
                raise ValueError(f"Mapping rule {n} ({t['type']}): needs an 'id' template.")
            known = {f["key"] for f in t["fields"]}
            for key in rule.get("fields") or {}:
                if key not in known:
                    raise ValueError(
                        f"Mapping rule {n} ({t['type']}): unknown field '{key}'. Known: {', '.join(sorted(known))}."
                    )

    @staticmethod
    def _render(template: str, data: dict, ref: str) -> str:
        def sub(m: re.Match) -> str:
            col = m.group(1)
            if col not in data:
                raise MappingError(f"column '{col}' not found in the source", ref)
            v = data[col]
            return "" if v is None else str(v).strip()

        return TEMPLATE_FIELD.sub(sub, str(template)).strip()

    def map(self, record: Record) -> list[Item]:
        items = []
        for rule in self.rules:
            t = self.types[rule["type"]]
            fields = {f["key"]: f for f in t["fields"]}
            entity: dict[str, Any] = {}
            if rule.get("id"):
                rid = self._render(rule["id"], record.data, record.ref)
                if not rid:
                    if rule.get("optional"):
                        continue
                    raise MappingError(
                        f"{t['type']}: id template '{rule['id']}' is empty for this record", record.ref
                    )
                entity["id"] = ID_SAFE.sub("-", rid)
            for key, template in (rule.get("fields") or {}).items():
                text = self._render(template, record.data, record.ref)
                if not text:
                    continue
                vmap = self.values.get(f"{t['type']}.{key}") or self.values.get(key)
                f = fields[key]
                parts = split_multi(text) if f["multi"] else [text]
                if vmap:
                    parts = [str(vmap.get(p, p)) for p in parts]
                if f["kind"] == "ref":
                    parts = [ID_SAFE.sub("-", p) for p in parts]
                entity[key] = parts if f["multi"] else parts[0]
            if not t["keyed"]:
                parent = next((f["key"] for f in t["fields"] if f["kind"] == "ref"), None)
                entity.setdefault(
                    "id", f"{entity.get(parent, 'row')}.{t['type']}.{ID_SAFE.sub('-', record.ref)}"
                )
            items.append(Item(type=t["type"], entity=entity, ref=record.ref))
        return items
