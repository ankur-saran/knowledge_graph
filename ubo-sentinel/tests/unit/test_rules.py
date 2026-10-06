"""The rule pack, the rule engine and the evaluator."""

from decimal import Decimal

import pytest
import yaml
from conftest import PACK, T1, build_graph
from graphs import designated, edge, graph, link, over_100, reporting_exception
from pydantic import ValidationError
from typer.testing import CliRunner

from ubo_sentinel.cli.app import app
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models import EffectiveExposure
from ubo_sentinel.pipeline.entity_linking import LINK_FLOOR
from ubo_sentinel.rules import pack as pack_module
from ubo_sentinel.rules.engine import PathLimitExceeded, RuleEngine
from ubo_sentinel.rules.evaluator import compare, run_rules_suite, screen
from ubo_sentinel.rules.pack import (
    DEFAULT_PACK,
    RulePack,
    UnknownRulePack,
    load_rule_pack,
    pack_path,
)

runner = CliRunner()


def evaluate(subgraph, matches=(), **changes):
    """Run the OFAC pack, with `changes` to its keys, over a hand-built subgraph."""
    pack = RulePack.model_validate({**PACK.model_dump(), **changes})
    return RuleEngine(pack).evaluate(subgraph, list(matches))


def gaps_of(result):
    return {(gap.code, gap.subject_id, gap.decision_relevant) for gap in result.data_gaps}


# --- the rule pack ----------------------------------------------------------------------


def test_the_shipped_pack_loads():
    assert PACK.id == "ofac_50pct_v1"
    assert PACK.ownership_threshold_pct == Decimal(50) and PACK.threshold_operator == ">="
    assert PACK.review_band_low >= LINK_FLOOR
    assert len(PACK.rule_pack_hash) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown_key": 1},
        # Silver stores no link below the floor, so the band would be silently empty.
        {"review_band_low": LINK_FLOOR - 0.01},
        {"review_band_low": 0.95},
        {"near_miss_pct": 50},
        {"threshold_operator": "=="},
        {"gap_scope": "none"},
        {"ownership_threshold_pct": 0},
        {"max_depth": 0},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_a_malformed_pack_is_rejected(changes):
    with pytest.raises(ValidationError):
        RulePack.model_validate({**PACK.model_dump(), **changes})


def test_a_pack_needs_every_key():
    for key in RulePack.model_fields:
        data = PACK.model_dump()
        del data[key]
        with pytest.raises(ValidationError):
            RulePack.model_validate(data)


def test_the_pack_hash_follows_values_not_formatting(tmp_path, monkeypatch):
    data = yaml.safe_load(pack_path(DEFAULT_PACK).read_text(encoding="utf-8"))
    reordered = tmp_path / "reordered.yaml"
    reordered.write_text(
        "# a comment\n" + yaml.safe_dump(dict(reversed(data.items()))), encoding="utf-8"
    )
    assert load_rule_pack(reordered).rule_pack_hash == PACK.rule_pack_hash

    changed = RulePack.model_validate({**PACK.model_dump(), "near_miss_pct": 41})
    assert changed.rule_pack_hash != PACK.rule_pack_hash

    # A change to the engine's logic is a change to what the pack concludes.
    before = PACK.rule_pack_hash
    monkeypatch.setattr(pack_module, "RULES_VERSION", pack_module.RULES_VERSION + 1)
    assert PACK.rule_pack_hash != before


def test_an_unknown_pack_name_is_refused():
    for name in ("nonesuch", "../ontology/ontology"):
        with pytest.raises(UnknownRulePack):
            pack_path(name)


# --- gold scenarios ---------------------------------------------------------------------


@pytest.fixture
def store(con, graph_t0):
    with GraphStore.open(graph_t0, con) as opened:
        yield opened


def test_every_rules_scenario_passes(store, gold):
    reports = run_rules_suite(store, RuleEngine(PACK), gold)
    assert len(reports) == len([scenario for scenario in gold if scenario.suite == "rules"])
    failed = {report.scenario_id: report.diffs for report in reports if not report.passed}
    assert not failed


def test_a_wrong_expectation_is_reported_field_by_field(store, gold):
    by_id = {scenario.id: scenario for scenario in gold}
    result = screen(store, RuleEngine(PACK), by_id["SCEN-05"].target_id)
    wrong = by_id["SCEN-05"].model_copy(
        update={"expected_reasons": ["EFFECTIVE_EXPOSURE"], "expected_gaps": []}
    )
    assert [diff.field for diff in compare(result, wrong).diffs] == ["reasons"]
    other = compare(result, by_id["SCEN-21"])
    assert {diff.field for diff in other.diffs} >= {"blocked", "exposure", "paths"}
    assert (other.paths_expected, other.paths_found) == (2, 0)


