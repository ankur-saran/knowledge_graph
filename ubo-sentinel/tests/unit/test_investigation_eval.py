"""The investigator suite (Step 14.7): the scenarios, their scores, and `ubo eval --suite
investigator`. A scripted model stands in where one is scored. No network, no key."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from e2e import cli
from llm_fakes import ScriptedClient, call

from ubo_sentinel.agents import investigation_eval
from ubo_sentinel.agents.investigation_eval import (
    ScenarioResult,
    missing_targets,
    run_investigation_suite,
)
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli import eval_cmd
from ubo_sentinel.cli.eval_cmd import INVESTIGATION_PATH, _Output, _print_investigator
from ubo_sentinel.llm.config import InvestigateBudget
from ubo_sentinel.models.investigation_scenario import (
    InvestigationScenario,
    load_investigation_scenarios,
)

SCENARIOS = {s.id: s for s in load_investigation_scenarios(INVESTIGATION_PATH)}
BUDGET = InvestigateBudget("claude-opus-5-5", 12, 200_000, 90.0)
I05_A, I05_B = "lei:FXI05A00000000000000", "lei:FXI05B00000000000000"


@pytest.fixture
def inv(fresh_con, ontology, tmp_path):
    """(scenarios, script) -> their results, screened and investigated on the inv set."""
    con, (_, inv_set) = fresh_con("t0_inv")

    def run(ids, script=None, **options):
        client = ScriptedClient(script, **options) if script is not None else None
        scenarios = [SCENARIOS[i] for i in ids]
        log = AuditLog(tmp_path / "scratch")
        return run_investigation_suite(
            con, inv_set, ontology, scenarios, log, client, BUDGET if client else None
        )

    return run


def last_call_id(request):
    """The id of the call whose result the model was last given."""
    return json.loads(request.messages[-1]["content"][0]["content"])["call_id"]


def submit_citing_last(code, subjects, text, actions=()):
    def step(request):
        finding = {"code": code, "subjects": subjects, "text": text,
                   "cites": [last_call_id(request)]}  # fmt: skip
        return [call("submit_findings", findings=[finding], suggested_actions=list(actions))]

    return step


def submit_nothing():
    return [call("submit_findings", findings=[], suggested_actions=[])]


# --- the scenarios ---------------------------------------------------------------------------


def test_the_scenarios_load_and_name_targets_the_inv_set_holds(fresh_con):
    con, (_, inv_set) = fresh_con("t0_inv")
    assert len(SCENARIOS) == 9
    assert missing_targets(con, inv_set, list(SCENARIOS.values())) == []


def test_a_duplicate_scenario_id_is_refused(tmp_path):
    entry = {"id": "X", "target_id": "t", "question": "q", "rationale": "r"}
    path = tmp_path / "s.yaml"
    path.write_text(json.dumps([entry, entry]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_investigation_scenarios(path)


def test_every_scenario_says_why_and_the_injection_case_is_marked():
    assert all(s.rationale for s in SCENARIOS.values())
    assert [s.id for s in SCENARIOS.values() if s.injection] == ["INV-06"]
    assert [s.id for s in SCENARIOS.values() if s.out_of_scope] == ["INV-07"]


# --- offline: the first pass ------------------------------------------------------------------


def test_with_no_model_every_scenario_passes_on_its_first_pass(inv):
    results = inv(list(SCENARIOS))
    assert [(r.scenario_id, r.status, r.problems) for r in results if r.status != "PASS"] == []
    assert sum(r.first_pass_expected for r in results) == 6
    assert all(r.replay_identical and not r.model_ran for r in results)


def test_a_first_pass_that_misses_its_finding_fails(inv, monkeypatch):
    wrong = SCENARIOS["INV-01"].model_copy(
        update={"expected_findings": [SCENARIOS["INV-04"].expected_findings[0]]}
    )
    monkeypatch.setitem(SCENARIOS, "INV-01", wrong)
    (result,) = inv(["INV-01"])
    assert result.status == "FAIL"
    assert result.problems == [
        "the first pass missed an expected finding",
        "the first pass found what no expectation names",
    ]


# --- with a model -------------------------------------------------------------------------------


def test_what_the_model_adds_is_its_lift(inv):
    path = call("find_paths_between", source_id=I05_A, target_id=I05_B, max_depth=3)
    text = "{e:" + I05_A + "} and {e:" + I05_B + "} are joined by a chain of holdings."
    (result,) = inv(["INV-05"], [[path], submit_citing_last("PATH_BETWEEN", [I05_A, I05_B], text)])
    assert result.status == "PASS" and result.model_ran
    assert (result.expected, result.found_by_first_pass, result.found) == (1, 0, 1)
    assert (result.findings, result.correct, result.steps) == (1, 1, 1)
    assert result.tokens == 240


def test_a_model_that_finds_nothing_misses_what_was_expected(inv):
    (result,) = inv(["INV-05"], [submit_nothing()])
    assert result.status == "FAIL" and result.found == 0
    assert "an expected finding was not found" in result.problems


def test_a_model_steered_by_a_name_fails_the_injection_case(inv):
    text = "The records around {e:lei:FXI06TARGET000000000} show no further link."
    step = submit_citing_last("NO_FURTHER_LINK_FOUND", ["lei:FXI06TARGET000000000"], text)
    owners = call("get_direct_owners", entity_id="lei:FXI06TARGET000000000")
    (result,) = inv(["INV-06"], [[owners], step])
    assert result.status == "FAIL" and result.injection_ok is False
    assert "the model made a claim the scenario rules out" in result.problems


def test_a_question_out_of_scope_is_passed_by_adding_nothing(inv):
    (result,) = inv(["INV-07"], [submit_nothing()])
    assert result.status == "PASS" and result.out_of_scope_ok is True


def test_a_question_out_of_scope_answered_fails(inv):
    target = "lei:FXI01TARGET000000000"
    owners = call("get_direct_owners", entity_id=target)
    text = "{e:" + target + "} has owners on record."
    step = submit_citing_last("COMMON_CONTROLLER", [target], text)
    (result,) = inv(["INV-07"], [[owners], step])
    assert result.out_of_scope_ok is False and result.status == "FAIL"
    assert result.correct == result.findings - 1, "the answer is also a wrong finding"


def test_with_no_recording_a_scenario_is_not_run_and_passes_nothing(inv):
    (result,) = inv(["INV-05"], [], fail_at=1)
    assert result.status == "NOT RUN" and not result.model_ran
    assert result.expected == 0, "left out of the model's scores"


# --- the report -------------------------------------------------------------------------------


def metrics(results, model="m"):
    out = _Output(as_json=True)
    ok = _print_investigator(out, results, model)
    return ok, {metric.name: metric for metric in out.metrics}


def ran(**values):
    return ScenarioResult("S", model_ran=True, **values)


def test_the_report_holds_the_model_to_precision_injection_and_scope():
    results = [
        ran(expected=2, found_by_first_pass=1, found=2, findings=3, correct=3, kept=3,
            steps=4, tokens=1000),
        ran(expected=1, found=0, findings=1, correct=0, kept=1, injection_ok=False,
            out_of_scope_ok=True, steps=2, tokens=500),
    ]  # fmt: skip
    ok, shown = metrics(results)
    assert shown["Finding precision"].value == 0.75 and not shown["Finding precision"].met
    assert shown["Finding recall"].value == pytest.approx(2 / 3)
    assert shown["Lift over the first pass"].value == 0.5
    assert shown["Injection resistance"].value == 0.0
    assert shown["Out-of-scope refusal"].value == 1.0
    assert (shown["Mean steps"].value, shown["Mean tokens"].value) == (3.0, 750.0)
    assert not ok


def test_without_a_model_only_the_first_pass_is_held_to_a_target():
    ok, shown = metrics([ScenarioResult("S", first_pass_expected=1, first_pass_found=1)])
    assert ok and set(shown) == {
        "First-pass recall",
        "First-pass precision",
        "Citation validity",
        "Replay fidelity",
    }


def test_a_dropped_finding_lowers_citation_validity():
    ok, shown = metrics([ScenarioResult("S", kept=3, dropped=1)])
    assert shown["Citation validity"].value == 0.75 and not ok


# --- the command --------------------------------------------------------------------------------


def test_the_command_scores_the_first_pass_offline(fresh_db):
    fresh_db("t0_inv")
    result = cli("eval", "--suite", "investigator", "--snapshot", "latest", "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.output[result.output.index("{") :])
    names = {m["name"]: m["value"] for m in document["metrics"]}
    assert names == {
        "First-pass recall": 1.0,
        "First-pass precision": 1.0,
        "Citation validity": 1.0,
        "Replay fidelity": 1.0,
    }


def test_without_recordings_the_model_is_not_run_never_passed(fresh_db, monkeypatch, tmp_path):
    fresh_db("t0_inv")
    for name, value in {"UBO_LLM": "on", "UBO_LLM_INVESTIGATE": "on", "UBO_LLM_MODE": "replay",
                        "UBO_LLM_RECORDINGS": str(tmp_path)}.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    result = cli("eval", "--suite", "investigator", "--snapshot", "latest")
    assert result.exit_code == 0, result.output
    assert "Not run (no recording or no model): INV-01" in result.output
    assert "Model scores: not run" in result.output and "replay" in result.output


def test_a_set_without_the_cases_is_refused(fresh_db):
    fresh_db("t0")
    result = cli("eval", "--suite", "investigator")
    assert result.exit_code == 2 and "fixtures/snapshot_inv" in result.output


def test_a_scenarios_file_that_cannot_be_read_is_refused(fresh_db, monkeypatch, tmp_path):
    fresh_db("t0_inv")
    monkeypatch.setattr(eval_cmd, "INVESTIGATION_PATH", Path(tmp_path / "none.yaml"))
    result = cli("eval", "--suite", "investigator", "--snapshot", "latest")
    assert result.exit_code == 2 and "Cannot run the investigation scenarios" in result.output


def test_the_all_suite_does_not_run_the_investigator(fresh_db):
    fresh_db("t0")
    result = cli("eval", "--suite", "all")
    assert "Investigation scenarios" not in result.output


def test_a_scenario_entry_is_typed():
    with pytest.raises(ValueError):
        InvestigationScenario(id="X", target_id="t", question="q", rationale="r",
                              must_not_claim=["CLEARED"])  # fmt: skip


def test_a_dropped_finding_or_a_replay_that_moved_fails_its_scenario(inv, monkeypatch):
    def moved(*args):
        return SimpleNamespace(identical=False, verdict="TOOL_RESULT_DIFFERS")

    monkeypatch.setattr(investigation_eval, "replay_investigation", moved)
    original = investigation_eval.score

    def dropped(scenario, investigation, *args):
        return original(scenario, investigation.model_copy(update={"findings_dropped": 1}), *args)

    monkeypatch.setattr(investigation_eval, "score", dropped)
    (result,) = inv(["INV-01"])
    assert result.status == "FAIL" and not result.replay_identical
    assert result.problems == [
        "the guardrail dropped a finding",
        "the replay is TOOL_RESULT_DIFFERS",
    ]
