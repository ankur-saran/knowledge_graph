"""`ubo screen` and the pipeline suite of `ubo eval`, run as commands."""

import json

import pytest
from typer.testing import CliRunner

from ubo_sentinel.agents import supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli.app import app
from ubo_sentinel.cli.screen_cmd import EXIT_NEEDS_PICK
from ubo_sentinel.graph.views import MASK
from ubo_sentinel.models.memo import DecisionView

runner = CliRunner()

PII = ["Dmitri Volkanov", "Дмитрий Волканов", "Yusuf Al-Qahdari", "يوسف القحدري"]


def run(*args):
    return runner.invoke(app, [str(arg) for arg in args])


@pytest.fixture
def built():
    """The fixture graph, built by the commands an engineer runs."""
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert run(*command).exit_code == 0


def payload_line(output):
    return next(line for line in output.splitlines() if line.startswith("Payload SHA-256"))


def test_screen_prints_a_recommendation_and_its_memo(built):
    result = run("screen", "Acme Trading FZE", "--snapshot", "fixtures")
    assert result.exit_code == 0, result.output
    assert "CLEAR  (status RECOMMENDED; recorded)" in result.output
    assert "Resolved by: SOLE_NAME" in result.output
    assert "Target: **Acme Trading FZE** (`lei:FXS01TARGET000000000`)" in result.output
    assert "No active designation reaches any of the 3 entities checked." in result.output

    # Run again, however the name is typed: the decision on record, and no new event.
    again = run("screen", "acme trading fze")
    assert "already on record, unchanged" in again.output
    assert payload_line(again.output) == payload_line(result.output)
    log = AuditLog()
    assert (log.verify().intact, log.verify().count) == (True, 1)
    assert log.records()[0]["actor"] == "analyst"


def test_an_ambiguous_query_asks_for_a_pick(built):
    asked = run("screen", "Silverline Commodities Ltd")
    assert asked.exit_code == EXIT_NEEDS_PICK
    assert "has 2 candidates" in asked.output
    assert "1. Silverline Commodities Ltd  [CY]" in asked.output
    assert "2. Silverline Commodities Ltd  [MT]" in asked.output
    assert AuditLog().verify().count == 0

    cyprus = run("screen", "Silverline Commodities Ltd", "--pick", 1)
    malta = run("screen", "Silverline Commodities Ltd", "--pick", 2, "--actor", "j.doe")
    assert ": CLEAR  (" in cyprus.output and ": ESCALATE  (" in malta.output
    assert "Resolved by: ANALYST_PICK" in malta.output
    assert [event["actor"] for event in AuditLog().records()] == ["analyst", "j.doe"]
    assert run("screen", "Silverline Commodities Ltd", "--pick", 3).exit_code == 2

    by_id = run("screen", "--target-id", "lei:FXS24TARGET000000000")
    assert "already on record" in by_id.output and "Resolved by: TARGET_ID" in by_id.output
    by_lei = run("screen", "anything", "--lei", "FXS24TARGET000000000", "--jurisdiction", "MT")
    assert "already on record" in by_lei.output and "Resolved by: IDENTIFIER" in by_lei.output


def test_what_is_not_found_and_what_is_not_seen(built):
    missing = run("screen", "Zzyzx Nonexistent Ventures")
    assert missing.exit_code == 0
    assert ": REVIEW  (" in missing.output and "Reasons: ENTITY_NOT_FOUND" in missing.output
    duplicate = run("screen", "Pendlewick Salt Refiners Ltd")
    assert duplicate.exit_code == 0 and "Reasons: DECISION_RELEVANT_GAP" in duplicate.output
    assert "screen **Pendlewick Salt Refiners Limited** (`fx_list:s27-dup`)" in duplicate.output
    deep = run("screen", "Kittiwake Packaging Ltd", "--max-depth", 7)
    assert ": ESCALATE  (" in deep.output and "Depth: 7" in deep.output


