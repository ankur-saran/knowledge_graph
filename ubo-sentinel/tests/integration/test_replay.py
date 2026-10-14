"""The commands, end to end: build, screen, review, verify and replay; then break the log."""

import json

import pytest
from e2e import cli, cli_process, decision_id_of, head_of

from ubo_sentinel.audit.log import AuditLog

pytestmark = pytest.mark.integration

CLEAR = "Acme Trading FZE"
ESCALATE = "Halcyon Ridge Minerals Ltd"
HOMONYM = "Silverline Commodities Ltd"


@pytest.fixture
def journey():
    """Build the fixtures with the commands, screen three entities and review one."""
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert cli(*command).exit_code == 0
    ids = []
    for args in ([CLEAR], [ESCALATE], [HOMONYM, "--pick", 2]):
        screened = cli("screen", *args, "--actor", "a.analyst")
        assert screened.exit_code == 0, screened.output
        ids.append(decision_id_of(screened.output))
    reviewed = cli(
        "review", ids[1], "--role", "reviewer", "--actor", "r.reviewer", "--action", "approve"
    )
    assert reviewed.exit_code == 0, reviewed.output
    return ids, head_of(reviewed.output)


def replay(decision_id, *args):
    result = cli("audit", "replay", decision_id, *args)
    assert result.exit_code == 0, result.output
    assert f"Replay identical: decision {decision_id}" in result.output
    return result


def test_screen_review_verify_and_replay(journey):
    ids, receipt = journey
    verified = cli("audit", "verify", "--expect-head", receipt, "--expect-count", 4)
    assert verified.exit_code == 0, verified.output
    assert verified.output.splitlines()[0] == "Chain intact (4 records)"
    for decision_id in ids:
        replay(decision_id)
        replay(decision_id, "--deep", "--role", "reviewer")
    # The reviewed decision replays as it was recommended: a review changes no payload.
    as_json = json.loads(cli("audit", "replay", ids[1], "--json").output)
    assert as_json["verdict"] == "IDENTICAL"


def test_old_decisions_replay_after_newer_data_arrives(journey):
    ids, _ = journey
    assert cli("ingest", "--source", "fixtures", "--path", "fixtures/snapshot_t1").exit_code == 0
    assert cli("normalize").exit_code == 0
    assert cli("build-graph").exit_code == 0
    newer = cli("screen", CLEAR, "--snapshot", "latest")
    assert ": ESCALATE  (" in newer.output
    # The t0 decisions are replayed against the set they were made on.
    for decision_id in ids:
        replay(decision_id)


def test_decisions_replay_after_a_rebuild(journey):
    ids, _ = journey
    assert cli("normalize", "--rebuild").exit_code == 0
    assert cli("build-graph").exit_code == 0
    for decision_id in ids:
        replay(decision_id, "--deep")


@pytest.mark.slow
def test_another_process_replays_what_this_one_recorded(journey):
    ids, _ = journey
    code, output = cli_process("audit", "replay", ids[0])
    assert code == 0, output
    assert f"Replay identical: decision {ids[0]}" in output


def test_every_command_stops_at_a_changed_line(journey):
    ids, _ = journey
    log = AuditLog()
    lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
    # One byte of the second event: the escalated decision now says it is clear.
    lines[1] = lines[1].replace('"recommendation":"ESCALATE"', '"recommendation":"CLEAR!"', 1)
    log.path.write_text("".join(lines), encoding="utf-8")

    for args in (
        ["audit", "verify"],
        ["audit", "replay", ids[0]],
        ["screen", "Vostrek Maritime LLC"],
        ["review", "--role", "reviewer"],
        ["review", ids[2], "--role", "reviewer", "--actor", "r", "--action", "escalate"],
    ):
        result = cli(*args)
        assert result.exit_code == 1, (args, result.output)
        assert "line 2" in result.output, args
    assert log.path.read_text(encoding="utf-8") == "".join(lines)


def test_a_cut_tail_verifies_and_its_receipt_does_not(journey):
    _, receipt = journey
    log = AuditLog()
    lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
    log.path.write_text("".join(lines[:2]), encoding="utf-8")
    assert cli("audit", "verify").exit_code == 0
    cut = cli("audit", "verify", "--expect-head", receipt)
    assert cut.exit_code == 1 and "does not hold the expected head" in cut.output
