"""The agent pipeline: from a query to a recorded decision."""

from datetime import UTC, datetime

import pytest
from conftest import PACK
from graphs import designated, edge, graph

from ubo_sentinel.agents import intake
from ubo_sentinel.agents.audit import DeterminismViolation, at_least_review
from ubo_sentinel.agents.base import PIPELINE_VERSION, BaseAgent, ScreenRequest, ScreenUsageError
from ubo_sentinel.agents.evaluator import run_pipeline_suite
from ubo_sentinel.agents.explainer import ExplainerAgent
from ubo_sentinel.agents.intake import fold_linked
from ubo_sentinel.agents.rule_engine import what_if
from ubo_sentinel.agents.supervisor import (
    PIPELINE,
    NeedsDisambiguation,
    Recommended,
    ScreenFailed,
    Supervisor,
)
from ubo_sentinel.agents.traversal import TraversalAgent
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.models import CandidateLink, EntityMatch, canonical_json
from ubo_sentinel.models.memo import Memo
from ubo_sentinel.rules.engine import RuleEngine

NOW = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
ACME = "lei:FXS01TARGET000000000"
CYPRUS, MALTA = "lei:FXS16TARGET000000000", "lei:FXS24TARGET000000000"
HOMONYM = "Silverline Commodities Ltd"
# SCEN-03: two designated persons hold 30 % each; two evidence paths.
HALCYON = "Halcyon Ridge Minerals Ltd"


@pytest.fixture
def screen(con, graph_t0, ontology):
    """Screen a query on the t0 graph. Keyword arguments go to the request or the supervisor."""

    def run(query="", *, agents=PIPELINE, audit=None, pack=None, **request):
        supervisor = Supervisor(ontology, audit or AuditLog(), now=lambda: NOW, agents=agents)
        request = {"snapshot": graph_t0} | request
        return supervisor.run(ScreenRequest(query=query, **request), con, pack)

    return run


def candidate(entity_id, confidence=1.0, match_type="NAME_EXACT", source="fx_registry", linked=()):
    return EntityMatch(
        entity_id=entity_id,
        confidence=confidence,
        match_type=match_type,
        legal_name=entity_id,
        matched_name=entity_id,
        entity_type="LegalEntity",
        status="ACTIVE",
        source=source,
        linked=[CandidateLink(entity_id=other, confidence=score) for other, score in linked],
    )


# --- the whole pipeline ----------------------------------------------------------------


def test_every_gold_scenario_passes_from_query_to_decision(con, graph_t0, ontology, gold):
    reports = run_pipeline_suite(Supervisor(ontology, AuditLog()), gold, graph_t0, con)
    assert len(reports) == len(gold) == 28
    assert [(r.scenario_id, r.diffs) for r in reports if not r.passed] == []
    assert sum(report.uncited_claims for report in reports) == 0
    assert sum(r.paths_found for r in reports) == sum(r.paths_expected for r in reports) > 0


def test_a_screen_is_a_recommended_decision_with_its_inputs(screen, graph_t0, con):
    outcome = screen("Acme Trading FZE")
    assert isinstance(outcome, Recommended) and outcome.is_new
    decision = outcome.decision
    assert (decision.recommendation, decision.status, decision.target_id) == (
        "CLEAR",
        "RECOMMENDED",
        ACME,
    )
    # The resolved set id, never the alias; the graph and the agents that made it.
    assert decision.snapshot_set_id == graph_t0
    assert len(decision.graph_digest) == 64
    assert decision.pipeline_version == PIPELINE_VERSION
    assert (decision.rule_pack_id, decision.rule_pack_hash) == (PACK.id, PACK.rule_pack_hash)
    assert (decision.max_depth, decision.created_at) == (PACK.max_depth, NOW)
    assert [step["agent"] for step in outcome.trace] == [agent.name for agent in PIPELINE]


def test_a_deeper_screen_is_another_decision(screen):
    shallow = screen("Kittiwake Packaging Ltd").decision
    deep = screen("Kittiwake Packaging Ltd", max_depth=7).decision
    assert (shallow.recommendation, deep.recommendation) == ("REVIEW", "ESCALATE")
    assert (shallow.max_depth, deep.max_depth) == (5, 7)
    assert shallow.decision_id != deep.decision_id


# --- intake ----------------------------------------------------------------------------


