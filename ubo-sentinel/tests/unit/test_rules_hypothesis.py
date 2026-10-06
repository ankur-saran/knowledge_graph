"""Properties of the rule engine over arbitrary graphs: cycles, parallel edges, unknown stakes."""

from collections import defaultdict
from fractions import Fraction

import networkx as nx
from conftest import PACK
from graphs import designated, edge, graph, over_100, upward_depths
from hypothesis import given, settings
from hypothesis import strategies as st

from ubo_sentinel.models.graph import upper_lower
from ubo_sentinel.rules.engine import RuleEngine
from ubo_sentinel.rules.pack import RulePack

NODES = [f"N{i}" for i in range(6)]
TARGET = NODES[0]
SEVERITY = {"CLEAR": 0, "REVIEW": 1, "ESCALATE": 2}
# Stakes around the levels the pack tests, and some that only add up to one.
PERCENTAGES = [None, 10, 20, 25, 30, "49.9999", 50, "50.0001", 60, 100]

PACKS = st.builds(
    lambda **changes: RulePack.model_validate({**PACK.model_dump(), **changes}),
    threshold_operator=st.sampled_from([">=", ">"]),
    aggregate_blocked_owners=st.booleans(),
    presume_majority_for_consolidation=st.booleans(),
    gap_scope=st.sampled_from(["on_path", "any"]),
    control_link_scope=st.sampled_from(["upstream", "target"]),
)


@st.composite
def edge_lists(draw, ultimate=True):
    """Edges between distinct nodes, at most one per (pair, relation, ultimate)."""
    keys = draw(
        st.lists(
            st.tuples(
                st.sampled_from(NODES),
                st.sampled_from(NODES),
                st.sampled_from(["OWNS", "CONSOLIDATED_BY", "CONTROLS"]),
                st.booleans() if ultimate else st.just(False),
            ).filter(lambda key: key[0] != key[1]),
            unique=True,
            max_size=14,
        )
    )
    edges = []
    for upper, lower, rel, is_ultimate in keys:
        pct = None if rel == "CONTROLS" else draw(st.sampled_from(PERCENTAGES))
        edges.append(edge(upper, lower, pct, rel, is_ultimate and rel == "CONSOLIDATED_BY"))
    return list({item.id: item for item in edges}.values())


def stakes(edges):
    """(upper, lower) -> the pair's direct stake, worked out apart from the engine."""
    found = {}
    for item in edges:
        if item.rel_type != "CONTROLS" and item.pct is not None and not item.is_ultimate:
            pair = upper_lower(item)
            found[pair] = max(found.get(pair, Fraction(0)), Fraction(item.pct))
    return found


def subgraph_of(edges):
    """The part above the target, with the set-level gap Silver would have written."""
    above = set(upward_depths(TARGET, edges))
    kept = [item for item in edges if set(upper_lower(item)) <= above]
    totals = defaultdict(Fraction)
    for (_, lower), pct in stakes(kept).items():
        totals[lower] += pct
    over = [over_100(node) for node, total in totals.items() if total > 100]
    return graph(TARGET, kept, set_gaps=over)


@st.composite
def cases(draw, ultimate=True):
    subgraph = subgraph_of(draw(edge_lists(ultimate)))
    listed = draw(st.lists(st.sampled_from(sorted(subgraph.depth_up)), unique=True, max_size=3))
    kinds = st.sampled_from(["own", "name", "band", "refused", "inactive"])
    matches = []
    for node in listed:
        kind = draw(kinds)
        if kind == "own":
            matches.append(designated(node))
        elif kind == "name":
            matches.append(designated(node, by="L", confidence=0.95))
        elif kind == "band":
            matches.append(designated(node, by="L", confidence=0.8))
        elif kind == "refused":
            matches.append(designated(node, by="L", match_type="REGISTRATION"))
        else:
            matches.append(designated(node, active=False))
    return subgraph, matches


def meets(pack, value):
    threshold = Fraction(pack.ownership_threshold_pct)
    return value > threshold if pack.threshold_operator == ">" else value >= threshold


def held(pack, subgraph, node, holders):
    """What `holders` own of `node` directly, under the pack's aggregation."""
    found = [
        pct for (u, lower), pct in stakes(subgraph.edges).items() if lower == node and u in holders
    ]
    if not found:
        return Fraction(0)
    return sum(found) if pack.aggregate_blocked_owners else max(found)


@given(cases(), PACKS)
@settings(max_examples=300, deadline=None)
def test_the_fix_point_ends_and_the_result_is_consistent(case, pack):
    subgraph, matches = case
    result = RuleEngine(pack).evaluate(subgraph, matches)

    blocked, possibly = set(result.blocked_set), set(result.possibly_blocked_set)
    assert not blocked & possibly
    assert blocked | possibly <= set(subgraph.depth_up)
    assert (result.recommendation == "ESCALATE") == (TARGET in blocked)
    if result.recommendation == "CLEAR":
        assert not any(gap.decision_relevant for gap in result.data_gaps)
        assert TARGET not in possibly
    for path in result.paths:
        assert path.node_ids[-1] == TARGET and len(set(path.node_ids)) == len(path.node_ids)


