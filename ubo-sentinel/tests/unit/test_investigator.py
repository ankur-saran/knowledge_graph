"""The investigator (Step 14.4): the first pass with no model, then the loop and its budgets.
No network, no key: a scripted model."""

import json
from types import SimpleNamespace

import pytest
from conftest import INV
from e2e import person_pii
from llm_fakes import ScriptedClient, call, say

from ubo_sentinel.agents import investigator
from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.investigator import LAST_CALL, SUBMIT_NOW, SYSTEM, investigate
from ubo_sentinel.agents.investigator_tools import SUBMIT_FINDINGS, ToolBox, tool_specs
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.views import MASK
from ubo_sentinel.llm.client import RecordedClient
from ubo_sentinel.llm.config import InvestigateBudget
from ubo_sentinel.llm.tools import ToolTurn
from ubo_sentinel.models import EntityRow, read_rows
from ubo_sentinel.models.canonical import canonical_json

I01_TARGET = "lei:FXI01TARGET000000000"
I01_DESIGNATED = "fx_list:i01-designated"
I03_TARGET, I03_DESIGNATED = "lei:FXI03TARGET000000000", "lei:FXI03DESIG0000000000"
I04_TARGET, I04_SIBLING = "lei:FXI04TARGET000000000", "lei:FXI04SIBLING00000000"
I04_PARENT = "lei:FXI04PARENT000000000"
I05_A = "lei:FXI05A00000000000000"
I06_TARGET = "lei:FXI06TARGET000000000"
QUESTION = "Who else does this owner control?"
BUDGET = InvestigateBudget(
    model="claude-opus-5-5", max_steps=12, max_tokens=200_000, max_seconds=90.0
)
FINDING = {
    "code": "SHARED_ADDRESS",
    "subjects": [I03_TARGET],
    "text": "{e:" + I03_TARGET + "} shows it.",
    "cites": ["c1"],
}
ACTION = {"code": "REQUEST_DOCUMENT", "entity_id": I03_TARGET, "document": "PROOF_OF_ADDRESS"}


def submit(findings=(FINDING,), actions=(ACTION,)):
    return call(SUBMIT_FINDINGS, findings=list(findings), suggested_actions=list(actions))


LOOK = call("get_entity", entity_id=I03_TARGET)


@pytest.fixture
def case(fresh_con, ontology):
    """(target) -> (decision on the inv set, connection)."""
    con, (_, inv_set) = fresh_con("t0_inv")

    def make(target):
        request = ScreenRequest(target_id=target, snapshot=inv_set)
        return Supervisor(ontology, AuditLog()).run(request, con).decision, con

    return make


@pytest.fixture
def run(case, ontology):
    """(target, script, budget, ...) -> (investigation, scripted client)."""

    def go(target, script, budget=BUDGET, question=QUESTION, **options):
        decision, con = case(target)
        client = ScriptedClient(script, **options)
        return investigate(decision, question, ontology, con, client, budget), client

    return go


def sent(client):
    """Every model input of a run, as text."""
    return "".join(canonical_json(r.model_dump(mode="json")).decode() for r in client.requests)


def last_user_text(request):
    return request.messages[-1]["content"][-1]["text"]


# --- the first pass -----------------------------------------------------------------------------


def test_the_first_pass_needs_no_model(case, ontology):
    decision, con = case(I01_TARGET)
    found = investigate(decision, QUESTION, ontology, con)
    (finding,) = found.first_pass
    assert finding.code == "SHARED_ADDRESS"
    assert finding.subjects == [I01_TARGET, I01_DESIGNATED]
    assert (found.findings, found.turns, found.model) == ([], [], None)
    assert found.complete and found.faults == [] and found.egress == "analyst_masked"
    assert found.budgets == {} and found.usage == {}


def test_a_common_director_with_a_designated_company_is_a_person_by_pseudonym(case, ontology):
    decision, con = case(I03_TARGET)
    (finding,) = investigate(decision, QUESTION, ontology, con).first_pass
    assert finding.code == "COMMON_CONTROLLER"
    assert finding.subjects[:2] == [I03_TARGET, I03_DESIGNATED]
    assert finding.subjects[2].startswith("person:") and len(finding.cites) == 2