def test_intake_selects_alone_only_when_the_answer_is_not_in_doubt(screen):
    assert screen("Acme Trading FZE").resolution.method == "SOLE_NAME"
    by_lei = screen("some other name", lei="FXS01TARGET000000000")
    assert (by_lei.resolution.method, by_lei.decision.target_id) == ("IDENTIFIER", ACME)
    assert screen("FXS01TARGET000000000").resolution.method == "IDENTIFIER"

    asked = screen(HOMONYM)
    assert isinstance(asked, NeedsDisambiguation)
    assert [match.entity_id for match in asked.candidates] == [CYPRUS, MALTA]
    # Jurisdiction ranks the candidates; it does not choose one.
    ranked = screen(HOMONYM, jurisdiction="MT")
    assert [match.entity_id for match in ranked.candidates] == [MALTA, CYPRUS]


def test_the_analysts_pick_decides_between_homonyms(screen):
    first, second = screen(HOMONYM, pick=1), screen(HOMONYM, pick=2)
    assert (first.decision.target_id, first.decision.recommendation) == (CYPRUS, "CLEAR")
    assert (second.decision.target_id, second.decision.recommendation) == (MALTA, "ESCALATE")
    assert first.resolution.method == second.resolution.method == "ANALYST_PICK"
    with pytest.raises(ScreenUsageError, match="no candidate 3"):
        screen(HOMONYM, pick=3)


def test_a_target_id_skips_resolution(screen):
    outcome = screen(target_id=MALTA)
    assert (outcome.resolution.method, outcome.decision.recommendation) == ("TARGET_ID", "ESCALATE")
    # The same entity reached by a pick is the same decision.
    assert outcome.decision.decision_id == screen(HOMONYM, pick=2).decision.decision_id
    with pytest.raises(ScreenUsageError, match="No entity"):
        screen(target_id="lei:NOSUCHENTITY00000000")


def test_a_query_that_matches_nothing_is_never_clear(screen):
    outcome = screen("Zzyzx Nonexistent Ventures")
    decision = outcome.decision
    assert (decision.recommendation, decision.reasons) == ("REVIEW", ["ENTITY_NOT_FOUND"])
    assert (decision.target_id, outcome.resolution.method) == (None, "NOT_FOUND")
    # Without a target the normalised query names the decision.
    assert screen("zzyzx   NONEXISTENT ventures").decision.decision_id == decision.decision_id
    assert screen("Zzyzx Other Ventures").decision.decision_id != decision.decision_id
    assert [step["agent"] for step in outcome.trace] == [
        "Intake",
        "Explainer",
        "Guardrail",
        "Audit",
    ]


def test_two_records_of_one_company_are_one_candidate(screen):
    """SCEN-27: the registry's and the list's record match the name equally."""
    outcome = screen("Pendlewick Salt Refiners Ltd")
    assert isinstance(outcome, Recommended)
    target, duplicate = "lei:FXS27TARGET000000000", "fx_list:s27-dup"
    assert (outcome.decision.target_id, outcome.resolution.method) == (target, "SOLE_NAME")
    assert outcome.resolution.folded == {target: [duplicate]}
    # The list record can still be screened by its id, and gives another answer.
    assert screen(target_id=duplicate).decision.recommendation == "ESCALATE"


def test_linked_records_fold_into_the_registrys_whatever_the_order():
    listed = candidate("fx_list:a", source="fx_list", linked=[("lei:A", 0.95)])
    registry = candidate("lei:A", linked=[("fx_list:a", 0.95)])
    other = candidate("lei:B")
    for order in ([listed, registry, other], [other, registry, listed]):
        kept, folded = fold_linked(order, PACK.match_threshold)
        assert {match.entity_id for match in kept} == {"lei:A", "lei:B"}
        assert folded == {"lei:A": ["fx_list:a"]}
    # The group takes the place of its first member.
    assert [m.entity_id for m in fold_linked([listed, other, registry], 0.92)[0]] == [
        "lei:A",
        "lei:B",
    ]


def test_a_link_below_the_match_threshold_does_not_fold():
    listed = candidate("fx_list:a", source="fx_list", linked=[("lei:A", 0.85)])
    registry = candidate("lei:A", linked=[("fx_list:a", 0.85)])
    kept, folded = fold_linked([listed, registry], PACK.match_threshold)
    assert ([match.entity_id for match in kept], folded) == (["fx_list:a", "lei:A"], {})


