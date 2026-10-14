"""`ubo audit verify`, run as a command."""

import json

import pytest
from e2e import cli as run
from e2e import head_of

from ubo_sentinel.audit.log import GENESIS_HASH, AuditLog


@pytest.fixture
def built():
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert run(*command).exit_code == 0


def test_an_empty_log_is_intact():
    result = run("audit", "verify")
    assert result.exit_code == 0
    assert result.output.splitlines() == ["Chain intact (0 records)", f"Head: {GENESIS_HASH}"]


def test_verify_prints_the_count_the_head_and_the_decisions(built):
    first = run("screen", "Acme Trading FZE", "--actor", "a.analyst")
    receipt = head_of(first.output)
    assert "(#1)" in first.output
    second = run("screen", "Halcyon Ridge Minerals Ltd")
    decision_id = second.output.split()[1].rstrip(":")
    reviewed = run(
        "review", decision_id, "--role", "reviewer", "--actor", "r", "--action", "approve"
    )
    assert reviewed.exit_code == 0, reviewed.output

    result = run("audit", "verify")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Chain intact (3 records)",
        f"Head: {head_of(reviewed.output)}",
        "Decisions: 1 APPROVED, 1 RECOMMENDED",
    ]
    # Every role but the analyst may verify; nobody writes by verifying.
    for role in ("reviewer", "engineer"):
        assert run("audit", "verify", "--role", role).exit_code == 0
    assert run("audit", "verify", "--role", "analyst").exit_code == 1

    # A receipt from an earlier screen is still in the log, which has grown since.
    assert run("audit", "verify", "--expect-head", receipt, "--expect-count", 3).exit_code == 0
    as_json = json.loads(run("audit", "verify", "--json").output)
    assert (as_json["intact"], as_json["count"]) == (True, 3)
    assert as_json["decisions"] == {"APPROVED": 1, "RECOMMENDED": 1}


def test_a_removed_tail_verifies_and_a_receipt_shows_it(built):
    run("screen", "Acme Trading FZE")
    receipt = head_of(run("screen", "Halcyon Ridge Minerals Ltd").output)
    log = AuditLog()
    first_line = log.path.read_text(encoding="utf-8").splitlines(keepends=True)[0]
    log.path.write_text(first_line, encoding="utf-8")

    # The chain of what is left is intact: a chain cannot show what was cut off its end.
    assert run("audit", "verify").exit_code == 0
    cut = run("audit", "verify", "--expect-head", receipt)
    assert cut.exit_code == 1 and "does not hold the expected head" in cut.output
    short = run("audit", "verify", "--expect-count", 2)
    assert short.exit_code == 1 and "1 records where at least 2" in short.output
    assert (
        json.loads(run("audit", "verify", "--expect-count", 2, "--json").output)["intact"] is False
    )


def test_a_changed_event_is_named_by_its_line(built):
    run("screen", "Acme Trading FZE")
    run("screen", "Halcyon Ridge Minerals Ltd")
    log = AuditLog()
    text = log.path.read_text(encoding="utf-8")
    log.path.write_text(text.replace("Halcyon Ridge", "Halcyon Bridge"), encoding="utf-8")
    result = run("audit", "verify")
    assert result.exit_code == 1
    assert "Chain BROKEN (1 records verify): line 2: hash does not match the event" in result.output
