"""The memo: claims that cite, built from what the rules concluded, and its rendering."""

from datetime import date

import pytest
from conftest import PACK
from graphs import PROVENANCE, designated, edge, graph, link
from hypothesis import given, settings
from test_agents import HALCYON, NOW
from test_rules_hypothesis import PACKS, cases

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.explainer import build_memo, format_pct, render_memo
from ubo_sentinel.agents.guardrail import check_prose
from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.models import Entity
from ubo_sentinel.models.memo import DISCLAIMER, Citation, Claim, DatasetRef, Memo
from ubo_sentinel.rules.engine import RuleEngine

DATASETS = [
    DatasetRef(
        dataset="hand",
        snapshot_id="s1",
        row_count=12,
        as_of_min=date(2026, 1, 1),
        as_of_max=date(2026, 1, 31),
    )
]
LISTED = Entity(
    id="L", legal_name="Listed Party", status="ACTIVE", entity_type="Person", provenance=PROVENANCE
)


def memo_of(subgraph, matches, pack=PACK, referenced=(), **fields):
    result = RuleEngine(pack).evaluate(subgraph, matches)
    return build_memo(
        pack=pack,
        max_depth=subgraph.max_depth_up,
        datasets=DATASETS,
        subgraph=subgraph,
        matches=matches,
        rule_result=result,
        referenced=list(referenced),
        **fields,
    ), result


@pytest.fixture
def memo(con, graph_t0, ontology):
    def screened(query, **request):
        supervisor = Supervisor(ontology, now=lambda: NOW)
        outcome = supervisor.run(ScreenRequest(query=query, snapshot=graph_t0, **request), con)
        return Memo.model_validate(outcome.decision.memo_json)

    return screened


def claims(memo, code):
    return [claim for claim in memo.claims if claim.code == code]


# --- what a memo holds -----------------------------------------------------------------


def test_a_memo_states_the_rules_trace_and_cites_every_step():
    subgraph = graph("T", [edge("A", "T", 30), edge("B", "T", 30), edge("C", "T", 40)])
    memo, result = memo_of(subgraph, [designated("A"), designated("B")])

    assert (memo.recommendation, memo.reasons) == ("ESCALATE", ["DERIVED_50PCT"])
    assert memo.blocked_set == result.blocked_set == ["A", "B", "T"]
    assert str(memo.aggregate_pct) == "60.0000"
    assert memo.paths == result.paths and len(memo.paths) == 2

    # The seeds cite their designations; the derived block cites the stakes that add up.
    seeds = claims(memo, "DESIGNATED")
    assert [claim.subject_id for claim in seeds] == ["A", "B"]
    assert all([c.kind for c in claim.citations] == ["sanction"] for claim in seeds)
    derived = claims(memo, "BLOCKED_BY_OWNERSHIP")[0]
    assert (derived.subject_id, derived.params) == ("T", {"aggregate_pct": "60.0000", "round": 1})
    stakes = {subgraph.edges[i].id for i in range(3) if subgraph.edges[i].subject_id in "AB"}
    assert {c.ref_id for c in derived.citations} == stakes
    assert {c.ref_id for c in claims(memo, "DIRECT_AGGREGATE")[0].citations} == stakes
    reason = claims(memo, "REASON")[0]
    assert (reason.params, len(reason.citations)) == (
        {"pct": "60.0000", "reason": result.reasons[0]},
        2,
    )

    # Names are in one table; the unblocked owner is in it, and so is every edge checked.
    assert [entity.id for entity in memo.entities] == ["A", "B", "C", "T"]
    assert {entity.id: entity.depth for entity in memo.entities}["T"] == 0
    assert len(memo.edges) == 3 and memo.edge(subgraph.edges[0].id).lower_id == "T"
    assert memo.uncited() == [] and memo.dangling() == []
    assert memo.disclaimer == DISCLAIMER