@pytest.mark.parametrize(
    ("found", "method"),
    [
        # One strong name: weaker neighbours do not force a pick.
        ([candidate(ACME), candidate(CYPRUS, 0.80, "NAME_FUZZY")], "SOLE_NAME"),
        ([candidate(ACME, 0.93, "NAME_FUZZY")], "SOLE_NAME"),
        # Two strong names, or none: the analyst decides.
        ([candidate(ACME), candidate(CYPRUS, 0.95, "NAME_FUZZY")], None),
        ([candidate(ACME, 0.85, "NAME_FUZZY")], None),
        # A registration number without its authority may belong to two entities.
        ([candidate(ACME, 1.0, "REGISTRATION")], "IDENTIFIER"),
        ([candidate(ACME, 1.0, "REGISTRATION"), candidate(CYPRUS, 1.0, "REGISTRATION")], None),
    ],
)
def test_when_intake_selects(screen, monkeypatch, found, method):
    monkeypatch.setattr(intake.queries, "resolve_entity", lambda *args, **kwargs: found)
    outcome = screen("whatever")
    if method is None:
        assert isinstance(outcome, NeedsDisambiguation)
        assert outcome.candidates == found
    else:
        assert (outcome.resolution.method, outcome.decision.target_id) == (method, ACME)
        # What was not chosen is on record with the decision.
        assert outcome.resolution.candidates == found


# --- determinism -----------------------------------------------------------------------


def test_a_decision_does_not_depend_on_who_asked(screen, tmp_path):
    asked = {
        role: screen(HALCYON, role=role, actor=role, audit=AuditLog(tmp_path / role)).decision
        for role in ("analyst", "reviewer")
    }
    analyst, reviewer = asked["analyst"], asked["reviewer"]
    assert analyst.decision_id == reviewer.decision_id
    assert canonical_json(analyst.canonical_payload()) == canonical_json(
        reviewer.canonical_payload()
    )
    # The stored memo is unmasked for both: masking happens when a role is shown it.
    assert "Dmitri Volkanov" in canonical_json(analyst.memo_json).decode()


def test_the_same_inputs_return_the_decision_on_record(screen, graph_t0):
    log = AuditLog()
    first = screen("Acme Trading FZE", audit=log)
    again = screen("  acme TRADING fze ", audit=log, role="reviewer")
    by_alias = screen("Acme Trading FZE", audit=log, snapshot="fixtures")
    assert (first.is_new, again.is_new, by_alias.is_new) == (True, False, False)
    assert first.payload_sha256 == again.payload_sha256 == by_alias.payload_sha256
    assert again.decision == first.decision
    # One decision, one event: nothing was appended by the repeats.
    events = log.records()
    assert [event["event_type"] for event in events] == ["RECOMMENDED"]
    payload = events[0]["payload"]
    assert payload["payload_sha256"] == first.payload_sha256
    assert payload["request"]["query"] == "Acme Trading FZE"
    assert payload["resolution"]["method"] == "SOLE_NAME"
    assert payload["rule_pack"]["id"] == PACK.id and payload["trace"]


class _OtherDisclaimer(ExplainerAgent):
    """Agents that changed what a memo says without a new PIPELINE_VERSION."""

    def run(self, state, ctx):
        state = super().run(state, ctx)
        return state.with_(memo=state.memo.model_copy(update={"disclaimer": "Changed."}))


def swap(old, new):
    return tuple(new if agent is old else agent for agent in PIPELINE)


def test_one_id_never_stands_for_two_decisions(screen):
    log = AuditLog()
    screen("Acme Trading FZE", audit=log)
    with pytest.raises(DeterminismViolation, match="memo_json"):
        screen("Acme Trading FZE", audit=log, agents=swap(ExplainerAgent, _OtherDisclaimer))
    assert len(log.records()) == 1


# --- failing closed --------------------------------------------------------------------


class _Uncited(ExplainerAgent):
    def run(self, state, ctx):
        state = super().run(state, ctx)
        claims = [claim.model_copy(update={"citations": []}) for claim in state.memo.claims]
        return state.with_(memo=state.memo.model_copy(update={"claims": claims}))


def test_a_guardrail_failure_raises_a_recommendation_and_never_lowers_one(screen):
    agents = swap(ExplainerAgent, _Uncited)
    clear = screen("Acme Trading FZE", agents=agents).decision
    assert (clear.recommendation, clear.reasons) == ("REVIEW", ["GUARDRAIL_FAILED"])
    assert "uncited claim: COVERAGE" in clear.guardrail_warnings
    # What the rules concluded is still in the memo.
    assert clear.memo_json["recommendation"] == "CLEAR"

    escalate = screen(HALCYON, agents=agents).decision
    assert escalate.recommendation == "ESCALATE"
    assert escalate.reasons == ["DERIVED_50PCT", "GUARDRAIL_FAILED"]
    assert at_least_review("CLEAR") == at_least_review("REVIEW") == "REVIEW"
    assert at_least_review("ESCALATE") == "ESCALATE"


