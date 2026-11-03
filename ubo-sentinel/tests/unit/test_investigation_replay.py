"""Recording and replaying an investigation (Step 14.6): one event, and the run made again
with its stored turns in the model's place. No network, no key."""

from types import SimpleNamespace

import pytest
from e2e import cli
from llm_fakes import ScriptedClient, call, say

from ubo_sentinel.agents import investigator
from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.investigator import record, run_investigation
from ubo_sentinel.agents.supervisor import Supervisor, utc_now
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.views import MASK
from ubo_sentinel.llm import replay as llm_replay
from ubo_sentinel.llm.client import LLMUnavailable
from ubo_sentinel.llm.config import InvestigateBudget
from ubo_sentinel.llm.replay import UnknownRecord, replay_investigation

TARGET = "lei:FXI03TARGET000000000"
I01_TARGET = "lei:FXI01TARGET000000000"
QUESTION = "Who else does this owner control?"
BUDGET = InvestigateBudget("claude-opus-5-5", 12, 200_000, 90.0)
LOOK = call("get_entity", entity_id=TARGET)
ADDRESSES = call("get_entities_sharing_address", entity_id=TARGET)
FINDING = {
    "code": "COMMON_CONTROLLER",
    "subjects": [TARGET],
    "text": "{e:" + TARGET + "} is held at {v:c2./truncated}.",
    "cites": ["c2"],
}


def submit():
    return call("submit_findings", findings=[FINDING], suggested_actions=[])


@pytest.fixture
def made(fresh_con, ontology):
    """(script, ...) -> (investigation, log, connection), recorded."""
    con, (_, inv_set) = fresh_con("t0_inv")
    log = AuditLog()

    def make(script=(), budget=BUDGET, question=QUESTION, target=TARGET, **options):
        request = ScreenRequest(target_id=target, snapshot=inv_set)
        decision = Supervisor(ontology, log).run(request, con).decision
        client = ScriptedClient(list(script), **options) if script else None
        found, new = run_investigation(
            log, ontology, decision.decision_id, question, "ana", "reviewer", utc_now(), con,
            client, budget if client else None,
        )  # fmt: skip
        assert new
        return found, log, con

    return make


def replay(ontology, found, log, con, **options):
    return replay_investigation(log, ontology, found.investigation_id, con, **options)


# --- the event --------------------------------------------------------------------------------


def test_an_investigation_is_one_event_about_its_decision(made, ontology):
    found, log, _ = made([[LOOK], [submit()]])
    (event,) = [r for r in log.records() if r["event_type"] == "INVESTIGATED"]
    assert event["decision_id"] == f"investigation:{found.investigation_id}"
    assert event["payload"]["decision_id"] == found.decision_id
    assert (event["actor"], event["role"]) == ("ana", "reviewer")
    assert log.verify(ontology).intact


def test_the_same_run_is_recorded_once(made, ontology):
    found, log, _ = made()
    assert not record(log, ontology, found, "ana", "reviewer", utc_now())


def test_it_leaves_the_decision_as_it_was(made):
    found, log, _ = made([[submit()]])
    decision = log.decision(found.decision_id)
    assert decision.status == "RECOMMENDED" and decision.llm_annotations == {}


def test_a_role_that_cannot_write_is_refused_before_a_model_is_asked(made, ontology):
    found, log, con = made()
    client = ScriptedClient([])
    with pytest.raises(PermissionError):
        run_investigation(
            log, ontology, found.decision_id, QUESTION, "aud", "auditor", utc_now(), con,
            client, BUDGET,
        )  # fmt: skip
    assert client.requests == []


def test_a_decision_not_on_record_is_refused(made, ontology):
    _, log, con = made()
    with pytest.raises(LookupError, match="No decision"):
        run_investigation(log, ontology, "nope", QUESTION, "ana", "reviewer", utc_now(), con)


def test_a_failure_in_the_loop_records_what_was_found(made):
    def crash(_):
        raise RuntimeError("a value that must not be kept")

    found, log, _ = made([[LOOK], crash])
    assert found.faults == ["interrupted: RuntimeError"] and not found.complete
    assert found.first_pass and len(found.calls) > 1
    assert log.records()[-1]["event_type"] == "INVESTIGATED"


def test_an_interrupted_run_is_recorded_too(made):
    def stop(_):
        raise KeyboardInterrupt

    found, _, _ = made([stop])
    assert found.faults == ["interrupted: KeyboardInterrupt"]