def test_a_deeper_screen_finds_the_owner_beyond_the_limit(store, gold):
    target = next(scenario.target_id for scenario in gold if scenario.id == "SCEN-22")
    engine = RuleEngine(PACK)
    assert screen(store, engine, target).recommendation == "REVIEW"
    deeper = screen(store, engine, target, max_depth=7)
    assert (deeper.recommendation, deeper.reasons) == ("ESCALATE", ["DERIVED_50PCT"])
    assert not deeper.data_gaps
    # One level is blocked in each round, from the designated owner down to the target.
    rounds = {why.node_id: why.round for why in deeper.derivations}
    assert sorted(rounds.values()) == list(range(7))
    assert (rounds["fx_list:s22-top"], rounds[target]) == (0, 6)


def test_a_clear_result_says_what_was_checked(store, gold):
    target = next(scenario.target_id for scenario in gold if scenario.id == "SCEN-01")
    result = screen(store, RuleEngine(PACK), target)
    assert result.recommendation == "CLEAR" and not result.reasons and not result.findings
    assert (result.max_depth, result.nodes_checked, result.edges_checked) == (5, 3, 2)
    assert result.sources_checked == ["fx_list", "fx_registry"]
    assert result.rule_pack_hash == PACK.rule_pack_hash


def test_the_changed_list_changes_the_recommendations(con, ontology, gold):
    build_graph(con, ontology)
    t1 = build_graph(con, ontology, T1)
    changing = [scenario for scenario in gold if scenario.expected_recommendation_t1]
    assert changing
    with GraphStore.open(t1, con) as store:
        for scenario in changing:
            result = screen(store, RuleEngine(PACK), scenario.target_id)
            assert result.recommendation == scenario.expected_recommendation_t1, scenario.id


# --- seeds ------------------------------------------------------------------------------


def test_a_designation_that_crossed_a_refused_merge_is_reviewed_not_blocking():
    """The merge guard would not merge on that identifier, so it proves nothing."""
    subgraph = graph("T", [edge("A", "T", 60)])
    result = evaluate(subgraph, [designated("A", by="X", match_type="REGISTRATION")])
    assert (result.blocked_set, result.possibly_blocked_set) == ([], ["A", "T"])
    assert result.recommendation == "REVIEW"
    assert result.reasons == ["DECISION_RELEVANT_GAP", "POSSIBLY_BLOCKED"]
    assert gaps_of(result) == {("REVIEW_BAND_MATCH", "A", True)}


def test_the_class_of_a_match_does_not_depend_on_how_its_record_was_joined():
    for match_type in ("DIRECT", "LEI", "REGISTRATION", "FUZZY_NAME"):
        result = evaluate(graph("T", []), [designated("T", match_type=match_type)])
        assert result.reasons == ["DIRECT_DESIGNATION"], match_type


def test_inactive_and_weak_matches_seed_nothing():
    subgraph = graph("T", [edge("A", "T", 60)])
    assert evaluate(subgraph, [designated("A", active=False)]).recommendation == "CLEAR"
    weak = designated("A", by="X", confidence=0.78)
    assert evaluate(subgraph, [weak], review_band_low=0.8).recommendation == "CLEAR"
    assert evaluate(subgraph, [weak]).possibly_blocked_set == ["A", "T"]


def test_a_blocking_match_outranks_a_review_band_match_on_one_node():
    subgraph = graph("T", [edge("A", "T", 60)])
    result = evaluate(subgraph, [designated("A"), designated("A", by="X", confidence=0.8)])
    assert result.blocked_set == ["A", "T"] and not result.data_gaps


def test_a_review_band_match_on_a_node_the_legal_test_blocks_adds_nothing():
    subgraph = graph("T", [edge("A", "X", 60), edge("X", "T", 10)])
    result = evaluate(subgraph, [designated("A"), designated("X", by="L", confidence=0.8)])
    assert (result.blocked_set, result.possibly_blocked_set) == (["A", "X"], [])
    assert result.recommendation == "CLEAR" and not result.data_gaps


# --- scope ------------------------------------------------------------------------------


