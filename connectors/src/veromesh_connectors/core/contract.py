"""The connector contract: what a connector implements, and nothing more.

A connector owns only the layers that differ per source system:

* its own transport and presentation to the SOURCE (open a file, call an API,
  parse a format) inside ``read`` / ``events``;
* its semantics: ``map`` turns a source record into model entities;
* how to point back into the source: ``locate``.

The SDK core supplies everything else: transport, session and presentation to
VeroMesh, coordination (batching, idempotent resume, version checks) and the
report. See docs/connectors.md for the full seven-layer mapping.

Layer 5 (interaction) is declared by the base class:

* ``BatchConnector``: pull a full snapshot; one changeset per run (replace or merge).
* ``EventConnector``: consume a stream of change events; one merge changeset
  per event, keyed by the event ID so a redelivered event is applied once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Record:
    """One unit read from the source, with a pointer back into it."""

    ref: str  # e.g. "KPIs row 4", "people.csv line 12", "ServiceNow cmdb_ci 8f2c…"
    data: dict
    type: str | None = None  # the model type, when the source already says (a sheet or file per type)


@dataclass
class Item:
    """One change for VeroMesh, produced by ``map``."""

    type: str
    entity: dict
    op: str = "upsert"
    ref: str | None = None  # the record it came from
    # Provenance (docs/provenance.md): when the source stated it, and when it holds.
    observed_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None

    @property
    def id(self) -> str:
        return str(self.entity.get("id", ""))

    def wire(self) -> dict:
        out = {"type": self.type, "op": self.op, "entity": self.entity}
        for key, value in (
            ("observedAt", self.observed_at),
            ("validFrom", self.valid_from),
            ("validUntil", self.valid_until),
        ):
            if value:
                out[key] = value
        return out


@dataclass
class Event:
    """One change notification from an event source."""

    id: str
    records: list[Record] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)  # (type, id)


class MappingError(Exception):
    def __init__(self, message: str, ref: str | None = None) -> None:
        super().__init__(message)
        self.ref = ref


class Connector(ABC):
    #: Short name, recorded on every changeset this connector opens.
    name: str = "connector"
    #: Changeset kind: "spreadsheet" or "connector".
    kind: str = "connector"
    interaction: str = "batch"

    def __init__(self) -> None:
        self.problems: list[str] = []  # structural problems found in the source while reading

    @abstractmethod
    def map(self, record: Record, schema: list[dict]) -> list[Item]:
        """Layer 7: source record -> model entities. Raise MappingError for a bad record."""

    def locate(self, ref: str | None, type_: str, field_key: str | None) -> str:
        """Point a finding back into the source. Override for richer locations (e.g. column names)."""
        where = ref or f"{type_}"
        return f"{where}, field {field_key}" if field_key and field_key != "id" else where

    def describe(self) -> str:
        return self.name


class BatchConnector(Connector):
    interaction = "batch"

    @abstractmethod
    def read(self, schema: list[dict]) -> Iterable[Record]:
        """Layers 2-3 on the source side: fetch and parse a full snapshot."""

    def fingerprint(self) -> str | None:
        """Optional label of the snapshot (e.g. a file name), recorded on the changeset."""
        return None


class EventConnector(Connector):
    interaction = "event"

    @abstractmethod
    def events(self, schema: list[dict]) -> Iterable[Event]:
        """Layers 2-3 on the source side: yield change events as they arrive."""