def test_an_analyst_is_shown_no_person_and_a_reviewer_is(built):
    analyst = run("screen", "Halcyon Ridge Minerals Ltd")
    reviewer = run("screen", "Halcyon Ridge Minerals Ltd", "--role", "reviewer")
    assert analyst.exit_code == reviewer.exit_code == 0
    assert not any(name in analyst.output for name in [*PII, "s03-owner-a"])
    assert MASK in analyst.output and "`person:" in analyst.output
    assert PII[0] in reviewer.output and MASK not in reviewer.output
    # One decision, whoever asked.
    assert payload_line(analyst.output) == payload_line(reviewer.output)
    assert "already on record" in reviewer.output

    as_json = run("screen", "Halcyon Ridge Minerals Ltd", "--json")
    view = DecisionView.model_validate(json.loads(as_json.output))
    assert (view.viewer_role, view.masked, view.recommendation) == ("analyst", True, "ESCALATE")
    assert not any(name in as_json.output for name in PII)
    assert all(node.startswith(("person:", "lei:")) for node in view.blocked_set)
    assert payload_line(analyst.output).endswith(view.payload_sha256)
    plain = run("screen", "Halcyon Ridge Minerals Ltd", "--json", "--role", "reviewer")
    assert DecisionView.model_validate(json.loads(plain.output)).masked is False

    # The analyst's own query, when it is a person's name, is not echoed back.
    person = run("screen", "Dmitri Volkanov", "--json")
    assert json.loads(person.output)["query"] == MASK and PII[0] not in person.output


def test_screen_refuses_what_it_cannot_run(built):
    assert run("screen", "Acme Trading FZE", "--role", "auditor").exit_code == 1
    assert run("screen", "Acme Trading FZE", "--role", "engineer").exit_code == 1
    assert run("screen").exit_code == 2
    assert run("screen", "Acme Trading FZE", "--snapshot", "0000000000000000").exit_code == 2
    assert run("screen", "Acme Trading FZE", "--rule-pack", "no_such_pack").exit_code == 2
    unknown = run("screen", "--target-id", "lei:NOSUCHENTITY00000000")
    assert unknown.exit_code == 2 and "No entity" in unknown.output
    assert AuditLog().verify().count == 0


def test_screen_needs_a_built_graph():
    assert run("screen", "Acme Trading FZE").exit_code == 2
    assert run("ingest", "--source", "fixtures").exit_code == 0
    result = run("screen", "Acme Trading FZE")
    assert result.exit_code == 1 and "ubo normalize" in result.output


def test_a_failed_screen_is_not_a_clear(built, monkeypatch):
    def broken(self, state, ctx):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(supervisor.TraversalAgent, "run", broken)
    result = run("screen", "Acme Trading FZE")
    assert result.exit_code == 1
    assert "REVIEW (PIPELINE_ERROR)" in result.output and "not screened" in result.output
    assert "CLEAR" not in result.output
    assert [event["event_type"] for event in AuditLog().records()] == ["SCREEN_FAILED"]


def test_a_broken_log_stops_a_screen(built):
    assert run("screen", "Acme Trading FZE").exit_code == 0
    log = AuditLog()
    log.path.write_text(log.path.read_text(encoding="utf-8").replace("CLEAR", "REVIEW"), "utf-8")
    result = run("screen", "Vostrek Maritime LLC")
    assert result.exit_code == 1 and "line 1" in result.output


def test_eval_runs_every_scenario_from_query_to_decision(built):
    result = run("eval", "--suite", "pipeline")
    assert result.exit_code == 0, result.output
    assert "28/28 scenarios passed" in result.output
    assert "Uncited claims" in result.output and "(target 0)   ok" in result.output
    # The suite records nothing.
    assert AuditLog().verify().count == 0

    everything = run("eval", "--suite", "all")
    assert everything.exit_code == 0
    assert "27/27 scenarios passed" in everything.output
    assert "28/28 scenarios passed" in everything.output
    assert "Not run here" not in everything.output
    assert "Not run here (pipeline suite): SCEN-23" in run("eval", "--suite", "rules").output