def test_a_clear_says_what_was_checked_and_cites_the_snapshots():
    memo, _ = memo_of(graph("T", [edge("A", "T", 60)]), [designated("A", active=False)])
    assert (memo.recommendation, memo.reasons) == ("CLEAR", [])
    found = claims(memo, "NO_DESIGNATION_FOUND")[0]
    assert found.params == {"nodes_checked": 2}
    assert found.citations == [Citation(kind="dataset", ref_id="s1")]
    coverage = claims(memo, "COVERAGE")[0]
    assert coverage.params == {
        "depth_reached": 1,
        "edges_checked": 1,
        "max_depth": 2,
        "nodes_checked": 2,
    }
    assert memo.coverage.datasets == DATASETS and memo.coverage.sources == ["hand"]
    # The designation that was found is shown, with what it did: nothing.
    assert [(m.entity_id, m.is_active, m.effect) for m in memo.matches] == [("A", False, "NONE")]
    assert memo.next_actions == [] and memo.what_if is None


def test_a_gap_is_a_cited_claim_and_a_next_action():
    subgraph = graph(
        "T",
        [edge("A", "T", 60), edge("P", "A", rel="CONSOLIDATED_BY")],
        statuses={"A": "LAPSED"},
        links=[link("T", "X", confidence=0.9)],
    )
    memo, result = memo_of(subgraph, [designated("P", by="L", confidence=0.8)], referenced=[LISTED])
    assert memo.recommendation == "REVIEW"
    gaps = {(claim.params["gap"], claim.subject_id): claim for claim in claims(memo, "DATA_GAP")}
    assert {code for code, _ in gaps} == {
        "LAPSED_LEI",
        "NULL_PCT_EDGE",
        "POSSIBLE_DUPLICATE",
        "REVIEW_BAND_MATCH",
    }
    # A gap cites the record that shows it: the sanction record of a review-band match.
    assert [c.kind for c in gaps["REVIEW_BAND_MATCH", "P"].citations] == ["sanction"]
    assert [c.kind for c in gaps["LAPSED_LEI", "A"].citations] == ["node"]
    assert all(claim.params["decision_relevant"] for claim in gaps.values())

    actions = {(action.code, action.subject_id): action.params for action in memo.next_actions}
    assert actions["SCREEN_LINKED_RECORD", "T"] == {"linked_id": "X"}
    assert actions["COMPARE_MATCH_RECORDS", "P"] == {"designated_id": "L"}
    assert ("REFRESH_LEI_RECORD", "A") in actions and len(actions) == 4
    assert {a.code for a in memo.next_actions} >= {"OBTAIN_SHAREHOLDING"}
    # The party a designation belongs to is named, though it is outside the graph.
    assert memo.entity("L").depth is None
    # "X" was not handed in as a referenced entity, and the memo says so.
    assert memo.dangling() == ["X"]
    assert len(result.data_gaps) == len(gaps)


def test_only_a_gap_that_decides_asks_for_action():
    subgraph = graph("T", [edge("A", "T", 60)], statuses={"A": "LAPSED"})
    memo, _ = memo_of(subgraph, [])
    assert [c.params["decision_relevant"] for c in claims(memo, "DATA_GAP")] == [False]
    assert (memo.recommendation, memo.next_actions) == ("CLEAR", [])


def test_a_memo_is_the_same_whatever_the_order_of_its_inputs():
    edges = [edge("A", "T", 30), edge("B", "T", 30), edge("C", "B", 80)]
    matches = [designated("A"), designated("C")]
    one, _ = memo_of(graph("T", edges), matches)
    two, _ = memo_of(graph("T", edges[::-1]), matches[::-1])
    assert one == two
    assert Memo.model_validate(one.model_dump(mode="json")) == one


def test_the_memo_of_a_screen_names_what_it_refers_to(memo):
    review = memo("Fairweather Bulk Carriers Ltd")
    listed = review.entity("fx_list:s14-listed")
    assert (listed.legal_name, listed.depth) == ("Petrakos Aegean Navigators", None)
    assert review.dangling() == [] and review.uncited() == []
    duplicate = memo("Pendlewick Salt Refiners Ltd")
    assert duplicate.entity("fx_list:s27-dup").depth is None
    assert [(a.code, a.params) for a in duplicate.next_actions] == [
        ("SCREEN_LINKED_RECORD", {"linked_id": "fx_list:s27-dup"})
    ]
    # The datasets of the set, without a wall-clock value.
    assert [d.dataset for d in review.coverage.datasets] == [
        "entities",
        "relationships",
        "repex",
        "sanctions",
    ]
    assert "ingested_at" not in str(review.model_dump(mode="json"))