@given(cases(), PACKS)
@settings(max_examples=300, deadline=None)
def test_the_blocked_set_is_the_least_fixed_point(case, pack):
    subgraph, matches = case
    result = RuleEngine(pack).evaluate(subgraph, matches)
    blocked = set(result.blocked_set)
    seeds = {why.node_id for why in result.derivations if why.basis == "SEED"} & blocked

    # Closed: nothing outside the set meets the test from the owners inside it.
    for node in set(subgraph.depth_up) - blocked:
        assert not meets(pack, held(pack, subgraph, node, blocked))
    # Least: growing the set from the seeds alone arrives at the same set.
    grown = set(seeds)
    while True:
        more = {
            node
            for node in set(subgraph.depth_up) - grown
            if meets(pack, held(pack, subgraph, node, grown))
        }
        if not more:
            break
        grown |= more
    assert grown == blocked


@given(cases(), PACKS, st.randoms(use_true_random=False))
@settings(max_examples=200, deadline=None)
def test_the_result_does_not_depend_on_input_order(case, pack, random):
    subgraph, matches = case
    nodes, edges, shuffled = list(subgraph.nodes), list(subgraph.edges), list(matches)
    for items in (nodes, edges, shuffled):
        random.shuffle(items)
    again = subgraph.model_copy(update={"nodes": nodes, "edges": edges})
    engine = RuleEngine(pack)
    assert engine.evaluate(again, shuffled) == engine.evaluate(subgraph, matches)


@given(cases(), PACKS, st.data())
@settings(max_examples=300, deadline=None)
def test_one_more_blocked_party_never_lowers_the_recommendation(case, pack, data):
    subgraph, matches = case
    unlisted = sorted(set(subgraph.depth_up) - {match.entity_id for match in matches})
    if not unlisted:
        return
    engine = RuleEngine(pack)
    before = engine.evaluate(subgraph, matches)
    after = engine.evaluate(subgraph, [*matches, designated(data.draw(st.sampled_from(unlisted)))])
    assert SEVERITY[after.recommendation] >= SEVERITY[before.recommendation]
    assert set(after.blocked_set) >= set(before.blocked_set)


@given(cases(), PACKS)
@settings(max_examples=200, deadline=None)
def test_a_stricter_test_blocks_no_more(case, pack):
    subgraph, matches = case
    loose = RulePack.model_validate({**pack.model_dump(), "threshold_operator": ">="})
    strict = RulePack.model_validate({**pack.model_dump(), "threshold_operator": ">"})
    higher = RulePack.model_validate({**loose.model_dump(), "ownership_threshold_pct": 60})
    base = set(RuleEngine(loose).evaluate(subgraph, matches).blocked_set)
    assert set(RuleEngine(strict).evaluate(subgraph, matches).blocked_set) <= base
    assert set(RuleEngine(higher).evaluate(subgraph, matches).blocked_set) <= base


@given(cases(), PACKS)
@settings(max_examples=200, deadline=None)
def test_an_unconnected_component_changes_nothing(case, pack):
    subgraph, matches = case
    apart = [edge("Y", "Z", 100)]
    wider = graph(TARGET, [*subgraph.edges, *apart], set_gaps=subgraph.set_gaps)
    assert wider.down_ids == ["Y", "Z"]
    engine = RuleEngine(pack)
    result = engine.evaluate(wider, [*matches, designated("Y")])
    # Only the depth the wider graph was read to differs.
    same = engine.evaluate(subgraph, matches).model_copy(update={"max_depth": result.max_depth})
    assert result == same


@given(cases(ultimate=False), PACKS)
@settings(max_examples=300, deadline=None)
def test_exposure_equals_a_brute_force_count(case, pack):
    """Every simple ownership path from a blocked entity with no blocked entity after it."""
    subgraph, matches = case
    result = RuleEngine(pack).evaluate(subgraph, matches)
    blocked = set(result.blocked_set)
    if TARGET in blocked:
        assert result.effective_exposure.sum_pct is None
        return

    known = stakes(subgraph.edges)
    owned = nx.DiGraph()
    owned.add_nodes_from(subgraph.depth_up)
    owned.add_edges_from(upper_lower(e) for e in subgraph.edges if e.rel_type != "CONTROLS")
    values, unknown = [], False
    for start in blocked:
        for path in nx.all_simple_paths(owned, start, TARGET):
            pairs = list(zip(path[:-1], path[1:], strict=True))
            if set(path[1:]) & blocked:
                continue
            if all(pair in known for pair in pairs):
                value = Fraction(100)
                for pair in pairs:
                    value = value * known[pair] / 100
                values.append(value)
            else:
                unknown = True

    exposure = result.effective_exposure
    assert exposure.has_unknown == unknown
    if not values:
        assert exposure.sum_pct is None and exposure.max_pct is None
        return
    assert abs(Fraction(exposure.sum_pct) - sum(values)) < Fraction(1, 10000)
    assert abs(Fraction(exposure.max_pct) - max(values)) < Fraction(1, 10000)
    fired = "EFFECTIVE_EXPOSURE" in result.reasons
    assert fired == (sum(values) >= Fraction(pack.review_exposure_pct))