def test_context_below_the_target_takes_no_part():
    subgraph = graph("T", [edge("T", "S", 100), edge("A", "T", 10)])
    assert subgraph.down_ids == ["S"]
    listed_subsidiary = evaluate(subgraph, [designated("S")])
    assert listed_subsidiary.recommendation == "CLEAR" and not listed_subsidiary.blocked_set
    assert evaluate(subgraph, [designated("T")]).blocked_set == ["T"]


# --- the legal test ---------------------------------------------------------------------


def test_stakes_through_an_unblocked_entity_do_not_count():
    subgraph = graph("T", [edge("A", "X", 40), edge("X", "T", 100)])
    result = evaluate(subgraph, [designated("A")])
    assert result.blocked_set == ["A"] and result.aggregate_pct == 0
    assert result.effective_exposure.sum_pct == 40


def test_the_operator_and_aggregation_are_the_packs():
    exactly = graph("T", [edge("A", "T", 30), edge("B", "T", 20)])
    both = [designated("A"), designated("B")]
    assert evaluate(exactly, both).recommendation == "ESCALATE"
    strict = evaluate(exactly, both, threshold_operator=">")
    assert (strict.recommendation, strict.aggregate_pct) == ("REVIEW", 50)
    assert "NEAR_MISS_AGGREGATE" in strict.reasons

    # Without aggregation the largest single stake is tested.
    alone = evaluate(exactly, both, aggregate_blocked_owners=False)
    assert (alone.recommendation, alone.aggregate_pct) == ("REVIEW", 30)
    majority = graph("T", [edge("A", "T", 55), edge("B", "T", 20)])
    assert evaluate(majority, both, aggregate_blocked_owners=False).blocked_set == ["A", "B", "T"]


def test_a_consolidation_edge_with_a_percentage_is_a_stake():
    subgraph = graph("T", [edge("A", "T", 80, rel="CONSOLIDATED_BY")])
    result = evaluate(subgraph, [designated("A")])
    assert (result.recommendation, result.reasons) == ("ESCALATE", ["DERIVED_50PCT"])


def test_a_designated_target_that_blocked_owners_hold_stays_blocked_if_delisted():
    subgraph = graph("T", [edge("A", "T", 60)])
    result = evaluate(subgraph, [designated("A"), designated("T")])
    assert result.reasons == ["DERIVED_50PCT", "DIRECT_DESIGNATION"]
    derived = next(f for f in result.findings if f.code == "DERIVED_50PCT")
    assert derived.pct == 60 and derived.edge_ids == [edge("A", "T", 60).id]

    # A stake that is only blocked because the target is does not count.
    circular = graph("T", [edge("T", "X", 60), edge("X", "T", 60)])
    assert evaluate(circular, [designated("T")]).reasons == ["DIRECT_DESIGNATION"]


def test_derivations_say_why_each_node_is_in_its_set():
    subgraph = graph("T", [edge("A", "X", 60), edge("X", "T", 51), edge("C", "T", 49)])
    result = evaluate(subgraph, [designated("A")])
    why = {item.node_id: item for item in result.derivations}
    assert (why["A"].basis, why["A"].round, len(why["A"].sanction_ids)) == ("SEED", 0, 1)
    assert (why["X"].basis, why["X"].round, why["X"].aggregate_pct) == ("AGGREGATE", 1, 60)
    assert (why["T"].round, why["T"].edge_ids) == (2, [edge("X", "T", 51).id])
    assert "C" not in why


# --- parallel and ultimate-parent edges -------------------------------------------------


def test_parallel_edges_are_one_holding():
    """A registry's direct and ultimate record of one pair: one hop, one path."""
    pair = [
        edge("A", "T", rel="CONSOLIDATED_BY"),
        edge("A", "T", rel="CONSOLIDATED_BY", ultimate=True),
    ]
    result = evaluate(graph("T", pair), [designated("A")])
    assert result.possibly_blocked_set == ["T"]
    assert [path.edge_ids for path in result.paths] == [[pair[0].id]]
    assert result.effective_exposure == EffectiveExposure(has_unknown=True)
    assert gaps_of(result) == {("NULL_PCT_EDGE", item.id, True) for item in pair}


def test_a_known_stake_does_not_cancel_a_consolidation_of_the_same_pair():
    stake, consolidation = edge("A", "T", 30), edge("A", "T", rel="CONSOLIDATED_BY")
    result = evaluate(graph("T", [stake, consolidation]), [designated("A")])
    assert result.recommendation == "REVIEW" and result.possibly_blocked_set == ["T"]
    assert result.aggregate_pct == 30
    assert result.effective_exposure == EffectiveExposure(max_pct=30, sum_pct=30)
    assert [path.edge_ids for path in result.paths] == [[stake.id]]
    assert gaps_of(result) == {("NULL_PCT_EDGE", consolidation.id, True)}