# --- replay -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "options"),
    [
        ((), {}),
        ([[LOOK], [ADDRESSES], [submit()]], {}),
        ([[say("Done.")], [submit()]], {}),
        ([[LOOK], [ADDRESSES], [submit()]], {"budget": InvestigateBudget("m", 2, 200_000, 90.0)}),
        ([[LOOK], [submit()]], {"seconds": 100.0}),
        ([[LOOK], [LOOK]], {"budget": InvestigateBudget("m", 1, 200_000, 90.0)}),
        ([[LOOK], [submit()]], {"fail_at": 2}),
        ([[call("get_entity", entity_id=I01_TARGET)], [submit()]], {}),
    ],
    ids=[
        "no model",
        "a model",
        "asked once to submit",
        "steps spent",
        "time spent",
        "no submission",
        "a model that failed",
        "a refused call",
    ],
)
def test_a_recorded_run_replays_identically(made, ontology, script, options):
    found, log, con = made(script, **options)
    report = replay(ontology, found, log, con)
    assert report.verdict == "IDENTICAL", report.detail
    assert report.kind == "investigation" and report.feature == "investigate"
    assert report.recorded_status == ("COMPLETE" if found.complete else "INCOMPLETE")


def test_a_replay_does_not_ask_a_model_or_write(made, ontology, monkeypatch):
    found, log, con = made([[LOOK], [submit()]])
    before = len(log.records())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert replay(ontology, found, log, con, deep=True).identical
    assert len(log.records()) == before


def test_a_masked_question_replays_with_the_mask(made, ontology):
    found, log, con = made([[submit()]], question="Who is Casimir Ondrejka?")
    assert found.question == MASK
    assert replay(ontology, found, log, con).identical


def test_an_unknown_investigation_is_said(made, ontology):
    _, log, con = made()
    with pytest.raises(UnknownRecord):
        replay_investigation(log, ontology, "nope", con)


def test_another_prompt_is_not_replayable(made, ontology, monkeypatch):
    found, log, con = made()
    monkeypatch.setattr(investigator, "PROMPT_VERSION", 2)
    report = replay(ontology, found, log, con)
    assert report.verdict == "NOT_REPLAYABLE" and "prompt version" in report.detail


def test_another_tool_list_is_not_replayable(made, ontology, monkeypatch):
    found, log, con = made()
    monkeypatch.setattr(investigator, "SYSTEM", "Another prompt.")
    assert replay(ontology, found, log, con).verdict == "NOT_REPLAYABLE"


def test_an_interrupted_run_is_not_replayable(made, ontology):
    def crash(_):
        raise RuntimeError("stopped")

    found, log, con = made([crash])
    report = replay(ontology, found, log, con)
    assert report.verdict == "NOT_REPLAYABLE" and "interrupted" in report.detail


def test_a_changed_graph_is_named_before_any_tool_runs(made, ontology):
    found, log, con = made()
    con.execute("UPDATE gold_builds SET gold_digest = 'moved'")
    report = replay(ontology, found, log, con)
    assert report.verdict == "GRAPH_CHANGED" and "GRAPH_CHANGED" in report.detail


def test_a_context_row_changed_behind_its_digest_shows_as_a_tool_result(made, ontology):
    found, log, con = made(target=I01_TARGET)
    con.execute("UPDATE ctx_addresses SET address = 'Elsewhere' WHERE entity_id = ?", [I01_TARGET])
    report = replay(ontology, found, log, con)
    assert report.verdict == "TOOL_RESULT_DIFFERS" and "c1" in report.detail
    assert "Elsewhere" not in report.detail


def test_a_deep_replay_finds_it_in_the_rows(made, ontology):
    found, log, con = made(target=I01_TARGET)
    con.execute("UPDATE ctx_addresses SET address = 'Elsewhere' WHERE entity_id = ?", [I01_TARGET])
    assert replay(ontology, found, log, con, deep=True).verdict == "CONTEXT_CHANGED"


def test_a_rebuilt_context_layer_is_named(made, ontology):
    found, log, con = made()
    con.execute("UPDATE ctx_builds SET context_digest = 'moved'")
    assert replay(ontology, found, log, con).verdict == "CONTEXT_CHANGED"


def test_a_context_layer_that_is_gone_is_named(made, ontology):
    found, log, con = made()
    con.execute("DELETE FROM ctx_builds")
    assert replay(ontology, found, log, con).verdict == "CONTEXT_CHANGED"


def test_a_tool_that_answers_differently_is_named_with_its_call(made, ontology, monkeypatch):
    found, log, con = made([[LOOK], [submit()]])
    original = investigator.ToolBox._execute

    def another(self, call_id, name, arguments):
        answer = original(self, call_id, name, arguments)
        return answer.model_copy(update={"truncated": 9}) if name == "get_entity" else answer

    monkeypatch.setattr(investigator.ToolBox, "_execute", another)
    report = replay(ontology, found, log, con)
    assert report.verdict == "TOOL_RESULT_DIFFERS" and "get_entity" in report.detail


