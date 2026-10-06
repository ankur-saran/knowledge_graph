"""The audit log: an append-only hash chain, and the decisions rebuilt from it."""

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_models import make_decision

from ubo_sentinel.audit import log as audit_log
from ubo_sentinel.audit.log import GENESIS_HASH, AuditChainError, AuditLog, event_hash, var_dir
from ubo_sentinel.models import Decision

AT = datetime(2026, 10, 6, 9, 30, 15, 123456, tzinfo=UTC)


@pytest.fixture
def log():
    return AuditLog()


@pytest.fixture
def append(log, ontology):
    def add(decision=None, event_type="RECOMMENDED", role="analyst", **fields):
        decision = decision or make_decision()
        payload = {"decision": decision.model_dump(mode="json")}
        fields = {"actor": role, "created_at": AT, "payload": payload} | fields
        return log.append(
            ontology=ontology,
            decision_id=decision.decision_id,
            event_type=event_type,
            role=role,
            **fields,
        )

    return add


def lines(log):
    return log.path.read_text(encoding="utf-8").splitlines()


def test_the_log_lives_under_var_and_starts_empty(log, tmp_path):
    assert var_dir() == tmp_path / "var"
    assert log.path == tmp_path / "var" / "audit" / "events.jsonl"
    report = log.verify()
    assert (report.intact, report.count, report.head) == (True, 0, GENESIS_HASH)
    assert log.records() == [] and log.decision("nothing") is None


def test_events_chain_from_the_genesis_hash(log, append):
    first = append()
    second = append(make_decision(target_id="asset-2"))
    assert (first.seq, first.prev_hash) == (0, GENESIS_HASH)
    assert (second.seq, second.prev_hash) == (1, first.hash)
    report = log.verify()
    assert (report.intact, report.count, report.head) == (True, 2, second.hash)
    # A line is the event in canonical JSON: sorted keys, no whitespace, LF.
    assert log.path.read_bytes().count(b"\n") == 2 and b"\r" not in log.path.read_bytes()
    record = json.loads(lines(log)[0])
    assert list(record) == sorted(record)
    assert record["hash"] == event_hash(record) == first.hash


def test_a_hash_survives_the_round_trip_through_the_file(log, append):
    """A time zone, microseconds and a Decimal are written in one form and hashed in it."""
    paris = timezone(timedelta(hours=2))
    append(created_at=datetime(2026, 10, 6, 11, 30, 15, 123456, tzinfo=paris))
    odd = {"pct": Decimal("49.99990"), "when": AT, "nested": [{"n": 0.1}]}
    append(event_type="SCREEN_FAILED", payload=odd)
    assert log.verify().intact
    first, second = log.records()
    assert first["created_at"] == "2026-10-06T09:30:15.123456Z"
    assert second["payload"] == {
        "pct": "49.9999",
        "when": AT.isoformat(),
        "nested": [{"n": 0.1}],
    }
    with pytest.raises(TypeError):
        append(payload={"x": object()})
    # A recommendation is its decision: an event without one, or with another's, is refused.
    with pytest.raises(ValueError):
        append(payload={})
    other = make_decision(target_id="asset-2")
    with pytest.raises(ValueError, match="decision"):
        append(payload={"decision": other.model_dump(mode="json")})
    assert log.verify().count == 2


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [
        (lambda record: record["payload"].update(x=1), "hash does not match"),
        (lambda record: record.update(actor="someone else"), "hash does not match"),
        (lambda record: record.update(prev_hash="0" * 64), "prev_hash"),
        (lambda record: record.update(seq=7), "seq 7"),
        (lambda record: record.update(event_type="DELETED"), "not a valid event"),
    ],
)
def test_a_changed_event_breaks_the_chain(log, append, tamper, problem):
    for target in ("asset-1", "asset-2", "asset-3"):
        append(make_decision(target_id=target))
    records = [json.loads(line) for line in lines(log)]
    tamper(records[1])
    log.path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    report = log.verify()
    assert (report.intact, report.broken_line, report.count) == (False, 2, 1)
    assert problem in report.problem
    # Nothing is read from, or added to, a log that does not verify.
    with pytest.raises(AuditChainError, match="line 2"):
        log.records()
    with pytest.raises(AuditChainError):
        append(make_decision(target_id="asset-4"))
    assert len(lines(log)) == 3


def test_a_removed_or_torn_line_breaks_the_chain(log, append):
    for target in ("asset-1", "asset-2", "asset-3"):
        append(make_decision(target_id=target))
    whole = lines(log)
    log.path.write_text("\n".join([whole[0], whole[2]]) + "\n", encoding="utf-8")
    assert log.verify().broken_line == 2
    log.path.write_text("\n".join([*whole, whole[0][:40]]) + "\n", encoding="utf-8")
    report = log.verify()
    assert (report.broken_line, report.count) == (4, 3)


def test_only_a_role_that_may_write_appends(log, append):
    with pytest.raises(PermissionError, match="read-only"):
        append(role="auditor")
    with pytest.raises(PermissionError, match="Unknown role"):
        append(role="intern")
    assert not log.path.exists()


def test_two_writers_keep_one_chain(append, ontology):
    append()
    other = AuditLog()
    decision = make_decision(target_id="asset-2")
    other.append(
        ontology=ontology,
        decision_id=decision.decision_id,
        event_type="RECOMMENDED",
        actor="reviewer",
        role="reviewer",
        created_at=AT,
        payload={"decision": decision.model_dump(mode="json")},
    )
    third = append(make_decision(target_id="asset-3"))
    assert (third.seq, other.verify().count) == (2, 3)


def test_an_append_waits_for_the_lock_and_gives_up(log, append, monkeypatch):
    log.path.parent.mkdir(parents=True)
    lock = log.path.with_suffix(".lock")
    lock.touch()
    monkeypatch.setattr(audit_log, "_LOCK_WAIT_SECONDS", 0.1)
    with pytest.raises(AuditChainError, match="locked"):
        append()
    # A lock nobody released is taken over once it is old.
    monkeypatch.setattr(audit_log, "_LOCK_STALE_SECONDS", -1.0)
    append()
    assert not lock.exists() and log.verify().count == 1


def test_a_decision_is_rebuilt_from_its_events(log, append):
    decision = make_decision()
    append(decision)
    assert log.decision(decision.decision_id) == decision
    view = log.decisions_dir / f"{decision.decision_id}.json"
    assert Decision.model_validate_json(view.read_text(encoding="utf-8")) == decision

    reviewed_at = AT + timedelta(hours=2)
    append(decision, "OVERRIDDEN", "reviewer", note="Ownership sold in August.", **{
        "created_at": reviewed_at, "payload": {}
    })  # fmt: skip
    reviewed = log.decision(decision.decision_id)
    assert (
        reviewed.status,
        reviewed.reviewed_by,
        reviewed.review_reason,
        reviewed.reviewed_at,
    ) == (
        "OVERRIDDEN",
        "reviewer",
        "Ownership sold in August.",
        reviewed_at,
    )
    # A review changes who decided, not what was recommended.
    assert reviewed.canonical_payload() == decision.canonical_payload()
    assert Decision.model_validate_json(view.read_text(encoding="utf-8")) == reviewed
    assert log.decisions() == [reviewed]


def test_a_failed_screen_is_an_event_and_not_a_decision(log, append):
    decision = make_decision()
    append(decision, "SCREEN_FAILED", payload={"agent": "Traversal"}, note="RuntimeError: x")
    assert log.verify().count == 1
    assert log.decision(decision.decision_id) is None and log.decisions() == []
    assert not log.decisions_dir.exists()