def test_a_designated_sibling(case, ontology):
    decision, con = case(I04_TARGET)
    (finding,) = investigate(decision, QUESTION, ontology, con).first_pass
    assert finding.code == "DESIGNATED_SIBLING"
    assert finding.subjects == [I04_TARGET, I04_SIBLING, I04_PARENT]


def test_a_mass_address_is_no_finding_and_nothing_found_cites_what_was_read(case, ontology):
    decision, con = case(I06_TARGET)
    found = investigate(decision, QUESTION, ontology, con)
    (finding,) = found.first_pass
    assert finding.code == "NO_FURTHER_LINK_FOUND"
    assert finding.cites == [call.call_id for call in found.calls]
    assert found.coverage.capped and found.coverage.capped[0].left_out > 50


def test_coverage_says_what_was_looked_at(case, ontology):
    decision, con = case(I04_TARGET)
    coverage = investigate(decision, QUESTION, ontology, con).coverage
    assert "get_entities_sharing_address" in coverage.tools_called
    assert {I04_TARGET, I04_SIBLING} <= set(coverage.entities_examined)
    assert coverage.refused == {} and coverage.subjects_not_examined == 0


def test_a_set_with_no_context_layer_is_said_not_hidden(case, ontology):
    decision, con = case(I03_TARGET)
    con.execute("DELETE FROM ctx_builds")
    found = investigate(decision, QUESTION, ontology, con)
    assert found.coverage.refused == {"NO_CONTEXT": 1} and found.context_digest is None


def test_a_linked_record_with_owners_is_a_finding(fresh_con, ontology):
    con, (t0,) = fresh_con("t0")
    request = ScreenRequest(target_id="lei:FXS27TARGET000000000", snapshot=t0)
    decision = Supervisor(ontology, AuditLog()).run(request, con).decision
    found = investigate(decision, QUESTION, ontology, con)
    assert [f.code for f in found.first_pass] == ["LINKED_RECORD_OWNERS"]
    assert found.first_pass[0].subjects[:2] == [decision.target_id, "fx_list:s27-dup"]


def test_a_change_since_an_earlier_set_is_a_finding(fresh_con, ontology):
    con, (_, t1) = fresh_con("t0_t1")
    request = ScreenRequest(target_id="lei:FXS01TARGET000000000", snapshot=t1)
    decision = Supervisor(ontology, AuditLog()).run(request, con).decision
    found = investigate(decision, QUESTION, ontology, con)
    assert "HISTORY_CHANGE" in [f.code for f in found.first_pass]


def test_the_first_pass_looks_around_a_capped_number_of_parties():
    memo = SimpleNamespace(
        target_id="t",
        blocked_set=[f"b{i:02}" for i in range(12)],
        possibly_blocked_set=["t"],
        matches=[SimpleNamespace(effect="POSSIBLY_BLOCKED", designated_entity_id="z")],
    )
    subjects, left_out = investigator._subjects(memo)
    assert subjects[:2] == ["t", "b00"] and len(subjects) == investigator.MAX_SUBJECTS
    assert left_out == 4


def test_a_fund_manager_is_no_common_controller():
    def run(_, arguments):
        groups = [
            {"fund_manager": True, "parent": {"id": "m"}, "siblings": [{"entity": {"id": "s"}}]}
        ]
        items = {
            "entity_id": groups,
            "entity_ids": [{"fund_manager": True, "controller": {"id": "m"}}],
        }
        return SimpleNamespace(call_id="c1", items=items[next(iter(arguments))])

    box = SimpleNamespace(run=run)
    assert investigator._siblings(box, "t") == []
    assert investigator._common_controllers(box, ["t", "u"]) == []
    assert investigator._common_controllers(box, ["t"]) == []


def test_a_decision_with_no_target_has_nothing_to_look_around(fresh_con, ontology):
    con, (t0,) = fresh_con("t0")
    request = ScreenRequest(query="Nobody Of This Name At All", snapshot=t0)
    decision = Supervisor(ontology, AuditLog()).run(request, con).decision
    client = ScriptedClient([])
    found = investigate(decision, QUESTION, ontology, con, client, BUDGET)
    assert found.first_pass == [] and found.faults == ["no target"] and not found.complete
    assert client.requests == []
    assert not investigate(decision, QUESTION, ontology, con).complete


