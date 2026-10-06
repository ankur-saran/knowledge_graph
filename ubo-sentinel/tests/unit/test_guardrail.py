"""The guardrails, tested by breaking a memo that holds and seeing each break caught."""

from datetime import date

import pytest
from conftest import PACK
from test_agents import HALCYON, NOW

from ubo_sentinel.agents.base import PipelineState, ScreenContext, ScreenRequest
from ubo_sentinel.agents.explainer import render_memo
from ubo_sentinel.agents.guardrail import check_memo, check_prose, check_render
from ubo_sentinel.agents.supervisor import PIPELINE, Supervisor
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.graph.views import MASK, mask_decision, masked_values
from ubo_sentinel.models.memo import Citation, Claim, MemoEntity

REVIEW = "Fairweather Bulk Carriers Ltd"
PII = ["Dmitri Volkanov", "Дмитрий Волканов", "Yusuf Al-Qahdari", "يوسف القحدري"]
PERSON = "fx_list:s03-owner-a"


@pytest.fixture
def screened(con, graph_t0, ontology):
    """Run the agents up to the Explainer: the state the Guardrail Agent is handed."""

    def run(query):
        with GraphStore.open(graph_t0, con) as store:
            ctx = ScreenContext(store=store, pack=PACK, ontology=ontology, now=lambda: NOW)
            state = PipelineState(request=ScreenRequest(query=query, snapshot=graph_t0))
            for agent in PIPELINE[:5]:
                if state.halt is None or agent.runs_after_halt:
                    state = agent().run(state, ctx)
            return state, store

    return run


def changed(memo, **fields):
    return memo.model_copy(update=fields)


def with_claim(memo, code, **fields):
    """The memo with its first claim of `code` changed."""
    index = next(i for i, claim in enumerate(memo.claims) if claim.code == code)
    claims = list(memo.claims)
    claims[index] = claims[index].model_copy(update=fields)
    return changed(memo, claims=claims)


def recited(claim, **provenance):
    first = claim.citations[0]
    moved = first.model_copy(update={"provenance": first.provenance.model_copy(update=provenance)})
    return [moved, *claim.citations[1:]]


# --- the decision guardrail ------------------------------------------------------------


@pytest.mark.parametrize(
    "query", [HALCYON, REVIEW, "Acme Trading FZE", "Zzyzx Nonexistent Ventures"]
)
def test_a_memo_the_explainer_built_holds(screened, query):
    state, store = screened(query)
    assert check_memo(state.memo, state, store) == []


def test_an_uncited_claim_is_caught(screened):
    state, store = screened(HALCYON)
    broken = with_claim(state.memo, "BLOCKED_BY_OWNERSHIP", citations=[])
    assert check_memo(broken, state, store) == ["uncited claim: BLOCKED_BY_OWNERSHIP"]
    bare = Claim(code="DATA_GAP", subject_id=state.target_id, params={"gap": "LAPSED_LEI"})
    added = changed(state.memo, claims=[*state.memo.claims, bare])
    # A claim that lacks what its sentence needs cannot be rendered either.
    faults = check_memo(added, state, store)
    assert {"the memo cannot be rendered", "uncited claim: DATA_GAP"} <= set(faults)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recommendation", "CLEAR"),
        ("reasons", ["DIRECT_DESIGNATION"]),
        ("blocked_set", ["lei:FXS03TARGET000000000"]),
        ("possibly_blocked_set", [PERSON]),
        ("aggregate_pct", "30"),
        ("paths", []),
    ],
)
def test_a_memo_that_disagrees_with_the_rules_is_caught(screened, field, value):
    state, store = screened(HALCYON)
    broken = state.memo.model_validate(state.memo.model_dump() | {field: value})
    assert f"the memo disagrees with the rules on {field}" in check_memo(broken, state, store)


def test_a_citation_to_a_record_that_does_not_exist_is_caught(screened):
    state, store = screened(HALCYON)
    claim = next(c for c in state.memo.claims if c.code == "DESIGNATED")
    fault = ["a citation names a record that does not exist"]
    for moved in ({"source_record_id": "no-such-record"}, {"snapshot_id": "0" * 16}):
        broken = with_claim(state.memo, "DESIGNATED", citations=recited(claim, **moved))
        assert check_memo(broken, state, store) == fault
    # A changed date does not hide a record that is there.
    dated = with_claim(state.memo, "DESIGNATED", citations=recited(claim, as_of=date(2020, 1, 1)))
    assert check_memo(dated, state, store) == []


