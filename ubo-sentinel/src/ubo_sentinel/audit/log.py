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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from ubo_sentinel.models.audit_event import AuditEvent, EventType
from ubo_sentinel.models.canonical import canonical_json, sha256_hex
from ubo_sentinel.models.decision import Decision, DecisionStatus, FinalOutcome
from ubo_sentinel.models.ontology import OntologyConfig, require_decision_right, require_write

# Commands are run from the repository root.
DEFAULT_VAR_DIR = Path("var")
# Tests and `ubo eval` point this at a temporary directory.
VAR_ENV_VAR = "UBO_VAR"

GENESIS_HASH = hashlib.sha256(b"genesis").hexdigest()

REVIEW_EVENTS = frozenset(get_args(DecisionStatus)) - {"RECOMMENDED"}

# Called inside the lock with the log as it stands; raises to refuse the append.
Guard = Callable[[list[dict[str, Any]]], None]

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


class _Ledger:
    """The status of each decision as the events read so far leave it.

    A chain shows that no event was changed. This shows that the events make
    sense together: one recommendation per decision, carrying the decision it
    names, and at most one review after it.
    """

    def __init__(self) -> None:
        self.status: dict[str, str] = {}

    def problem(self, record: dict[str, Any]) -> str | None:
        """Why the event cannot follow the ones before it, or None after taking it in."""
        decision_id, event_type = record["decision_id"], record["event_type"]
        payload = record["payload"]
        status = self.status.get(decision_id)
        if event_type == "RECOMMENDED":
            if status is not None:
                return "a second recommendation for one decision"
            try:
                decision = Decision.model_validate(payload.get("decision"))
            except ValidationError:
                return "the recommendation does not hold a valid decision"
            if decision.decision_id != decision_id or decision.status != "RECOMMENDED":
                return "the recommendation holds another decision"
            recorded = payload.get("payload_sha256")
            if recorded is not None and recorded != sha256_hex(decision.canonical_payload()):
                return "payload_sha256 is not the hash of the decision"
        elif event_type in REVIEW_EVENTS:
            if status is None:
                return "a review of a decision that was not recommended"
            if status != "RECOMMENDED":
                return f"a review of a decision that is already {status}"
            if payload.get("outcome") not in get_args(FinalOutcome):
                return "a review without an outcome"
            if event_type == "OVERRIDDEN" and not (record["note"] or "").strip():
                return "an override without a reason"
        else:
            return None
        self.status[decision_id] = event_type
        return None


class AuditLog:
    def __init__(self, root: Path | None = None) -> None:
        root = var_dir() if root is None else root
        self.path = root / "audit" / "events.jsonl"
        self.decisions_dir = root / "decisions"

    # --- reading ---------------------------------------------------------------------------

    def records(self) -> list[dict[str, Any]]:
        """Every event in its JSON form, in order. Raises `AuditChainError` on a broken log."""
        return self._checked()[0]

    def verify(self, ontology: OntologyConfig | None = None) -> ChainReport:
        """Check the chain and what the events say. With an ontology, also who reviewed."""
        records, report, _ = self._read()
        if not report.intact or ontology is None:
            return report
        head = GENESIS_HASH
        for number, record in enumerate(records, start=1):
            if record["event_type"] in REVIEW_EVENTS:
                try:
                    require_decision_right(ontology, record["role"], record["event_type"])
                except PermissionError:
                    problem = f"a review by role '{record['role']}', which has no such right"
                    return ChainReport(number - 1, head, number, problem)
            head = record["hash"]
        return report

    def _checked(self) -> tuple[list[dict[str, Any]], _Ledger]:
        records, report, ledger = self._read()
        if not report.intact:
            raise AuditChainError(f"{self.path}, line {report.broken_line}: {report.problem}")
        return records, ledger

    def _read(self) -> tuple[list[dict[str, Any]], ChainReport, _Ledger]:
        records: list[dict[str, Any]] = []
        ledger = _Ledger()
        head = GENESIS_HASH
        if not self.path.exists():
            return records, ChainReport(0, head), ledger
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
                    else:
                        problem = ledger.problem(record)
                if problem:
                    return records, ChainReport(len(records), head, number, problem), ledger
                records.append(record)
                head = record["hash"]
        return records, ChainReport(len(records), head), ledger

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
        guard: Guard | None = None,
    ) -> AuditEvent:
        """Add one event to the end of the chain.

        `guard` is called inside the lock with the events on record, so what it
        checks still holds when the event is written.

        Raises `PermissionError` for a role that may not write or may not
        review, `AuditChainError` when the log as it stands does not verify,
        and `ValueError` for an event that cannot follow the ones on record: a
        recommendation that does not carry its decision, a second review.
        """
        require_write(ontology, role)
        if event_type in REVIEW_EVENTS:
            require_decision_right(ontology, role, event_type)
        # The payload is stored as JSON; anything that is not JSON is refused here.
        payload = json.loads(canonical_json(payload))
        if event_type == "RECOMMENDED":
            recorded = Decision.model_validate(payload.get("decision"))
            if recorded.decision_id != decision_id:
                raise ValueError(
                    f"the event's decision is {recorded.decision_id}, not {decision_id}"
                )
        with self._lock():
            records, ledger = self._checked()
            if guard is not None:
                guard(records)
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
            # An event the log could not be read with is never written.
            problem = ledger.problem(record)
            if problem:
                raise ValueError(f"Decision {decision_id}: {problem}.")
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
                    "final_outcome": record["payload"]["outcome"],
                }
            )
    return decision
