"""Memo prose by a model (Step 13.1): masked input, slots, the guardrail, the record."""

import json

import pytest
from e2e import cli, person_pii
from llm_fakes import FakeClient, gold_decisions, good_prose

from ubo_sentinel.agents import llm_prose
from ubo_sentinel.agents.llm_prose import check_slotted, prose_input, render_prose
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.audit.replay import replay_decision
from ubo_sentinel.cli import annotate_cmd
from ubo_sentinel.graph.views import MASK, mask_decision
from ubo_sentinel.llm import annotation as annotations
from ubo_sentinel.llm.egress import EgressRefused, check_egress
from ubo_sentinel.models.canonical import canonical_json

MODEL = "claude-haiku-4-5"


@pytest.fixture
def screened(fresh_con, ontology, gold):
    return gold_decisions(fresh_con, ontology, gold, ["SCEN-03", "SCEN-14", "SCEN-01"])


@pytest.fixture
def decisions(screened):
    return screened[1]


@pytest.fixture
def llm_on(monkeypatch):
    for name in ("UBO_LLM", "UBO_LLM_PROSE", "UBO_LLM_MATCH"):
        monkeypatch.setenv(name, "on")


def _annotate(decision, ontology, output, log=None):
    return llm_prose.annotate_prose(decision, ontology, FakeClient(output), MODEL, 1000, log=log)


def test_the_model_reads_the_analyst_s_view_and_no_person(decisions, ontology, entities):
    data = prose_input(decisions["SCEN-03"], ontology)
    text = canonical_json(data).decode("utf-8")
    assert [value for value in person_pii(entities) if value in text] == []
    persons = [e for e in data["entities"] if e["type"] == "Person"]
    assert len(persons) == 2 and all(e["name"] == MASK for e in persons)
    assert all(e["id"].startswith("person:") for e in persons)
    # The claims point at the pseudonyms, so a sentence can still name the party.
    subjects = {claim["subject"] for claim in data["claims"]}
    assert {e["id"] for e in persons} <= subjects


def test_a_document_with_a_masked_value_is_never_sent(decisions, ontology):
    decision = decisions["SCEN-03"]
    real = mask_decision(decision, "reviewer", ontology).memo.entities
    person = next(e for e in real if e.entity_type == "Person")
    with pytest.raises(EgressRefused):
        check_egress({"name": person.legal_name}, decision, ontology)


def test_text_from_a_list_is_cut_and_cleaned():
    from ubo_sentinel.llm.egress import MAX_TEXT, clean

    assert clean({"a": ["x\x00y\n" + "z" * 500]}) == {"a": ["x y " + "z" * (MAX_TEXT - 4)]}


def test_sentences_that_follow_the_rules_are_accepted(decisions, ontology):
    found, asked = _annotate(decisions["SCEN-03"], ontology, good_prose)
    assert asked and found.status == "ACCEPTED" and found.faults == []
    assert found.egress == "analyst_masked" and found.model == MODEL
    assert found.usage["input_tokens"] == 10


@pytest.mark.parametrize(
    ("text", "fault"),
    [
        ("Halcyon Ridge Minerals is owned by sanctioned people.", "a name outside a slot"),
        ("Blocked owners hold 60 percent of {e:lei:FXS03TARGET000000000}.", "a digit outside"),
        ("{e:lei:FXS03TARGET000000000} is safe to onboard.", "a word that states an outcome"),
        ("Reviewer should escalate {e:lei:FXS03TARGET000000000}.", "a word that states"),
        ("{e:lei:NOT-IN-THE-MEMO} owns it.", "an entity slot the memo does not hold"),
        ("It holds {p:0.not_a_param}.", "a figure slot the memo does not hold"),
        ("It holds {p:99.pct}.", "a figure slot the memo does not hold"),
        ("The outcome is {rec:CLEAR}.", "a recommendation slot with a value"),
        ("See {x:1}.", "a slot of an unknown kind"),
        ("Open {e:lei:FXS03TARGET000000000 slot.", "a slot that is not closed"),
        ("word " * 80, "a sentence that is too long"),
    ],
)
def test_the_guardrail_refuses_what_the_memo_does_not_hold(decisions, ontology, text, fault):
    decision = decisions["SCEN-03"]
    data = prose_input(decision, ontology)
    output = good_prose(type("R", (), {"data": data})())
    output["sentences"].append({"claim": 0, "text": text})
    faults = check_slotted(output, decision, data)
    assert any(f.startswith(fault) for f in faults), faults