def test_a_citation_to_another_snapshot_is_caught(screened):
    state, store = screened("Acme Trading FZE")
    elsewhere = [Citation(kind="dataset", ref_id="0" * 16)]
    broken = with_claim(state.memo, "NO_DESIGNATION_FOUND", citations=elsewhere)
    faults = check_memo(broken, state, store)
    assert "a citation names a snapshot outside the screened set" in faults


def test_an_entity_the_memo_does_not_describe_is_caught(screened):
    state, store = screened(HALCYON)
    without = [entity for entity in state.memo.entities if entity.id != PERSON]
    faults = check_memo(changed(state.memo, entities=without), state, store)
    assert "the memo refers to an entity or edge it does not describe" in faults


def test_a_name_that_is_not_the_records_is_caught_in_the_text(screened):
    """The table is what is rendered: a wrong name is a wrong memo unless the text shows it."""
    state, _ = screened(HALCYON)
    text = render_memo(state.memo)
    assert check_prose(text, state.memo) == []
    renamed = [
        MemoEntity.model_validate(entity.model_dump() | {"legal_name": "Someone Else"})
        if entity.id == PERSON
        else entity
        for entity in state.memo.entities
    ]
    assert check_prose(text, changed(state.memo, entities=renamed)) == [
        "the memo text has a name that the memo does not hold"
    ]


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ("hold 60 % of it", "hold 61 % of it", "number"),
        ("`OWNS` 30 %", "`OWNS` 33.5 %", "number"),
        ("**Dmitri Volkanov**", "**Dmitri Volkov**", "name"),
        ("`fx_list:s03-owner-a`", "`fx_list:s03-owner-z`", "id or code"),
        ("`ESCALATE`", "`CLEAR`", "id or code"),
        ("as of 2026-09-15", "as of 2026-09-16", "date"),
    ],
)
def test_text_that_states_what_the_memo_does_not_hold_is_caught(screened, old, new, kind):
    state, _ = screened(HALCYON)
    text = render_memo(state.memo)
    assert old in text
    article = "an" if kind == "id or code" else "a"
    assert check_prose(text.replace(old, new), state.memo) == [
        f"the memo text has {article} {kind} that the memo does not hold"
    ]


def test_a_warning_never_holds_a_value(screened):
    state, store = screened(HALCYON)
    broken = changed(
        with_claim(state.memo, "DESIGNATED", citations=[]),
        entities=[e for e in state.memo.entities if e.id != PERSON],
        recommendation="CLEAR",
    )
    warnings = " ".join(check_memo(broken, state, store))
    assert len(check_memo(broken, state, store)) >= 3
    assert not any(value in warnings for value in [*PII, PERSON, "s03-owner-a", "60"])


# --- the render guardrail --------------------------------------------------------------


@pytest.fixture
def decision(con, graph_t0, ontology):
    outcome = Supervisor(ontology, now=lambda: NOW).run(
        ScreenRequest(query=HALCYON, snapshot=graph_t0), con
    )
    return outcome.decision


def test_what_an_analyst_is_shown_holds_nothing_masked(decision, ontology):
    hidden = masked_values(decision, "analyst", ontology)
    assert set(PII) <= set(hidden) and {PERSON, "s03-owner-a", "s03-des-a"} <= set(hidden)
    assert masked_values(decision, "reviewer", ontology) == []

    view = mask_decision(decision, "analyst", ontology)
    for text in (view.model_dump_json(), render_memo(view.memo)):
        assert check_render(text, decision, "analyst", ontology) == []
        assert not any(value in text for value in hidden)
        assert MASK in text


def test_a_leak_into_an_analysts_output_is_caught(decision, ontology):
    plain = mask_decision(decision, "reviewer", ontology)
    leak = ["the output shows a value that is masked for this role"]
    # The unmasked memo, shown to an analyst by mistake.
    assert check_render(render_memo(plain.memo), decision, "analyst", ontology) == leak
    assert check_render(plain.model_dump_json(), decision, "analyst", ontology) == leak
    masked = render_memo(mask_decision(decision, "analyst", ontology).memo)
    for value in (PII[1], PERSON, "s03-des-a"):
        assert check_render(f"{masked}\nSee also {value}.", decision, "analyst", ontology) == leak
    # The same text is what a reviewer may see.
    assert check_render(render_memo(plain.memo), decision, "reviewer", ontology) == []
