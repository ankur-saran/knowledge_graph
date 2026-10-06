"""The audit log: one hash-chained event per state transition, append-only.

`var/audit/events.jsonl` is the record. A decision is rebuilt from its events;
`var/decisions/<decision_id>.json` is a copy for convenience, never read back.

An event's hash is taken over its JSON form without the `hash` field. That
form holds `prev_hash`, so each event commits to the one before it. The line
written is the canonical JSON of the same form, and verification recomputes
from the line as parsed, so a round trip through the file cannot change a hash.
"""

import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from ubo_sentinel.models.audit_event import AuditEvent, EventType
from ubo_sentinel.models.canonical import canonical_json, sha256_hex
from ubo_sentinel.models.decision import Decision, DecisionStatus
from ubo_sentinel.models.ontology import OntologyConfig, require_write

# Commands are run from the repository root.
DEFAULT_VAR_DIR = Path("var")
# Tests and `ubo eval` point this at a temporary directory.
VAR_ENV_VAR = "UBO_VAR"

GENESIS_HASH = hashlib.sha256(b"genesis").hexdigest()

REVIEW_EVENTS = frozenset(get_args(DecisionStatus)) - {"RECOMMENDED"}

# How long an append waits for another writer, and when a lock is taken to be abandoned.
_LOCK_WAIT_SECONDS = 10.0
_LOCK_STALE_SECONDS = 60.0


class AuditChainError(RuntimeError):
    """The log does not verify. Nothing is appended to a broken chain."""


def var_dir() -> Path:
    return Path(os.environ.get(VAR_ENV_VAR) or DEFAULT_VAR_DIR)


def event_hash(record: dict[str, Any]) -> str:
    """The hash of an event in its JSON form, whether or not it holds one already."""
    return sha256_hex({key: value for key, value in record.items() if key != "hash"})


@dataclass(frozen=True)
class ChainReport:
    """What `verify` found."""

    count: int
    # The hash of the last event, or the genesis hash of an empty log.
    head: str
    # The line of the first event that does not verify (1-based), and why.
    broken_line: int | None = None
    problem: str | None = None

    @property
    def intact(self) -> bool:
        return self.broken_line is None


class AuditLog:
    def __init__(self, root: Path | None = None) -> None:
        root = var_dir() if root is None else root
        self.path = root / "audit" / "events.jsonl"
        self.decisions_dir = root / "decisions"

    # --- reading ---------------------------------------------------------------------------

    def records(self) -> list[dict[str, Any]]:
        """Every event in its JSON form, in order. Raises `AuditChainError` on a broken log."""
        records, report = self._read()
        if not report.intact:
            raise AuditChainError(f"{self.path}, line {report.broken_line}: {report.problem}")
        return records

    def verify(self) -> ChainReport:
        return self._read()[1]

    def _read(self) -> tuple[list[dict[str, Any]], ChainReport]:
        records: list[dict[str, Any]] = []
        head = GENESIS_HASH
        if not self.path.exists():
            return records, ChainReport(0, head)
        with self.path.open(encoding="utf-8") as f:
            for number, line in enumerate(f, start=1):
                problem = None
                try:
                    record = json.loads(line)
                    AuditEvent.model_validate(record)
                except ValueError as exc:
                    problem = f"not a valid event ({type(exc).__name__})"
                else:
                    if record["seq"] != len(records):
                        problem = f"seq {record['seq']} where {len(records)} is expected"
                    elif record["prev_hash"] != head:
                        problem = "prev_hash is not the hash of the event before it"
                    elif record["hash"] != event_hash(record):
                        problem = "hash does not match the event"
                if problem:
                    return records, ChainReport(len(records), head, number, problem)
                records.append(record)
                head = record["hash"]
        return records, ChainReport(len(records), head)

    def events_of(self, decision_id: str) -> list[dict[str, Any]]:
        return [record for record in self.records() if record["decision_id"] == decision_id]

    def decision(self, decision_id: str) -> Decision | None:
        """The decision as its events leave it, or None when none was recorded."""
        return _fold(self.events_of(decision_id))

    def decisions(self) -> list[Decision]:
        """Every recorded decision, in the order each was first recommended."""
        by_id: dict[str, list[dict[str, Any]]] = {}
        for record in self.records():
            by_id.setdefault(record["decision_id"], []).append(record)
        found = (_fold(events) for events in by_id.values())
        return [decision for decision in found if decision is not None]

    # --- writing ---------------------------------------------------------------------------

    def append(
        self,
        *,
        ontology: OntologyConfig,
        decision_id: str,
        event_type: EventType,
        actor: str,
        role: str,
        created_at: datetime,
        payload: dict[str, Any],
        note: str | None = None,
    ) -> AuditEvent:
        """Add one event to the end of the chain.

        Raises `PermissionError` for a read-only role, `AuditChainError` when
        the log as it stands does not verify, and `ValueError` for a
        recommendation that does not carry its decision.
        """
        require_write(ontology, role)
        # The payload is stored as JSON; anything that is not JSON is refused here.
        payload = json.loads(canonical_json(payload))
        if event_type == "RECOMMENDED":
            recorded = Decision.model_validate(payload.get("decision"))
            if recorded.decision_id != decision_id:
                raise ValueError(
                    f"the event's decision is {recorded.decision_id}, not {decision_id}"
                )
        with self._lock():
            records = self.records()
            unhashed = AuditEvent(
                seq=len(records),
                decision_id=decision_id,
                event_type=event_type,
                actor=actor,
                role=role,
                note=note,
                payload=payload,
                created_at=created_at,
                prev_hash=records[-1]["hash"] if records else GENESIS_HASH,
                hash="-",
            ).model_dump(mode="json")
            record = unhashed | {"hash": event_hash(unhashed)}
            event = AuditEvent.model_validate(record)
            with self.path.open("ab") as f:
                f.write(canonical_json(record) + b"\n")
                f.flush()
                os.fsync(f.fileno())
        decision = self.decision(decision_id)
        if decision is not None:
            self._write_view(decision)
        return event

    def _write_view(self, decision: Decision) -> None:
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        path = self.decisions_dir / f"{decision.decision_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(decision.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """One writer at a time: two appends that read the same tail would fork the chain."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > _LOCK_STALE_SECONDS:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise AuditChainError(
                        f"{self.path} is locked by another writer ({lock})."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)


def _fold(events: list[dict[str, Any]]) -> Decision | None:
    """The decision that a decision id's events add up to."""
    decision: Decision | None = None
    for record in events:
        if record["event_type"] == "RECOMMENDED":
            decision = Decision.model_validate(record["payload"]["decision"])
        elif record["event_type"] in REVIEW_EVENTS and decision is not None:
            decision = Decision.model_validate(
                decision.model_dump()
                | {
                    "status": record["event_type"],
                    "reviewed_by": record["actor"],
                    "review_reason": record["note"],
                    "reviewed_at": record["created_at"],
                }
            )
    return decision