def test_the_same_question_is_the_same_investigation(case, ontology):
    decision, con = case(I03_TARGET)
    one = investigate(decision, QUESTION, ontology, con)
    assert one == investigate(decision, QUESTION, ontology, con)
    assert one.investigation_id != (
        investigate(decision, "Another question?", ontology, con).investigation_id
    )


def test_nothing_is_written_and_the_decision_does_not_move(case, ontology):
    decision, con = case(I03_TARGET)
    log = AuditLog()
    before = len(log.records())
    payload = decision.canonical_payload()
    investigate(decision, QUESTION, ontology, con, ScriptedClient([[submit()]]), BUDGET)
    assert len(log.records()) == before and decision.canonical_payload() == payload


# --- the loop -----------------------------------------------------------------------------------


def test_the_model_starts_from_the_first_pass_and_its_findings_stay_apart(run):
    found, client = run(I03_TARGET, [[say("Looking."), LOOK], [submit()]])
    assert found.complete and found.faults == []
    assert [f.code for f in found.first_pass] == ["COMMON_CONTROLLER"]
    assert [f.code for f in found.findings] == ["SHARED_ADDRESS"]
    assert found.suggested_actions[0].document == "PROOF_OF_ADDRESS"
    assert found.model == BUDGET.model and found.budgets["max_steps"] == 12
    assert found.usage["steps"] == 1 and found.usage["turns"] == 2
    assert found.usage["input_tokens"] == 200 and found.usage["output_tokens"] == 40
    assert found.turns[0].call_ids == [found.calls[-1].call_id]
    assert found.turns[0].request_digest == client.requests[0].digest()


def test_the_first_turn_holds_the_memo_the_findings_and_the_question_as_data(run):
    _, client = run(I03_TARGET, [[submit()]])
    request = client.requests[0]
    assert request.system == SYSTEM and QUESTION not in request.system
    text = last_user_text(request)
    assert text.startswith("The decision, what a first pass found")
    data = json.loads(text[text.index("{") :])
    assert data["question"] == QUESTION
    assert data["first_pass"]["findings"][0]["code"] == "COMMON_CONTROLLER"
    assert {r["call_id"] for r in data["first_pass"]["results"]} == {"c2", "c3"}
    assert data["memo"]["target_id"] == I03_TARGET and data["records"]


def test_a_request_offers_every_tool_and_asks_the_budget(run):
    _, client = run(I03_TARGET, [[LOOK], [submit()]])
    for request in client.requests:
        assert request.tools == tool_specs()
        assert request.task_budget == BUDGET.max_tokens and request.fallbacks


def test_the_history_only_grows_and_turns_go_back_unchanged(run):
    _, client = run(I03_TARGET, [[say("One."), LOOK], [say("Two."), LOOK], [submit()]])
    first, second, third = client.requests
    assert second.messages[: len(first.messages)] == first.messages
    assert third.messages[: len(second.messages)] == second.messages
    assert second.messages[1]["role"] == "assistant"
    assert second.messages[1]["content"][0]["text"] == "One."


def test_a_haiku_model_asks_no_fallback_and_a_small_budget_no_task_budget(run):
    small = InvestigateBudget("claude-haiku-5-5", 12, 19_999, 90.0)
    _, client = run(I03_TARGET, [[submit()]], small)
    assert not client.requests[0].fallbacks and client.requests[0].task_budget is None


def test_a_model_is_given_a_budget(case, ontology):
    decision, con = case(I03_TARGET)
    with pytest.raises(ValueError, match="budget"):
        investigate(decision, QUESTION, ontology, con, ScriptedClient([]))


def test_every_call_is_a_step_and_a_repeat_is_one_too(run):
    found, _ = run(I03_TARGET, [[LOOK], [LOOK], [submit()]])
    assert found.usage["steps"] == 2
    assert found.calls[-1].repeat_of == found.calls[-2].call_id


def test_a_refused_call_is_told_to_the_model_as_an_error(run):
    found, client = run(I03_TARGET, [[call("get_entity", entity_id=I01_TARGET)], [submit()]])
    assert found.coverage.refused == {"UNSEEN_ID": 1}
    (block,) = client.requests[1].messages[-1]["content"]
    assert block["is_error"] is True and "UNSEEN_ID" in block["content"]