def test_a_person_s_real_name_is_refused_though_the_model_never_saw_it(decisions, ontology):
    decision = decisions["SCEN-03"]
    real = mask_decision(decision, "reviewer", ontology).memo.entities
    name = next(e.legal_name for e in real if e.entity_type == "Person")
    data = prose_input(decision, ontology)
    output = {"sentences": [{"claim": 0, "text": f"{name} owns it."}]}
    assert "a name outside a slot" in check_slotted(output, decision, data)


def test_the_summary_must_explain_every_claim_that_decides(decisions, ontology):
    decision = decisions["SCEN-03"]
    data = prose_input(decision, ontology)
    output = {"sentences": [{"claim": 1, "text": "The screen read {p:1.nodes_checked} parties."}]}
    faults = check_slotted(output, decision, data)
    assert "a claim the summary does not explain" in faults
    assert check_slotted({"sentences": []}, decision, data) == ["no sentences"]
    assert "a sentence tagged with a claim the memo does not hold" in check_slotted(
        {"sentences": [{"claim": 99, "text": "x"}]}, decision, data
    )


def test_a_rejected_or_unavailable_answer_changes_nothing(decisions, ontology):
    decision = decisions["SCEN-03"]
    rejected, _ = _annotate(decision, ontology, {"sentences": [{"claim": 0, "text": "safe"}]})
    assert rejected.status == "REJECTED" and rejected.faults
    unavailable, _ = llm_prose.annotate_prose(
        decision, ontology, FakeClient(fail=True), MODEL, 1000
    )
    assert unavailable.status == "UNAVAILABLE" and unavailable.output is None
    assert unavailable.faults == ["unavailable: no model here"]


def test_each_role_reads_the_slots_filled_as_it_may_see_them(decisions, ontology, entities):
    decision = decisions["SCEN-03"]
    found, _ = _annotate(decision, ontology, good_prose)
    analyst = render_prose(found.model_dump(mode="json"), decision, "analyst", ontology)
    reviewer = render_prose(found.model_dump(mode="json"), decision, "reviewer", ontology)
    pii = person_pii(entities)
    assert [v for v in pii if v in analyst] == [] and MASK in analyst
    assert any(v in reviewer for v in pii) and MASK not in reviewer
    assert "`ESCALATE`" in analyst and "`60.0000`" in analyst
    assert "{" not in analyst


def test_an_annotation_is_recorded_beside_the_decision_and_never_in_it(
    screened, decisions, ontology
):
    decision = decisions["SCEN-03"]
    log = AuditLog()
    found, _ = _annotate(decision, ontology, good_prose, log)
    assert annotations.record(log, ontology, found, "someone", "reviewer", decision.created_at)
    assert not annotations.record(log, ontology, found, "someone", "reviewer", decision.created_at)
    folded = log.decision(decision.decision_id)
    assert folded.llm_annotations["prose"]["annotation_id"] == found.annotation_id
    assert folded.canonical_payload() == decision.canonical_payload()
    assert folded.status == "RECOMMENDED"
    # Asked again for the same input, prompt and model: the record answers.
    client = FakeClient(good_prose)
    again, asked = llm_prose.annotate_prose(decision, ontology, client, MODEL, 1000, log=log)
    assert not asked and again == found and client.requests == []
    # The analyst's view keeps it: it was made from that view.
    assert "prose" in mask_decision(folded, "analyst", ontology).llm_annotations
    assert log.verify(ontology).intact
    assert replay_decision(log, ontology, decision.decision_id, screened[0]).identical


def test_a_rejected_annotation_is_in_the_log_and_not_shown(decisions, ontology):
    decision, log = decisions["SCEN-03"], AuditLog()
    rejected, _ = _annotate(decision, ontology, {"sentences": [{"claim": 0, "text": "safe"}]})
    annotations.record(log, ontology, rejected, "someone", "reviewer", decision.created_at)
    assert log.decision(decision.decision_id).llm_annotations == {}
    assert [r["event_type"] for r in log.events_of(decision.decision_id)][-1] == "ANNOTATED"


