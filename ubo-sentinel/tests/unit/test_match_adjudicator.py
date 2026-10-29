"""Review-band matches (Step 13.4): the match card, and a model's advisory reading of it."""

import pytest
from e2e import cli
from llm_fakes import FakeClient, gold_decisions

from ubo_sentinel.agents import match_adjudicator as adjudicator
from ubo_sentinel.agents.match_adjudicator import (
    adjudication_input,
    check_assessments,
    deciding,
    match_cards,
)
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli import annotate_cmd
from ubo_sentinel.graph.views import MASK
from ubo_sentinel.llm import annotation as annotations
from ubo_sentinel.models.memo import Memo

MODEL = "claude-haiku-4-5"


@pytest.fixture
def decisions(fresh_con, ontology, gold):
    return gold_decisions(fresh_con, ontology, gold, ["SCEN-14", "SCEN-29", "SCEN-03"])[1]


def good_answer(request):
    return {
        "assessments": [
            {
                "key": card["key"],
                "assessment": "LIKELY_DIFFERENT",
                "evidence_fields": ["name_score", "shared_name_tokens"],
                "rationale": "The names share one token and the score sits in the review band.",
            }
            for card in request.data["cards"]
        ]
    }


def test_a_card_lays_the_two_records_side_by_side(decisions, ontology):
    (card,) = match_cards(decisions["SCEN-14"], "analyst", ontology)
    assert card["key"] == "lei:FXS14OWNER0000000000|fx_list:s14-listed"
    assert card["a"]["names"] == ["Petrakis Aegean Navigation SA"]
    assert card["b"]["names"] == ["Petrakos Aegean Navigators"]
    assert card["shared_name_tokens"] == ["aegean"]
    assert card["jurisdictions_agree"] is True and card["types_agree"] is True
    assert card["lei_conflict"] is None and card["match_type"] == "FUZZY_NAME"
    assert 0.85 <= card["name_score"] < 0.92


def test_only_a_match_that_decides_is_sent(decisions, ontology):
    assert deciding(decisions["SCEN-14"]) and deciding(decisions["SCEN-29"])
    assert not deciding(decisions["SCEN-03"])
    client = FakeClient(good_answer)
    found, asked = adjudicator.annotate_matches(decisions["SCEN-03"], ontology, client, MODEL, 1000)
    assert found is None and not asked and client.requests == []


def test_a_card_about_a_person_is_never_sent(decisions, ontology, monkeypatch):
    decision = decisions["SCEN-14"]
    memo = Memo.model_validate(decision.memo_json)
    designated = memo.what_if.contested[0].designated_entity_id
    entities = [
        e.model_copy(update={"entity_type": "Person"}) if e.id == designated else e
        for e in memo.entities
    ]
    as_person = decision.model_copy(
        update={"memo_json": memo.model_copy(update={"entities": entities}).model_dump(mode="json")}
    )
    data = adjudication_input(as_person, ontology)
    assert data["cards"] == []
    found, asked = adjudicator.annotate_matches(as_person, ontology, FakeClient(), MODEL, 1000)
    assert found is None and not asked
    # The reviewer's card shows the person; the analyst's does not.
    assert MASK in str(match_cards(as_person, "analyst", ontology))
    assert MASK not in str(match_cards(as_person, "reviewer", ontology))


def test_an_assessment_that_follows_the_rules_is_accepted(decisions, ontology):
    found, asked = adjudicator.annotate_matches(
        decisions["SCEN-14"], ontology, FakeClient(good_answer), MODEL, 1000
    )
    assert asked and found.status == "ACCEPTED", found.faults
    assert found.output["assessments"][0]["assessment"] == "LIKELY_DIFFERENT"
    assert found.output["not_sent"] == []


