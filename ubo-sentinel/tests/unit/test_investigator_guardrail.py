"""The investigator's guardrail (Step 14.5): each check, shown to fail by breaking a finding."""

import pytest
from llm_fakes import ScriptedClient, call

from ubo_sentinel.agents import investigator
from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.investigator import investigate
from ubo_sentinel.agents.investigator_guardrail import (
    check_actions,
    check_findings,
    resolve_value,
)
from ubo_sentinel.agents.investigator_tools import SUBMIT_FINDINGS
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.llm.config import InvestigateBudget
from ubo_sentinel.models.investigation import Finding, SuggestedAction, ToolCallRecord

TARGET = "lei:FXI03TARGET000000000"
PEER = "lei:FXI01TARGET000000000"
OUTSIDER = "lei:FXI05A00000000000000"
I06_TARGET, I06_OWNER = "lei:FXI06TARGET000000000", "fx_list:i06-owner"
BUDGET = InvestigateBudget("claude-opus-5-5", 12, 200_000, 90.0)

PLACE = {
    "address": "14 Arch Street",
    "country": "KZ",
    "mass": False,
    "total": 1,
    "peers": [{"entity": {"id": PEER, "legal_name": "Marlowe Grain Traders", "aliases": []}}],
}
CALLS = [
    ToolCallRecord(
        call_id="c1",
        tool="get_entities_sharing_address",
        arguments={"entity_id": TARGET},
        items=[PLACE],
    ),
    ToolCallRecord(call_id="c2", tool="get_entity", arguments={"entity_id": OUTSIDER},
                   fault="UNSEEN_ID"),
    ToolCallRecord(call_id="c3", tool="get_sibling_entities", arguments={"entity_id": TARGET}),
    ToolCallRecord(call_id="c4", tool="get_designations_of", arguments={"entity_ids": [PEER]}),
]  # fmt: skip


@pytest.fixture
def decision(fresh_con, ontology):
    con, (_, inv_set) = fresh_con("t0_inv")
    request = ScreenRequest(target_id=TARGET, snapshot=inv_set)
    return Supervisor(ontology, AuditLog()).run(request, con).decision


def finding(**update):
    base = dict(
        code="SHARED_ADDRESS",
        subjects=[TARGET, PEER],
        text="{e:" + TARGET + "} and {e:" + PEER + "} state one place in {v:c1./items/0/country}.",
        cites=["c1"],
    )
    return Finding(**(base | update))


def test_a_finding_that_follows_every_rule_passes(decision):
    good = finding()
    assert check_findings([good], CALLS, decision) == ([good], [])


@pytest.mark.parametrize(
    ("update", "fault"),
    [
        ({"subjects": []}, "a finding about no entity"),
        ({"cites": []}, "a finding that cites nothing"),
        ({"cites": ["c9"]}, "a citation of no call"),
        ({"cites": ["c1", "c2"]}, "a citation of a refused call"),
        ({"code": "PATH_BETWEEN"}, "a finding that cites no tool that can show it"),
        ({"code": "DESIGNATED_SIBLING", "cites": ["c3"]},
         "a finding that cites no tool that can show it"),
        ({"subjects": [TARGET, OUTSIDER]}, "a subject no cited call shows"),
        ({"text": "{e:" + OUTSIDER + "} is there."}, "an entity slot no cited call shows"),
        ({"text": "It is {v:c3./items/0/country}."},
         "a value slot of a call the finding does not cite"),
        ({"text": "It is {v:c1./items/7/country}."}, "a value slot that resolves to no value"),
        ({"text": "It is {v:c1./items/0}."}, "a value slot that resolves to no value"),
        ({"text": "It is {v:c1.items/0/country}."}, "a value slot that resolves to no value"),
        ({"text": "It is {v:c1./items/0/address}."}, "a value slot on a field of free text"),
        ({"text": "It is {v:c1./items/0/peers/0/entity/legal_name}."},
         "a value slot on a field of free text"),
        ({"text": "It is {x:y}."}, "a slot of an unknown kind"),
        ({"text": "It is {e:" + TARGET}, "a slot that is not closed"),
        ({"text": "Held at 60 percent."}, "a digit outside a slot"),
        ({"text": "The target is cleared."}, "a word that states an outcome"),
        ({"text": "The rules say REVIEW here."}, "a word that states an outcome"),
        ({"text": "This is a false positive."}, "a word that states an outcome"),
        ({"text": "The owner is not sanctioned."}, "a word that states an outcome"),
        ({"text": "This should be looked at no further."}, "a word that states an outcome"),
        ({"text": "Marlowe Grain Traders is there."}, "a name outside a slot"),
        ({"text": "Silvercrest Tannery Holdings is there."}, "a name outside a slot"),
        ({"text": "The records say so. " * 20}, "a finding that is too long"),
    ],
)  # fmt: skip
def test_a_broken_finding_is_dropped_by_kind(decision, update, fault):
    kept, faults = check_findings([finding(**update)], CALLS, decision)
    assert kept == [] and fault in faults