def test_a_query_that_matched_nothing_has_a_memo_too(memo):
    found = memo("Zzyzx  Nonexistent Ventures")
    assert (found.target_id, found.query_norm) == (None, "zzyzx nonexistent ventures")
    assert (found.recommendation, found.reasons) == ("REVIEW", ["ENTITY_NOT_FOUND"])
    assert [(claim.code, claim.subject_id) for claim in found.claims] == [("REASON", None)]
    assert found.uncited() == [] and found.entities == []
    assert [action.code for action in found.next_actions] == ["CHECK_QUERY"]


# --- rendering -------------------------------------------------------------------------


def test_the_rendered_memo_says_what_the_memo_holds(memo):
    found = memo(HALCYON)
    text = render_memo(found)
    assert "Target: **Halcyon Ridge Minerals Ltd** (`lei:FXS03TARGET000000000`)" in text
    assert "Rule recommendation: `ESCALATE` (`DERIVED_50PCT`)" in text
    assert "blocked owners hold 60 % of it. [`fx_list:s03-rel-1` as of 2026-09-15;" in text
    assert "**Dmitri Volkanov** (`fx_list:s03-owner-a`) `OWNS` 30 % → **Halcyon" in text
    assert text.rstrip().endswith(DISCLAIMER)
    assert check_prose(text, found) == []
    # Rendering reads nothing but the memo.
    assert render_memo(Memo.model_validate(found.model_dump(mode="json"))) == text


def test_every_section_renders_for_the_scenario_that_needs_it(memo):
    review = render_memo(memo("Fairweather Bulk Carriers Ltd"))
    for heading in ("## Why", "## Evidence paths", "## Designations", "## Data gaps"):
        assert heading in review
    assert "matched to **Petrakos Aegean Navigators** (`fx_list:s14-listed`) at 0.88" in review
    assert "If confirmed, the rules recommend `ESCALATE`; if rejected, `CLEAR`." in review
    assert "`COMPARE_MATCH_RECORDS` for **Petrakis Aegean Navigation SA**" in review

    clear = render_memo(memo("Acme Trading FZE"))
    assert "No active designation reaches any of the 3 entities checked." in clear
    assert "## Evidence paths" not in clear and "## What would settle this" not in clear
    assert "reached depth 1 of at most 5" in clear

    assert "`zzyzx nonexistent ventures` matched no entity" in render_memo(
        memo("Zzyzx Nonexistent Ventures")
    )
    unknown = render_memo(memo("Zephyrine Logistics BV"))
    assert "`CONSOLIDATED_BY` ? % →" in unknown and "presumed a majority" in unknown
    deep = render_memo(memo("Kittiwake Packaging Ltd"))
    assert "`RESCREEN_DEEPER`" in deep and "the screen stopped at 5 hops" in deep


def test_percentages_are_printed_without_trailing_zeros():
    assert [format_pct(value) for value in ("60.0000", "49.9999", "0.5000", None)] == [
        "60",
        "49.9999",
        "0.5",
        "?",
    ]


# --- properties ------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(cases(), PACKS)
def test_any_graph_gives_a_memo_in_which_every_claim_is_cited(case, pack):
    subgraph, matches = case
    memo, result = memo_of(subgraph, matches, pack, referenced=[LISTED])
    assert memo.uncited() == []
    assert memo.dangling() == []
    assert all(claim.citations for claim in memo.claims)
    assert Memo.model_validate(memo.model_dump(mode="json")) == memo
    assert (memo.recommendation, memo.reasons) == (result.recommendation, result.reasons)
    # Every reason and every gap of the result is a claim.
    assert sorted(c.params["reason"] for c in claims(memo, "REASON")) == sorted(
        finding.code for finding in result.findings
    )
    assert len(claims(memo, "DATA_GAP")) == len(result.data_gaps)
    assert check_prose(render_memo(memo), memo) == []


def test_a_claim_without_a_citation_is_counted():
    memo, _ = memo_of(graph("T", [edge("A", "T", 60)]), [designated("A")])
    bare = Claim(code="DATA_GAP", subject_id="A", params={"gap": "LAPSED_LEI"})
    assert memo.model_copy(update={"claims": [*memo.claims, bare]}).uncited() == ["DATA_GAP A"]