@pytest.mark.parametrize(
    ("change", "fault"),
    [
        ({"key": "x|y"}, "an assessment of a match that was not asked about"),
        ({"assessment": "MAYBE"}, "an assessment that is not one of the three"),
        ({"evidence_fields": ["vibes"]}, "evidence that is not a card field"),
        ({"evidence_fields": []}, "evidence that is not a card field"),
        ({"rationale": "Scores 88 percent."}, "a digit in a rationale"),
        ({"rationale": "Safe to clear."}, "a word that states an outcome"),
        ({"rationale": "This should be blocked."}, "a word that states an outcome"),
        ({"rationale": "Petrakis Aegean Navigation is the same."}, "a name in a rationale"),
        ({"rationale": "x" * 500}, "a rationale that is too long"),
    ],
)
def test_the_guardrail_refuses_what_the_card_does_not_hold(decisions, ontology, change, fault):
    data = adjudication_input(decisions["SCEN-14"], ontology)
    answer = good_answer(type("R", (), {"data": data})())
    answer["assessments"][0] |= change
    assert fault in check_assessments(answer, data)


def test_every_match_needs_one_assessment(decisions, ontology):
    data = adjudication_input(decisions["SCEN-14"], ontology)
    assert check_assessments({"assessments": []}, data) == ["a match left without an assessment"]
    twice = good_answer(type("R", (), {"data": data})())
    twice["assessments"] *= 2
    assert "two assessments of one match" in check_assessments(twice, data)
    assert check_assessments({}, data) == ["no assessments"]


def test_an_assessment_changes_nothing_in_the_decision(decisions, ontology):
    decision, log = decisions["SCEN-14"], AuditLog()
    found, _ = adjudicator.annotate_matches(
        decision, ontology, FakeClient(good_answer), MODEL, 1000
    )
    annotations.record(log, ontology, found, "someone", "reviewer", decision.created_at)
    folded = log.decision(decision.decision_id)
    assert folded.recommendation == "REVIEW" and folded.status == "RECOMMENDED"
    assert folded.canonical_payload() == decision.canonical_payload()
    assert folded.llm_annotations["match"]["status"] == "ACCEPTED"


def test_the_command_shows_the_cards_with_every_flag_off(decisions, monkeypatch):
    monkeypatch.delenv("UBO_LLM", raising=False)
    result = cli("annotate", decisions["SCEN-14"].decision_id, "--feature", "match")
    assert result.exit_code == 0, result.output
    assert "Match cards (code, not a model)" in result.output
    assert "Petrakos Aegean Navigators" in result.output and "UBO_LLM=on" in result.output
    none = cli("annotate", decisions["SCEN-03"].decision_id, "--feature", "match")
    assert none.exit_code == 0 and "No unsettled name match" in none.output


def test_the_command_records_an_assessment(decisions, monkeypatch):
    for name in ("UBO_LLM", "UBO_LLM_MATCH"):
        monkeypatch.setenv(name, "on")
    monkeypatch.setattr(annotate_cmd, "client_for", lambda config: FakeClient(good_answer))
    result = cli("annotate", decisions["SCEN-14"].decision_id, "--feature", "match")
    assert result.exit_code == 0, result.output
    assert "LIKELY_DIFFERENT (on name_score, shared_name_tokens)" in result.output
    assert "LLM-assisted" in result.output


def test_review_shows_the_cards_and_an_accepted_assessment(decisions, ontology):
    from ubo_sentinel.ui import panels

    decision, log = decisions["SCEN-14"], AuditLog()
    shown = cli("review", decision.decision_id, "--role", "reviewer")
    assert shown.exit_code == 0 and "Match cards (code, not a model)" in shown.output
    assert "LLM annotation" not in shown.output
    found, _ = adjudicator.annotate_matches(
        decision, ontology, FakeClient(good_answer), MODEL, 1000
    )
    annotations.record(log, ontology, found, "someone", "reviewer", decision.created_at)
    shown = cli("review", decision.decision_id, "--role", "reviewer")
    assert "LLM annotation (match):" in shown.output and "LIKELY_DIFFERENT" in shown.output
    folded = log.decision(decision.decision_id)
    assert "LIKELY_DIFFERENT" in panels.advisory(folded, "reviewer", ontology)
    assert panels.advisory(decisions["SCEN-03"], "reviewer", ontology) is None