def test_a_person_never_reaches_the_model(run):
    script = [
        [call("get_direct_owners", entity_id=I03_TARGET)],
        [call("get_entities_sharing_address", entity_id=I03_DESIGNATED)],
        [submit()],
    ]
    found, client = run(I03_TARGET, script)
    assert found.coverage.refused == {}
    names = [*person_pii(read_rows(INV / "entities.csv", EntityRow)), "Casimir Ondrejka"]
    text = sent(client)
    assert [value for value in names if value in text] == []


def test_a_question_holding_a_masked_name_is_not_sent_or_kept(run):
    found, client = run(I03_TARGET, [], question="Is Casimir Ondrejka known elsewhere?")
    assert client.requests == [] and found.question == MASK
    assert found.faults == ["question held a masked value"] and not found.complete
    assert found.first_pass


def test_a_masked_question_with_no_model_keeps_the_first_pass_complete(case, ontology):
    decision, con = case(I03_TARGET)
    found = investigate(decision, "Who is Casimir Ondrejka?", ontology, con)
    assert found.question == MASK and found.complete


def test_an_opening_that_holds_a_masked_value_is_not_sent(run, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(ToolBox, "holds_masked", lambda self, data: next(answers))
    found, client = run(I03_TARGET, [])
    assert client.requests == [] and found.faults == ["model input held a masked value"]
    assert not found.complete and found.model is None


def test_a_submission_of_no_known_form_is_dropped_by_kind(run):
    bad = {"code": "SANCTIONS_EVASION", "subjects": [], "text": "x", "cites": []}
    found, _ = run(I03_TARGET, [[submit([bad, FINDING], [{"code": "APPROVE"}])]])
    assert [f.code for f in found.findings] == ["SHARED_ADDRESS"]
    assert found.suggested_actions == []
    assert found.faults == ["a submission of no known form"]


def test_a_submission_that_is_not_a_list_gives_nothing(run):
    found, _ = run(I03_TARGET, [[call(SUBMIT_FINDINGS, findings="none", suggested_actions=None)]])
    assert found.findings == [] and found.complete


def test_calls_beside_a_submission_are_not_run(run):
    found, _ = run(I03_TARGET, [[LOOK, submit()]])
    assert found.usage["steps"] == 0


# --- budgets ------------------------------------------------------------------------------------


def budget(**update):
    return InvestigateBudget(**(vars(BUDGET) | update))


def test_a_spent_step_budget_asks_for_the_findings_and_keeps_them(run):
    found, client = run(I03_TARGET, [[LOOK], [LOOK], [submit()]], budget(max_steps=2))
    assert last_user_text(client.requests[-1]) == LAST_CALL
    assert client.requests[-1].tools == client.requests[0].tools
    assert [f.code for f in found.findings] == ["SHARED_ADDRESS"]
    assert not found.complete and found.faults == []


def test_a_spent_token_budget_is_read_from_what_turns_used(run):
    found, client = run(I03_TARGET, [[LOOK], [submit()]], budget(max_tokens=100))
    assert last_user_text(client.requests[-1]) == LAST_CALL and not found.complete


def test_a_spent_time_budget_is_read_from_what_turns_took(run):
    found, client = run(I03_TARGET, [[LOOK], [submit()]], budget(max_seconds=5.0), seconds=6.0)
    assert last_user_text(client.requests[-1]) == LAST_CALL and not found.complete
    assert found.usage["seconds"] == 12.0


def test_a_cached_prefix_is_not_spent_again(run):
    def turn(request):
        return ToolTurn(
            content=[submit() | {"id": "toolu_1"}],
            stop_reason="tool_use",
            model=request.model,
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=900_000,
            cache_write_tokens=20,
        )

    found, _ = run(I03_TARGET, [turn], budget(max_tokens=100))
    assert found.usage["input_tokens"] == 30 and found.usage["cache_read_tokens"] == 900_000
    assert found.complete


def test_calls_past_the_step_budget_in_one_turn_are_refused(run):
    other = call("get_reporting_exceptions", entity_id=I03_TARGET)
    found, client = run(I03_TARGET, [[LOOK, other], [submit()]], budget(max_steps=1))
    assert found.usage["steps"] == 1 and found.coverage.refused == {"BUDGET_SPENT": 1}
    results = client.requests[-1].messages[-1]["content"]
    assert [block["type"] for block in results] == ["tool_result", "tool_result", "text"]


def test_any_call_but_the_submission_on_the_last_turn_is_refused(run):
    found, _ = run(I03_TARGET, [[LOOK], [LOOK]], budget(max_steps=1))
    assert found.coverage.refused == {"BUDGET_SPENT": 1}
    assert found.faults == ["no submission"] and not found.complete
    assert found.first_pass


def test_a_model_that_stops_without_submitting_is_asked_once(run):
    found, client = run(I03_TARGET, [[say("I am done.")], [submit()]])
    assert last_user_text(client.requests[-1]) == SUBMIT_NOW
    assert found.complete and [f.code for f in found.findings] == ["SHARED_ADDRESS"]


def test_a_model_that_never_submits_is_a_fault(run):
    found, _ = run(I03_TARGET, [[say("Done.")], [say("Still done.")]])
    assert found.faults == ["no submission"] and not found.complete


def paused(request):
    content = [say("Working.")]
    return ToolTurn(content=content, stop_reason="pause_turn", model=request.model, seconds=0.1)


def test_a_paused_turn_is_sent_back_as_it_is(run):
    found, client = run(I03_TARGET, [paused, [submit()]])
    assert client.requests[1].messages[-1] == {"role": "assistant", "content": [say("Working.")]}
    assert found.complete and found.usage["steps"] == 0


def test_a_model_that_pauses_for_ever_is_stopped(run):
    found, client = run(I03_TARGET, [paused] * 30, budget(max_steps=2))
    assert len(client.requests) == 6 and found.faults == ["no submission"]


def test_a_model_that_fails_keeps_what_was_found(run):
    found, _ = run(I03_TARGET, [[LOOK], [submit()]], fail_at=2)
    assert found.faults == ["model unavailable"] and not found.complete
    assert found.first_pass and found.findings == [] and len(found.turns) == 1


# --- record and replay ---------------------------------------------------------------------------


def test_a_recorded_run_replays_turn_by_turn_with_no_model(case, ontology, tmp_path):
    decision, con = case(I03_TARGET)
    script = [[say("Looking."), LOOK], [call("get_sibling_entities", entity_id=I03_TARGET)]]
    live = ScriptedClient([*script, [submit()]], seconds=31.0)
    spend = budget(max_seconds=60.0)
    made = investigate(decision, QUESTION, ontology, con, RecordedClient(tmp_path, live), spend)
    assert not made.complete, "the time budget ran out"
    replayed = investigate(decision, QUESTION, ontology, con, RecordedClient(tmp_path), spend)
    assert replayed == made


# --- the first pass, from stubbed results ---------------------------------------------------------


def test_a_controller_of_two_parties_is_a_finding():
    item = {"fund_manager": False, "controller": {"id": "c"}, "held_ids": ["t", "u"]}
    box = SimpleNamespace(run=lambda *_: SimpleNamespace(call_id="c1", items=[item]))
    (finding,) = investigator._common_controllers(box, ["t", "u"])
    assert finding.code == "COMMON_CONTROLLER" and finding.subjects == ["c", "t", "u"]


def test_only_a_linked_record_action_that_names_its_record_is_followed():
    actions = [
        SimpleNamespace(code="OBTAIN_SHAREHOLDING", params={}, subject_id="t"),
        SimpleNamespace(code="SCREEN_LINKED_RECORD", params={}, subject_id="t"),
        SimpleNamespace(code="SCREEN_LINKED_RECORD", params={"linked_id": "l"}, subject_id=None),
    ]
    assert investigator._linked(None, SimpleNamespace(next_actions=actions)) == []


def test_first_pass_results_past_the_limit_are_held_back_and_counted(run, monkeypatch):
    monkeypatch.setattr(investigator, "MAX_FIRST_PASS_BYTES", 0)
    _, client = run(I03_TARGET, [[submit()]])
    text = last_user_text(client.requests[0])
    data = json.loads(text[text.index("{") :])
    assert data["first_pass"]["results"] == [] and data["first_pass"]["results_held_back"] == 2