def test_the_log_refuses_an_annotation_of_nothing(decisions, ontology):
    decision, log = decisions["SCEN-03"], AuditLog()
    found, _ = _annotate(decision, ontology, good_prose)
    orphan = found.model_copy(update={"decision_id": "0000000000000000"})
    with pytest.raises(ValueError, match="not recommended"):
        annotations.record(log, ontology, orphan, "someone", "reviewer", decision.created_at)
    with pytest.raises(ValueError, match="without an id"):
        log.append(
            ontology=ontology,
            decision_id=decision.decision_id,
            event_type="ANNOTATED",
            actor="someone",
            role="reviewer",
            created_at=decision.created_at,
            payload={"annotation": found.model_dump(mode="json")},
        )
    with pytest.raises(PermissionError):
        annotations.record(log, ontology, found, "someone", "auditor", decision.created_at)


def test_a_review_after_an_annotation_keeps_both(decisions, ontology):
    from ubo_sentinel.audit.review import review_decision

    decision, log = decisions["SCEN-03"], AuditLog()
    found, _ = _annotate(decision, ontology, good_prose)
    annotations.record(log, ontology, found, "someone", "reviewer", decision.created_at)
    review_decision(
        log, ontology, decision.decision_id, action="approve", actor="checker",
        role="reviewer", now=lambda: decision.created_at,
    )  # fmt: skip
    folded = log.decision(decision.decision_id)
    assert folded.status == "APPROVED" and "prose" in folded.llm_annotations


# --- the command ---------------------------------------------------------------------------


def test_the_command_is_off_unless_its_flags_are_on(decisions, monkeypatch):
    monkeypatch.delenv("UBO_LLM", raising=False)
    result = cli("annotate", decisions["SCEN-03"].decision_id)
    assert result.exit_code == 1 and "UBO_LLM=on" in result.output
    assert all(r["event_type"] != "ANNOTATED" for r in AuditLog().records())


def test_the_command_records_and_shows_an_accepted_summary(
    decisions, llm_on, monkeypatch, entities
):
    client = FakeClient(good_prose)
    monkeypatch.setattr(annotate_cmd, "client_for", lambda config: client)
    decision_id = decisions["SCEN-03"].decision_id
    result = cli("annotate", decision_id, "--role", "analyst")
    assert result.exit_code == 0, result.output
    assert "ACCEPTED (recorded)" in result.output and "LLM-assisted" in result.output
    assert [v for v in person_pii(entities) if v in result.output] == []
    again = cli("annotate", decision_id, "--role", "reviewer")
    assert again.exit_code == 0 and "the model was not asked" in again.output
    assert len(client.requests) == 1
    assert any(v in again.output for v in person_pii(entities))


def test_the_command_reports_a_rejected_answer(decisions, llm_on, monkeypatch):
    bad = FakeClient({"sentences": [{"claim": 0, "text": "It is safe."}]})
    monkeypatch.setattr(annotate_cmd, "client_for", lambda config: bad)
    result = cli("annotate", decisions["SCEN-03"].decision_id)
    assert result.exit_code == 1 and "REJECTED" in result.output
    assert "a word that states an outcome" in result.output


def test_the_command_refuses_what_it_cannot_do(decisions, llm_on, monkeypatch):
    assert cli("annotate", "0000000000000000").exit_code == 2
    assert cli("annotate", decisions["SCEN-01"].decision_id, "--role", "auditor").exit_code == 1
    monkeypatch.setenv("UBO_LLM_MODE", "bogus")
    assert cli("annotate", decisions["SCEN-01"].decision_id).exit_code == 2


def test_replay_mode_answers_from_recordings_only(decisions, llm_on, monkeypatch, tmp_path):
    monkeypatch.setenv("UBO_LLM_MODE", "replay")
    monkeypatch.setenv("UBO_LLM_RECORDINGS", str(tmp_path / "rec"))
    decision_id = decisions["SCEN-03"].decision_id
    result = cli("annotate", decision_id)
    assert result.exit_code == 1 and "UNAVAILABLE" in result.output
    assert "No recorded response" in result.output
    events = [r for r in AuditLog().records() if r["event_type"] == "ANNOTATED"]
    assert json.dumps(events[0]["payload"]["annotation"]["status"]) == '"UNAVAILABLE"'