def test_a_changed_request_is_named_with_its_turn(made, ontology, monkeypatch):
    found, log, con = made([[LOOK], [submit()]])
    original = investigator._user_text
    monkeypatch.setattr(
        investigator, "_user_text", lambda *args: original(*args) + " And another line."
    )
    report = replay(ontology, found, log, con)
    assert report.verdict == "INPUT_DIFFERS" and "turn 1" in report.detail


def test_a_changed_guardrail_is_named(made, ontology, monkeypatch):
    found, log, con = made([[LOOK], [submit()]])
    assert found.findings
    original = investigator.check_findings

    def stricter(findings, calls, decision):
        """Drops the model's finding only: the first pass, and so each request, is unchanged."""
        kept, faults = original(findings, calls, decision)
        if any(finding.text == FINDING["text"] for finding in kept):
            return [], [*faults, "a new fault"]
        return kept, faults

    monkeypatch.setattr(investigator, "check_findings", stricter)
    assert replay(ontology, found, log, con).verdict == "GUARDRAIL_DIFFERS"


def test_a_guardrail_that_changes_the_first_pass_changes_what_was_sent(made, ontology, monkeypatch):
    found, log, con = made([[LOOK], [submit()]])
    monkeypatch.setattr(investigator, "check_findings", lambda f, c, d: ([], []))
    report = replay(ontology, found, log, con)
    assert report.verdict == "INPUT_DIFFERS" and "turn 1" in report.detail


# --- the command --------------------------------------------------------------------------------


@pytest.fixture
def recorded(fresh_db, ontology):
    _, inv_set = fresh_db("t0_inv")
    log = AuditLog()
    request = ScreenRequest(target_id=TARGET, snapshot=inv_set)
    decision = Supervisor(ontology, log).run(request).decision
    found, _ = run_investigation(
        log, ontology, decision.decision_id, QUESTION, "ana", "reviewer", utc_now()
    )
    return found


def test_the_command_says_replay_identical(recorded):
    result = cli("audit", "replay-investigation", recorded.investigation_id, "--deep")
    assert result.exit_code == 0, result.output
    assert f"Replay identical: investigation {recorded.investigation_id}" in result.output


def test_the_command_exits_as_the_other_replays_do(recorded, monkeypatch):
    assert cli("audit", "replay-investigation", "nope").exit_code == 2
    monkeypatch.setattr(investigator, "PROMPT_VERSION", 2)
    result = cli("audit", "replay-investigation", recorded.investigation_id)
    assert result.exit_code == 4 and "Replay NOT_REPLAYABLE" in result.output


def test_the_command_prints_json_and_a_difference_exits_one(recorded, monkeypatch):
    original = investigator.check_findings
    result = cli("audit", "replay-investigation", recorded.investigation_id, "--json")
    assert '"verdict": "IDENTICAL"' in result.output
    monkeypatch.setattr(
        investigator, "check_findings", lambda f, c, d: (original(f, c, d)[0], ["a new fault"])
    )
    result = cli("audit", "replay-investigation", recorded.investigation_id)
    assert result.exit_code == 1 and "Replay GUARDRAIL_DIFFERS" in result.output


def test_a_verify_counts_an_investigation_as_no_monitor_run(recorded):
    result = cli("audit", "verify")
    assert result.exit_code == 0 and "Monitor runs" not in result.output


def test_a_first_pass_that_reads_more_is_a_tool_difference(made, ontology, monkeypatch):
    found, log, con = made()
    original = investigator.first_pass

    def more(box, memo):
        box.run("get_entity", {"entity_id": TARGET})
        return original(box, memo)

    monkeypatch.setattr(investigator, "first_pass", more)
    assert replay(ontology, found, log, con).verdict == "TOOL_RESULT_DIFFERS"


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("NOT_REPLAYABLE", "NOT_REPLAYABLE"), ("PAYLOAD_DIFFERS", "GRAPH_CHANGED")],
)
def test_a_decision_that_no_longer_replays_comes_first(made, ontology, monkeypatch, verdict,
                                                       expected):  # fmt: skip
    found, log, con = made()
    stub = SimpleNamespace(verdict=verdict, detail="why", identical=False)
    monkeypatch.setattr(llm_replay, "replay_decision", lambda *args: stub)
    report = replay(ontology, found, log, con)
    assert report.verdict == expected and (verdict in report.detail or report.detail == "why")


def test_no_database_is_not_replayable(made, ontology, monkeypatch, tmp_path):
    found, log, _ = made()
    monkeypatch.setattr(llm_replay, "db_path", lambda: tmp_path / "none.duckdb")
    report = replay_investigation(log, ontology, found.investigation_id)
    assert report.verdict == "NOT_REPLAYABLE" and "no database" in report.detail


def test_stored_turns_answer_no_single_turn_request():
    with pytest.raises(LLMUnavailable):
        llm_replay._StoredTurns([]).complete(None)