def test_an_ultimate_parent_shortcut_does_not_restate_the_direct_chain():
    shortcut = edge("A", "T", rel="CONSOLIDATED_BY", ultimate=True)
    subgraph = graph("T", [edge("A", "M", 30), edge("M", "T", 100), shortcut])
    result = evaluate(subgraph, [designated("A")])
    assert [path.node_ids for path in result.paths] == [["A", "M", "T"]]
    assert result.effective_exposure == EffectiveExposure(max_pct=30, sum_pct=30)
    # Consolidated by a blocked ultimate parent: possibly blocked, whatever the chain says.
    assert result.possibly_blocked_set == ["T"]
    assert gaps_of(result) == {("NULL_PCT_EDGE", shortcut.id, False)}

    alone = evaluate(graph("T", [shortcut]), [designated("A")])
    assert [path.edge_ids for path in alone.paths] == [[shortcut.id]]


def test_without_the_presumption_an_unknown_stake_is_still_a_gap():
    subgraph = graph("T", [edge("A", "T", rel="CONSOLIDATED_BY")])
    result = evaluate(subgraph, [designated("A")], presume_majority_for_consolidation=False)
    assert not result.possibly_blocked_set
    assert (result.recommendation, result.reasons) == ("REVIEW", ["DECISION_RELEVANT_GAP"])


# --- exposure ---------------------------------------------------------------------------


def test_exposure_counts_from_the_last_blocked_entity():
    """A holds 60 % of X, so X is blocked in full; X's 20 % is 20 %, not 20 % + 12 %."""
    subgraph = graph("T", [edge("A", "X", 60), edge("X", "T", 20)])
    result = evaluate(subgraph, [designated("A")])
    assert result.blocked_set == ["A", "X"]
    assert result.effective_exposure == EffectiveExposure(max_pct=20, sum_pct=20)
    assert result.recommendation == "CLEAR"
    assert [path.node_ids for path in result.paths] == [["A", "X", "T"]]


def test_a_level_is_compared_with_the_exact_value_not_the_rounded_one():
    # 49.9999 % of 50.0001 % is 24.99999999 %: below 25, though it rounds to 25.0000.
    subgraph = graph("T", [edge("A", "X", "49.9999"), edge("X", "T", "50.0001")])
    result = evaluate(subgraph, [designated("A")])
    assert result.recommendation == "CLEAR"
    assert result.effective_exposure.sum_pct == Decimal("24.9999")

    chain = [edge(f"N{i}", f"N{i + 1}", "33.3333") for i in range(5)] + [edge("N5", "T", 100)]
    deep = evaluate(graph("T", chain), [designated("N0")])
    assert deep.effective_exposure.sum_pct == Decimal("0.4115")


def test_exposure_is_not_computed_for_a_blocked_target_or_over_control_links():
    blocked = evaluate(graph("T", [edge("A", "T", 60)]), [designated("A")])
    assert blocked.effective_exposure == EffectiveExposure()
    control = evaluate(graph("T", [edge("A", "T", rel="CONTROLS")]), [designated("A")])
    assert control.effective_exposure == EffectiveExposure()


def test_too_many_paths_stop_the_screen():
    diamond = [edge("A", "X", 30), edge("A", "Y", 30), edge("X", "T", 50), edge("Y", "T", 50)]
    assert len(evaluate(graph("T", diamond), [designated("A")]).paths) == 2
    with pytest.raises(PathLimitExceeded, match="max_paths"):
        evaluate(graph("T", diamond), [designated("A")], max_paths=1)


# --- control links ----------------------------------------------------------------------


def test_a_control_link_counts_anywhere_upstream_unless_the_pack_says_target():
    subgraph = graph("T", [edge("D", "M", rel="CONTROLS"), edge("M", "T", 100)])
    upstream = evaluate(subgraph, [designated("D")])
    assert (upstream.recommendation, upstream.reasons) == ("REVIEW", ["CONTROL_LINK"])
    finding = upstream.findings[0]
    assert (finding.subject_id, finding.edge_ids) == ("D", [edge("D", "M", rel="CONTROLS").id])

    assert evaluate(subgraph, [designated("D")], control_link_scope="target").reasons == []
    assert evaluate(subgraph, [designated("D")], control_link_review=False).reasons == []
    direct = graph("T", [edge("D", "T", rel="CONTROLS")])
    assert evaluate(direct, [designated("D")], control_link_scope="target").reasons == [
        "CONTROL_LINK"
    ]