class _Broken(TraversalAgent):
    def run(self, state, ctx):
        raise RuntimeError("disk on fire")


def test_an_agent_that_raises_is_not_screened_and_is_not_cached(screen):
    log = AuditLog()
    failed = screen("Acme Trading FZE", audit=log, agents=swap(TraversalAgent, _Broken))
    assert isinstance(failed, ScreenFailed)
    assert (failed.agent, failed.recommendation, failed.reason) == (
        "Traversal",
        "REVIEW",
        "PIPELINE_ERROR",
    )
    assert "disk on fire" in failed.error
    event = log.records()[0]
    assert (event["event_type"], event["payload"]["agent"]) == ("SCREEN_FAILED", "Traversal")

    # The failure is on record; it is not the decision for those inputs.
    assert log.decision(event["decision_id"]) is None
    outcome = screen("Acme Trading FZE", audit=log)
    assert outcome.is_new and outcome.decision.recommendation == "CLEAR"
    assert outcome.decision.decision_id == event["decision_id"]
    assert [e["event_type"] for e in log.records()] == ["SCREEN_FAILED", "RECOMMENDED"]


def test_more_paths_than_the_pack_allows_is_a_review(screen):
    pack = PACK.model_copy(update={"max_paths": 1})
    outcome = screen(HALCYON, pack=pack)
    decision = outcome.decision
    assert (decision.recommendation, decision.reasons) == ("REVIEW", ["EVIDENCE_LIMIT_EXCEEDED"])
    assert decision.guardrail_warnings == [] and decision.blocked_set == []
    memo = Memo.model_validate(decision.memo_json)
    assert [action.code for action in memo.next_actions] == ["NARROW_SCREEN"]
    # The designations that were found are still shown.
    assert {match.effect for match in memo.matches} == {"BLOCKED"}
    # It is the pack that decided, so it is a decision like any other.
    assert not screen(HALCYON, pack=pack, audit=AuditLog()).is_new


def test_an_agent_run_out_of_order_fails_at_its_own_boundary(screen):
    out_of_order = (PIPELINE[1], PIPELINE[0], *PIPELINE[2:])
    failed = screen("Acme Trading FZE", agents=out_of_order)
    assert isinstance(failed, ScreenFailed)
    assert (failed.agent, failed.error) == (
        "Traversal",
        "ContractError: Traversal requires: target_id",
    )


class _Forgetful(BaseAgent):
    name = "Forgetful"
    provides = ("subgraph",)

    def run(self, state, ctx):
        return state


def test_an_agent_must_provide_what_it_declares(screen):
    failed = screen("Acme Trading FZE", agents=swap(TraversalAgent, _Forgetful))
    assert failed.error == "ContractError: Forgetful did not provide: subgraph"


def test_a_read_only_role_cannot_record_a_decision(screen):
    with pytest.raises(PermissionError, match="read-only"):
        screen("Acme Trading FZE", role="auditor")


# --- what would settle a review --------------------------------------------------------


def test_what_if_says_what_the_review_decides(screen):
    memo = Memo.model_validate(screen("Fairweather Bulk Carriers Ltd").decision.memo_json)
    assert (memo.recommendation, memo.what_if.if_confirmed, memo.what_if.if_rejected) == (
        "REVIEW",
        "ESCALATE",
        "CLEAR",
    )
    assert [(m.entity_id, m.designated_entity_id) for m in memo.what_if.contested] == [
        ("lei:FXS14OWNER0000000000", "fx_list:s14-listed")
    ]
    # Nothing is unsettled where every match is the node's own.
    assert Memo.model_validate(screen(HALCYON).decision.memo_json).what_if is None


def test_what_if_shows_a_match_that_does_not_matter():
    engine = RuleEngine(PACK)
    subgraph = graph("T", [edge("O", "T", 5)])
    matches = [designated("O", by="L", confidence=0.8)]
    result = engine.evaluate(subgraph, matches)
    assert result.recommendation == "REVIEW"
    found = what_if(engine, subgraph, matches, result)
    # A 5 % owner: confirmed or rejected, the target is clear.
    assert (found.if_confirmed, found.if_rejected) == ("CLEAR", "CLEAR")

    contested = [designated("O", by="L", match_type="REGISTRATION")]
    majority = graph("T", [edge("O", "T", 60)])
    found = what_if(engine, majority, contested, engine.evaluate(majority, contested))
    assert (found.if_confirmed, found.if_rejected) == ("ESCALATE", "CLEAR")
    assert what_if(engine, subgraph, [], engine.evaluate(subgraph, [])) is None