def test_a_fault_never_holds_a_value(decision):
    _, faults = check_findings([finding(text="Marlowe Grain Traders held 60.")], CALLS, decision)
    assert faults and not any("Marlowe" in fault or "60" in fault for fault in faults)


def test_one_bad_finding_does_not_take_a_good_one_with_it(decision):
    good = finding()
    kept, faults = check_findings([finding(text="Held at 60."), good], CALLS, decision)
    assert kept == [good] and faults == ["a digit outside a slot"]


def test_a_value_slot_cannot_carry_an_outcome_or_a_sentence(decision):
    long = "A" * 61
    calls = [
        CALLS[0].model_copy(update={"items": [{"program": "CLEARED"}, {"program": "cleared"},
                                              {"program": long}]})
    ]  # fmt: skip
    for index, fault in ((1, "a value that states an outcome"), (2, "a value too long to be one")):
        slot = "{v:c1./items/" + str(index) + "/program}"
        broken = finding(subjects=[TARGET], text=f"Under {slot}.")
        assert check_findings([broken], calls, decision)[1] == [fault]


def test_a_pointer_reads_lists_and_escapes():
    result = {"a/b": [{"c~d": 1}]}
    assert resolve_value("/a~1b/0/c~0d", result) == 1


@pytest.mark.parametrize(
    ("action", "fault"),
    [
        ({"code": "SCREEN_RELATED_ENTITY"}, "a suggested screen of no entity"),
        ({"code": "SCREEN_RELATED_ENTITY", "entity_id": OUTSIDER},
         "an action naming an entity it was not given"),
        ({"code": "REQUEST_DOCUMENT"}, "a document on the wrong action"),
        ({"code": "OBTAIN_SHAREHOLDING", "document": "PROOF_OF_ADDRESS"},
         "a document on the wrong action"),
    ],
)  # fmt: skip
def test_a_broken_action_is_dropped_by_kind(decision, ontology, action, fault):
    kept, faults = check_actions([SuggestedAction(**action)], CALLS, decision, ontology)
    assert kept == [] and faults == [fault]


@pytest.mark.parametrize(
    "action",
    [
        {"code": "SCREEN_RELATED_ENTITY", "entity_id": PEER},
        {"code": "REQUEST_DOCUMENT", "document": "SHAREHOLDER_REGISTER"},
        {"code": "OBTAIN_SHAREHOLDING", "entity_id": TARGET},
        {"code": "OBTAIN_SHAREHOLDING"},
    ],
)
def test_a_good_action_passes(decision, ontology, action):
    good = SuggestedAction(**action)
    assert check_actions([good], CALLS, decision, ontology) == ([good], [])


def test_a_refused_call_gives_an_action_nothing(decision, ontology):
    action = SuggestedAction(code="SCREEN_RELATED_ENTITY", entity_id=OUTSIDER)
    assert check_actions([action], CALLS, decision, ontology)[0] == []


# --- through an investigation --------------------------------------------------------------------