# --- gaps -------------------------------------------------------------------------------


def test_a_gap_decides_only_on_a_path_from_a_blocked_party():
    edges = [edge("A", "X", 20), edge("X", "T", 100), edge("C", "T", rel="CONSOLIDATED_BY")]
    subgraph = graph(
        "T",
        edges,
        statuses={"X": "RETIRED", "C": "LAPSED"},
        reporting_exceptions=[reporting_exception("C")],
        set_gaps=[over_100("C")],
    )
    clean = evaluate(subgraph)
    assert clean.recommendation == "CLEAR"
    assert {code for code, _, relevant in gaps_of(clean) if not relevant} == {
        "LAPSED_LEI",
        "NULL_PCT_EDGE",
        "PCT_SUM_OVER_100",
        "REPORTING_EXCEPTION",
    }

    result = evaluate(subgraph, [designated("A")])
    assert result.reasons == ["DECISION_RELEVANT_GAP"]
    assert {gap for gap in gaps_of(result) if gap[2]} == {("LAPSED_LEI", "X", True)}
    assert [finding.subject_id for finding in result.findings] == ["X"]

    every = evaluate(subgraph, gap_scope="any")
    assert every.recommendation == "REVIEW" and all(gap[2] for gap in gaps_of(every))


def test_a_truncated_traversal_always_decides():
    subgraph = graph("T", [edge("A", "T", 10)], truncated_at=["A"])
    result = evaluate(subgraph)
    assert gaps_of(result) == {("DEPTH_LIMIT_REACHED", "A", True)}
    assert result.reasons == ["DECISION_RELEVANT_GAP"]


def test_a_linked_record_with_owners_of_its_own_is_a_gap():
    subgraph = graph("T", [edge("A", "T", 100)], links=[link("T", "T2")])
    result = evaluate(subgraph)
    assert gaps_of(result) == {("POSSIBLE_DUPLICATE", "T", True)}
    assert result.recommendation == "REVIEW"

    assert evaluate(subgraph, duplicate_link_review=False).recommendation == "CLEAR"
    for quiet in (
        link("T", "T2", has_upstream=False),
        link("T", "T2", confidence=0.78),
        # The other record is in the graph, so its owners are too.
        link("T", "A"),
    ):
        same = graph("T", [edge("A", "T", 100)], links=[quiet])
        assert evaluate(same, review_band_low=0.8).recommendation == "CLEAR"


# --- the command ------------------------------------------------------------------------


def build_with_the_cli():
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert runner.invoke(app, command).exit_code == 0


def test_eval_reports_every_suite():
    build_with_the_cli()
    result = runner.invoke(app, ["eval", "--suite", "all"])
    assert result.exit_code == 0, result.output
    assert "27/27 scenarios passed" in result.output
    assert "SCEN-23" in result.output and "pipeline suite" in result.output
    for metric in ("Decision accuracy", "Evidence-path recall", "ER precision@1"):
        assert metric in result.output
    assert "BELOW" not in result.output

    rules_only = runner.invoke(app, ["eval", "--suite", "rules", "--role", "auditor"])
    assert rules_only.exit_code == 0 and "ER precision@1" not in rules_only.output


def test_eval_fails_when_a_scenario_does(tmp_path, monkeypatch):
    build_with_the_cli()
    strict = tmp_path / "rules"
    strict.mkdir()
    data = {**PACK.model_dump(mode="json"), "threshold_operator": ">"}
    (strict / "strict.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(pack_module, "RULES_DIR", strict)

    result = runner.invoke(app, ["eval", "--suite", "rules", "--rule-pack", "strict"])
    assert result.exit_code == 1
    # Exactly 50 % no longer meets the test.
    assert "SCEN-07  FAIL  REVIEW" in result.output
    assert "recommendation: expected ESCALATE" in result.output
    assert "BELOW" in result.output


def test_eval_refuses_what_it_cannot_run():
    assert runner.invoke(app, ["eval"]).exit_code == 2  # no database
    assert runner.invoke(app, ["ingest", "--source", "fixtures"]).exit_code == 0
    unbuilt = runner.invoke(app, ["eval"])
    assert unbuilt.exit_code == 1 and "ubo build-graph" in unbuilt.output
    assert runner.invoke(app, ["eval", "--rule-pack", "nonesuch"]).exit_code == 2
    assert runner.invoke(app, ["eval", "--role", "analyst"]).exit_code == 1
