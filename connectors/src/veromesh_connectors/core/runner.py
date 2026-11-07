"""Layer 6, coordination: running a connector against VeroMesh.

Batch run:   read -> map -> merge duplicates -> open changeset (idempotent)
             -> add items in batches -> validate -> commit or discard
Event run:   for each event: map -> one merge changeset keyed by the event ID

* Nothing is sent if the source has structural problems or a record fails to map.
* Each run has a content key (connector, source, mode and the exact items) and
  an idempotency key (content key + the source version it starts from):
  - unchanged input, and the source was last written by that same input:
    nothing to do;
  - a run that crashed after opening its changeset: the re-run resumes it;
  - anything else, including the same input after someone else changed the
    source: a new changeset.
* Commit is refused by the server if the source changed since the run started
  (another load, or a single-entity edit), so concurrent runs never overwrite
  each other.
* Findings come back keyed by entity type and ID and are mapped to source
  locations through the connector's ``locate``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from .contract import BatchConnector, Connector, EventConnector, Item, MappingError
from .session import ApiError, ApiSession

BATCH_SIZE = 500


@dataclass
class RunOptions:
    source: str
    mode: str = "replace"
    dry_run: bool = False


@dataclass
class RunResult:
    ok: bool
    changeset: str | None = None
    report: dict | None = None
    messages: list[str] = field(default_factory=list)


class Runner:
    def __init__(self, session: ApiSession, out: Callable[[str], None] = print) -> None:
        self.api = session
        self.out = out

    # ------------------------------------------------------------------ mapping

    def _map_all(self, connector: Connector, records, schema) -> tuple[list[Item], list[str]]:
        items: dict[tuple[str, str], Item] = {}
        errors: list[str] = []
        for rec in records:
            try:
                produced = connector.map(rec, schema)
            except MappingError as exc:
                errors.append(f"{exc.ref or rec.ref}: {exc}")
                continue
            for it in produced:
                key = (it.type, it.id)
                prior = items.get(key)
                if prior is None:
                    items[key] = it
                elif prior.entity != it.entity:
                    errors.append(
                        f"{it.ref}: {it.type} '{it.id}' is also defined at {prior.ref} with different values."
                    )
        return list(items.values()), errors

    def _content_key(self, connector: Connector, opts: RunOptions, items: list[Item]) -> str:
        body = json.dumps(
            [
                connector.name,
                opts.source,
                opts.mode,
                sorted((i.type, i.op, json.dumps(i.wire(), sort_keys=True)) for i in items),
            ],
            sort_keys=True,
        )
        return f"{connector.name}:{hashlib.sha256(body.encode()).hexdigest()[:32]}"

    # ------------------------------------------------------------------ reporting

    def _print_report(self, connector: Connector, report: dict, index: dict[tuple[str, str], Item]) -> None:
        s = report["summary"]
        self.out(f"Validation: {s['errors']} errors, {s['violations']} violations, {s['warnings']} warnings.")
        for kind in ("errors", "violations", "warnings"):
            for f in report.get(kind, []):
                item = index.get((f.get("type") or "", f.get("id") or ""))
                where = connector.locate(item.ref if item else None, f.get("type") or "model", f.get("field"))
                if item is None and f.get("id"):
                    where = f"{f.get('type') or 'entity'} '{f['id']}'" + (
                        f", field {f['field']}" if f.get("field") else ""
                    )
                self.out(f"  {kind[:-1]:9} {where}: {f['message']}")
        if report.get("preexistingViolations"):
            self.out(
                f"  note: the model already had {report['preexistingViolations']} violations unrelated to this run."
            )

    # ------------------------------------------------------------------ runs

    def _apply(
        self,
        connector: Connector,
        opts: RunOptions,
        items: list[Item],
        *,
        file: str | None,
        key: str | None = None,
    ) -> RunResult:
        index = {(i.type, i.id): i for i in items}
        version = self.api.source_version(opts.source)
        if key is None:
            content = self._content_key(connector, opts, items)
            if version != "none" and not opts.dry_run:
                last = self.api.changeset(version)
                if (last.get("idempotencyKey") or "").rsplit(":v:", 1)[0] == content:
                    self.out(
                        f"Source '{opts.source}' is already up to date with this input (changeset {version})."
                    )
                    return RunResult(True, version, last.get("report"))
            key = f"{content}:v:{version}"
        if opts.dry_run:
            key += ":dry-run"
        cs = self.api.open_changeset(
            source=opts.source,
            mode=opts.mode,
            kind=connector.kind,
            connector=connector.name,
            idempotency_key=key,
            file=file,
        )
        cid = cs["id"]
        if cs["status"] == "committed":
            self.out(f"Already applied by changeset {cid}; nothing to do.")
            return RunResult(True, cid, cs.get("report"))
        if cs.get("reused"):
            self.out(f"Resuming changeset {cid} from an earlier, unfinished run.")
        wire = [i.wire() for i in items]
        for n in range(0, len(wire), BATCH_SIZE):
            self.api.add_items(cid, wire[n : n + BATCH_SIZE])
        report = self.api.validate(cid)
        self._print_report(connector, report, index)
        if opts.dry_run or not report["valid"]:
            self.api.discard(cid)
            self.out(
                "Dry run: nothing written."
                if opts.dry_run and report["valid"]
                else "Not written: fix the errors and violations above."
            )
            return RunResult(report["valid"], cid, report)
        try:
            done = self.api.commit(cid)
        except ApiError as exc:
            if exc.status == 409:
                self.out(f"Not written: {exc.detail}")
                return RunResult(False, cid, exc.body.get("report"))
            raise
        self.out(
            f"Committed changeset {cid}: source '{opts.source}' now has {done.get('facts')} facts ({opts.mode})."
        )
        return RunResult(True, cid, done)

    def run(self, connector: BatchConnector, opts: RunOptions) -> RunResult:
        schema = self.api.schema()
        records = list(connector.read(schema))
        self.out(f"Read {len(records)} records from {connector.describe()}.")
        items, errors = self._map_all(connector, records, schema)
        problems = connector.problems + errors
        if problems:
            self.out(f"Source problems ({len(problems)}), nothing sent:")
            for p in problems:
                self.out(f"  error     {p}")
            return RunResult(False, messages=problems)
        return self._apply(connector, opts, items, file=connector.fingerprint())

    def run_events(self, connector: EventConnector, opts: RunOptions) -> list[RunResult]:
        schema = self.api.schema()
        merge = RunOptions(source=opts.source, mode="merge", dry_run=opts.dry_run)
        results = []
        for event in connector.events(schema):
            items, errors = self._map_all(connector, event.records, schema)
            items += [
                Item(type=t, entity={"id": i}, op="delete", ref=f"event {event.id}") for t, i in event.deletes
            ]
            if errors:
                for e in errors:
                    self.out(f"  error     event {event.id}: {e}")
                results.append(RunResult(False, messages=errors))
                continue
            self.out(f"Event {event.id}: {len(items)} changes.")
            results.append(
                self._apply(connector, merge, items, file=None, key=f"{connector.name}:event:{event.id}")
            )
        return results