@pytest.fixture
def case(fresh_con, ontology):
    con, (_, inv_set) = fresh_con("t0_inv")

    def make(target):
        request = ScreenRequest(target_id=target, snapshot=inv_set)
        return Supervisor(ontology, AuditLog()).run(request, con).decision, con

    return make


@pytest.mark.parametrize(
    "target",
    [
        TARGET,
        PEER,
        "lei:FXI04TARGET000000000",
        "lei:FXI05A00000000000000",
        I06_TARGET,
    ],
)
def test_the_first_pass_of_each_case_passes_its_own_guardrail(case, ontology, target):
    decision, con = case(target)
    found = investigate(decision, "Who else?", ontology, con)
    assert found.first_pass and found.faults == []


def test_a_linked_record_finding_passes_with_its_party_cited(fresh_con, ontology):
    con, (t0,) = fresh_con("t0")
    request = ScreenRequest(target_id="lei:FXS27TARGET000000000", snapshot=t0)
    decision = Supervisor(ontology, AuditLog()).run(request, con).decision
    found = investigate(decision, "Who else?", ontology, con)
    assert [f.code for f in found.first_pass] == ["LINKED_RECORD_OWNERS"] and found.faults == []


def test_a_first_pass_finding_that_fails_is_dropped_before_a_model_reads_it(
    case, ontology, monkeypatch
):
    decision, con = case(TARGET)
    bad = Finding(code="SHARED_ADDRESS", subjects=[TARGET], text="Held at 60.", cites=["c1"])
    original = investigator.first_pass

    def broken(box, memo):
        found, left_out = original(box, memo)
        return [*found, bad], left_out

    monkeypatch.setattr(investigator, "first_pass", broken)
    submit = call(SUBMIT_FINDINGS, findings=[], suggested_actions=[])
    client = ScriptedClient([[submit]])
    found = investigate(decision, "Who else?", ontology, con, client, BUDGET)
    assert bad not in found.first_pass and found.first_pass
    assert "a digit outside a slot" in found.faults
    assert "Held at" not in client.requests[0].messages[0]["content"][0]["text"]
    assert found.turns, "a dropped first-pass finding does not stop the model"


def test_a_models_bad_finding_is_dropped_and_its_good_one_kept(case, ontology):
    decision, con = case(TARGET)
    good = {
        "code": "SHARED_ADDRESS",
        "subjects": [TARGET],
        "text": "{e:" + TARGET + "} states a place.",
        "cites": ["c1"],
    }
    bad = good | {"text": "The target is cleared."}
    action = {"code": "SCREEN_RELATED_ENTITY", "entity_id": OUTSIDER, "document": None}
    submit = call(SUBMIT_FINDINGS, findings=[good, bad], suggested_actions=[action])
    found = investigate(decision, "Who else?", ontology, con, ScriptedClient([[submit]]), BUDGET)
    assert [f.text for f in found.findings] == [good["text"]]
    assert found.suggested_actions == []
    assert found.faults == [
        "a word that states an outcome",
        "an action naming an entity it was not given",
    ]
    assert found.complete, "a dropped finding is a fault, not an unfinished run"


def test_an_injected_alias_cannot_reach_a_reader_through_a_value_slot(case, ontology):
    """The i06 case: a name written to steer a model is text, and no slot reads text."""
    decision, con = case(I06_TARGET)
    owners = call("get_direct_owners", entity_id=I06_TARGET)
    pointer = "/items/0/owner/aliases/0"

    def submit(request):
        call_id = request.messages[-1]["content"][0]["content"].split('"')[3]
        text = "{v:" + call_id + "." + pointer + "}"
        finding = {"code": "COMMON_CONTROLLER", "subjects": [I06_TARGET], "text": text,
                   "cites": [call_id]}  # fmt: skip
        return [call(SUBMIT_FINDINGS, findings=[finding], suggested_actions=[])]

    found = investigate(
        decision, "Who owns it?", ontology, con, ScriptedClient([[owners], submit]), BUDGET
    )
    assert found.findings == [] and "a value slot on a field of free text" in found.faults
