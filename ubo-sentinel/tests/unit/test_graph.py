"""The store, ego-graph extraction, the query API and query-time entity resolution."""

import inspect
from pathlib import Path

import pytest
from conftest import PACK, T1, build_graph
from typer.testing import CliRunner

from ubo_sentinel.cli.app import app
from ubo_sentinel.graph import queries
from ubo_sentinel.graph.entity_resolution import RESOLVE_FLOOR, evaluate_labelled
from ubo_sentinel.graph.projection import UnknownEntity, extract_ego_graph
from ubo_sentinel.graph.store import GraphNotBuilt, GraphStore
from ubo_sentinel.models import EntityRow, SubGraph
from ubo_sentinel.models.graph import upper_lower
from ubo_sentinel.pipeline.bronze import UnknownSnapshotSet
from ubo_sentinel.pipeline.entity_linking import LINK_FLOOR
from ubo_sentinel.pipeline.loaders.fixture_loader import ingest_fixtures
from ubo_sentinel.pipeline.transformer import build_silver

ER_PATH = Path("eval/er_labelled.csv")

MAX_DEPTH = PACK.max_depth
MAX_DEPTH_DOWN = PACK.max_depth_down

HUB = "lei:FXS00HUB000000000000"
MID = "lei:FXS00MID000000000000"
FOUNDER = "fx_list:s00-founder"

runner = CliRunner()


@pytest.fixture
def store(con, graph_t0):
    with GraphStore.open(graph_t0, con) as opened:
        yield opened


def subgraph_of(store, entity_id, max_depth_up=MAX_DEPTH):
    return queries.get_ownership_subgraph(store, entity_id, max_depth_up, MAX_DEPTH_DOWN)


# --- the store ------------------------------------------------------------------------


def test_a_store_is_bound_to_one_built_set(store, graph_t0):
    assert store.set_id == graph_t0
    assert len(store.gold_digest) == 64
    assert store.build_info()["gold"]["gold_digest"] == store.gold_digest


def test_a_store_opens_its_own_read_only_connection():
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert runner.invoke(app, command).exit_code == 0
    with GraphStore.open("fixtures") as store:
        assert queries.get_entity_by_id(store, HUB).legal_name == "Brackenridge Consolidated plc"
        with pytest.raises(Exception, match="read-only"):
            store.con.execute("DELETE FROM graph_nodes")


def test_a_set_without_a_graph_is_refused(con, ontology):
    with pytest.raises(UnknownSnapshotSet):
        with GraphStore.open("latest", con):
            pass

    snapshot_set = ingest_fixtures(con).snapshot_set
    with pytest.raises(GraphNotBuilt, match="ubo normalize"):
        with GraphStore.open("latest", con):
            pass

    build_silver(con, snapshot_set, ontology)
    with pytest.raises(GraphNotBuilt, match="ubo build-graph"):
        with GraphStore.open("latest", con):
            pass
    with pytest.raises(UnknownSnapshotSet):
        with GraphStore.open("0000000000000000", con):
            pass


def test_there_is_no_store_without_a_database():
    with pytest.raises(UnknownSnapshotSet):
        with GraphStore.open("fixtures"):
            pass


# --- ego-graph extraction ---------------------------------------------------------------


def test_every_expected_path_is_in_its_targets_subgraph(store, gold):
    scenarios = [s for s in gold if s.suite == "rules"]
    assert len(scenarios) >= 25
    for scenario in scenarios:
        subgraph = subgraph_of(store, scenario.target_id)
        assert subgraph.target_id == scenario.target_id
        edge_ids = {edge.id for edge in subgraph.edges}
        for path in scenario.expected_paths:
            assert set(path.nodes) <= set(subgraph.upward_ids), scenario.id
            assert {edge.edge_id() for edge in path.edges()} <= edge_ids, scenario.id
        # Every party the rules must find is among the upward nodes.
        found = set(scenario.expected_blocked) | set(scenario.expected_possibly_blocked)
        assert found <= set(subgraph.upward_ids), scenario.id


def test_depth_is_the_hop_at_which_a_node_is_first_reached(store):
    subgraph = subgraph_of(store, "lei:FXS06TARGET000000000")
    assert subgraph.depth_up == {
        "lei:FXS06INTER1000000000": 2,
        "lei:FXS06INTER2000000000": 1,
        "lei:FXS06TARGET000000000": 0,
        "lei:FXS06TOP000000000000": 3,
    }
    assert subgraph.truncated_at == [] and subgraph.down_ids == []
    assert len(subgraph.edges) == 3


def test_a_cycle_terminates_and_keeps_both_of_its_edges(store):
    target, cross = "lei:FXS15TARGET000000000", "lei:FXS15CROSS0000000000"
    subgraph = subgraph_of(store, target)
    assert subgraph.depth_up == {"fx_list:s15-blocked": 1, cross: 1, target: 0}
    ends = {(edge.subject_id, edge.object_id) for edge in subgraph.edges}
    assert ends == {("fx_list:s15-blocked", target), (cross, target), (target, cross)}
    assert subgraph.truncated_at == []


def test_a_walk_cut_at_max_depth_says_where(store, gold):
    scenario = next(s for s in gold if s.id == "SCEN-22")
    expected = [gap.node for gap in scenario.expected_gaps if gap.code == "DEPTH_LIMIT_REACHED"]

    cut = subgraph_of(store, scenario.target_id, max_depth_up=5)
    assert cut.truncated_at == expected == ["lei:FXS22INTER5000000000"]
    assert "fx_list:s22-top" not in cut.upward_ids

    whole = subgraph_of(store, scenario.target_id, max_depth_up=7)
    assert whole.truncated_at == []
    assert whole.depth_up["fx_list:s22-top"] == 6

    # With no hop allowed, the target itself is where the walk stopped.
    assert subgraph_of(store, scenario.target_id, max_depth_up=0).truncated_at == [
        scenario.target_id
    ]


def test_an_ultimate_parent_edge_is_one_hop(store):
    subgraph = subgraph_of(store, "lei:FXS00SUB700000000000")
    assert subgraph.depth_up == {
        "lei:FXS00SUB700000000000": 0,
        MID: 1,
        HUB: 1,
        FOUNDER: 2,
    }
    ultimate = [edge for edge in subgraph.edges if edge.is_ultimate]
    assert [(edge.subject_id, edge.object_id) for edge in ultimate] == [
        ("lei:FXS00SUB700000000000", HUB)
    ]
    # The controller of the hub is reached through a CONTROLS edge.
    assert {edge.rel_type for edge in subgraph.edges} == {"CONSOLIDATED_BY", "CONTROLS"}


def test_context_below_the_target_is_capped(store):
    whole = subgraph_of(store, HUB)
    assert len(whole.down_ids) == 9 and not whole.down_truncated
    assert set(whole.depth_up) == {HUB, FOUNDER}
    # Context nodes are placed below, never among the nodes the rules read.
    assert not set(whole.down_ids) & set(whole.upward_ids)

    capped = extract_ego_graph(store, HUB, MAX_DEPTH, MAX_DEPTH_DOWN, max_down_nodes=4)
    assert capped.down_ids == whole.down_ids[:4] and capped.down_truncated
    assert len(capped.nodes) == 6

    two_down = queries.get_ownership_subgraph(store, MID, MAX_DEPTH, 1)
    assert len(two_down.down_ids) == 2


def test_a_subgraph_carries_its_exceptions_gaps_and_links(store, gold):
    by_id = {scenario.id: scenario for scenario in gold}

    repex = subgraph_of(store, by_id["SCEN-12"].target_id).reporting_exceptions
    assert [(r.entity_id, r.category, r.reason) for r in repex] == [
        (by_id["SCEN-12"].target_id, "DIRECT_PARENT", "NON_PUBLIC")
    ]

    over = subgraph_of(store, by_id["SCEN-19"].target_id).set_gaps
    assert [(gap.code, gap.subject_id) for gap in over] == [
        ("PCT_SUM_OVER_100", by_id["SCEN-19"].target_id)
    ]
    assert over[0].detail == {"sum_pct": "110.0000"}

    links = subgraph_of(store, by_id["SCEN-14"].target_id).links
    assert [(l.node_id, l.linked_node_id, l.match_type) for l in links] == [  # noqa: E741
        ("lei:FXS14OWNER0000000000", "fx_list:s14-listed", "FUZZY_NAME")
    ]
    assert LINK_FLOOR <= links[0].confidence < PACK.match_threshold
    assert links[0].linked_name == "Petrakos Aegean Navigators"
    assert not links[0].linked_has_upstream


def test_a_subgraph_is_a_valid_model_and_a_networkx_graph(store):
    subgraph = subgraph_of(store, "lei:FXS21TARGET000000000")
    assert SubGraph.model_validate(subgraph.model_dump()) == subgraph

    graph = subgraph.to_networkx()
    assert set(graph.nodes) == {node.id for node in subgraph.nodes}
    assert graph.number_of_edges() == len(subgraph.edges) == 4
    for upper, lower, edge_id, data in graph.edges(keys=True, data=True):
        assert (upper, lower) == upper_lower(data["relationship"])
        assert data["relationship"].id == edge_id
    # The diamond: two ways up from the target to the blocked party.
    assert graph.in_degree("lei:FXS21TARGET000000000") == 2
    assert graph.out_degree("fx_list:s21-blocked") == 2


def test_a_subgraph_refuses_nodes_and_edges_that_do_not_fit(store):
    subgraph = subgraph_of(store, "lei:FXS06TARGET000000000")
    data = subgraph.model_dump()
    with pytest.raises(ValueError, match="outside the subgraph"):
        SubGraph.model_validate(data | {"nodes": data["nodes"][1:], "depth_up": {
            key: depth for key, depth in data["depth_up"].items() if key != data["nodes"][0]["id"]
        }})  # fmt: skip
    with pytest.raises(ValueError, match="depth 0"):
        SubGraph.model_validate(data | {"target_id": "lei:FXS06TOP000000000000"})


def test_an_unknown_entity_has_no_subgraph(store):
    with pytest.raises(UnknownEntity):
        subgraph_of(store, "lei:NOSUCHENTITY00000000")


# --- the query API ---------------------------------------------------------------------


def test_the_allow_list_is_the_modules_public_functions(store):
    public = {
        name
        for name, value in vars(queries).items()
        if inspect.isfunction(value)
        and value.__module__ == queries.__name__
        and not name.startswith("_")
    } - {"query"}
    assert set(queries.QUERIES) == public == set(queries.__all__) - {"QUERIES"}
    assert len(public) == 8
    # No query takes a snapshot set: the store fixes it.
    for spec in queries.QUERIES.values():
        assert "snapshot_set_id" not in spec.signature.parameters
        assert "store" not in spec.signature.parameters


def test_every_call_is_logged_on_its_store(store):
    queries.get_entity_by_id(store, HUB)
    queries.get_direct_owners(store, HUB)
    assert [(call.name, call.rows) for call in store.calls] == [
        ("get_entity_by_id", 1),
        ("get_direct_owners", 1),
    ]
    assert all(call.seconds >= 0 for call in store.calls)


def test_entity_by_id(store, entities):
    row = next(r for r in entities if r.source_record_id == "s01-target")
    entity = queries.get_entity_by_id(store, row.canonical_id())
    assert (entity.legal_name, entity.aliases, entity.lei) == (row.legal_name, row.aliases, row.lei)
    assert queries.get_entity_by_id(store, "lei:NOSUCHENTITY00000000") is None


def test_direct_owners_are_one_hop_up_whatever_the_relation(store):
    owners = queries.get_direct_owners(store, "lei:FXS11TARGET000000000")
    assert [(item.owner.id, item.relationship.rel_type) for item in owners] == [
        ("fx_list:s11-director", "CONTROLS"),
        ("lei:FXS11PARENT000000000", "OWNS"),
    ]
    # A child is consolidated by its parent: the parent is the owner.
    parents = queries.get_direct_owners(store, "lei:FXS04TARGET000000000")
    assert [item.owner.id for item in parents] == ["lei:FXS04PARENT000000000"]
    assert parents[0].relationship.subject_id == "lei:FXS04TARGET000000000"
    assert queries.get_direct_owners(store, FOUNDER) == []


def test_sanctioned_nodes_say_how_and_whose(store, gold):
    by_id = {scenario.id: scenario for scenario in gold}

    review = queries.get_sanctioned_nodes(store, subgraph_of(store, by_id["SCEN-14"].target_id))
    assert len(review) == 1
    match = review[0]
    assert (match.entity_id, match.designated_entity_id, match.match_type) == (
        "lei:FXS14OWNER0000000000",
        "fx_list:s14-listed",
        "FUZZY_NAME",
    )
    assert LINK_FLOOR <= match.confidence < PACK.match_threshold and match.is_active
    assert [s.provenance.source_record_id for s in match.sanction_records] == ["s14-des"]

    direct = queries.get_sanctioned_nodes(store, subgraph_of(store, by_id["SCEN-03"].target_id))
    assert [(m.entity_id, m.designated_entity_id, m.match_type, m.confidence) for m in direct] == [
        ("fx_list:s03-owner-a", "fx_list:s03-owner-a", "DIRECT", 1.0),
        ("fx_list:s03-owner-b", "fx_list:s03-owner-b", "DIRECT", 1.0),
    ]

    # An inactive designation is still returned; it is the rules that ignore it.
    inactive = queries.get_sanctioned_nodes(store, subgraph_of(store, by_id["SCEN-17"].target_id))
    assert [(m.entity_id, m.is_active) for m in inactive] == [("fx_list:s17-owner", False)]

    assert queries.get_sanctioned_nodes(store, subgraph_of(store, by_id["SCEN-01"].target_id)) == []


def test_reporting_exceptions_of_an_entity(store):
    found = queries.get_reporting_exceptions(store, HUB)
    assert [(r.category, r.reason) for r in found] == [
        ("DIRECT_PARENT", "NATURAL_PERSONS"),
        ("ULTIMATE_PARENT", "NATURAL_PERSONS"),
    ]
    assert queries.get_reporting_exceptions(store, MID) == []


def test_affected_entities_are_what_a_changed_designation_can_reach(store):
    # A designated owner: itself and what it owns.
    assert queries.get_affected_entities(store, "fx_list:s03-owner-a", MAX_DEPTH) == [
        "fx_list:s03-owner-a",
        "lei:FXS03TARGET000000000",
    ]
    # A list record that owns nothing: its designation reaches the linked registry
    # entity, and through it the company that entity owns.
    assert queries.get_affected_entities(store, "fx_list:s14-listed", MAX_DEPTH) == [
        "fx_list:s14-listed",
        "lei:FXS14OWNER0000000000",
        "lei:FXS14TARGET000000000",
    ]
    # Depth limits the walk down; a cycle ends it.
    top = "fx_list:s22-top"
    assert len(queries.get_affected_entities(store, top, 2)) == 3
    assert len(queries.get_affected_entities(store, top, 10)) == 7
    assert queries.get_affected_entities(store, "fx_list:s15-blocked", 10) == [
        "fx_list:s15-blocked",
        "lei:FXS15CROSS0000000000",
        "lei:FXS15TARGET000000000",
    ]


def test_a_citation_leads_to_the_published_record(store, entities):
    row = next(r for r in entities if r.source_record_id == "s01-target")
    entity = queries.get_entity_by_id(store, row.canonical_id())
    record = queries.get_source_record(store, entity.provenance)
    assert EntityRow.model_validate(record) == row

    edge = queries.get_direct_owners(store, row.canonical_id())[0].relationship
    cited = queries.get_source_record(store, edge.provenance)
    assert cited["source_record_id"] == edge.provenance.source_record_id
    assert cited["object_ref"] == row.ref

    missing = entity.provenance.model_copy(update={"source_record_id": "no-such-record"})
    assert queries.get_source_record(store, missing) is None
    elsewhere = entity.provenance.model_copy(update={"snapshot_id": "0000000000000000"})
    assert queries.get_source_record(store, elsewhere) is None


def test_two_stores_read_two_sets(con, graph_t0, ontology, gold):
    t1 = build_graph(con, ontology, T1)
    scenario = next(s for s in gold if s.id == "SCEN-01")
    with GraphStore.open(graph_t0, con) as old, GraphStore.open("latest", con) as new:
        assert (old.set_id, new.set_id) == (graph_t0, t1)
        assert old.gold_digest != new.gold_digest
        before = queries.get_sanctioned_nodes(old, subgraph_of(old, scenario.target_id))
        after = queries.get_sanctioned_nodes(new, subgraph_of(new, scenario.target_id))
    assert before == []
    assert [match.entity_id for match in after] == ["lei:FXS01PARENT000000000"]
    # `fixtures` still names the complete set.
    with GraphStore.open("fixtures", con) as fixtures:
        assert fixtures.set_id == graph_t0


# --- entity resolution -----------------------------------------------------------------


def test_an_identifier_decides_alone(store):
    by_lei = queries.resolve_entity(store, "Kittiwake Pkg", lei="FXS22TARGET000000000")
    assert [(m.entity_id, m.match_type, m.confidence) for m in by_lei] == [
        ("lei:FXS22TARGET000000000", "LEI", 1.0)
    ]
    # An LEI typed as the query is an LEI.
    as_query = queries.resolve_entity(store, " fxs22target000000000 ")
    assert [m.entity_id for m in as_query] == ["lei:FXS22TARGET000000000"]

    by_number = queries.resolve_entity(store, "anything", registration_number="06-TOP")
    assert [(m.entity_id, m.match_type) for m in by_number] == [
        ("lei:FXS06TOP000000000000", "REGISTRATION")
    ]
    # An identifier that matches nothing falls back to the name.
    fallback = queries.resolve_entity(store, "Acme Trading FZE", lei="FXS99NOSUCH000000000")
    assert [m.entity_id for m in fallback] == ["lei:FXS01TARGET000000000"]


def test_a_name_is_matched_exactly_then_by_similarity(store):
    exact = queries.resolve_entity(store, "ACME TRADING F.Z.E.")
    assert [(m.entity_id, m.match_type, m.confidence) for m in exact] == [
        ("lei:FXS01TARGET000000000", "NAME_EXACT", 1.0)
    ]
    assert (exact[0].legal_name, exact[0].jurisdiction) == ("Acme Trading FZE", "AE")

    alias = queries.resolve_entity(store, "Vostrek Marine")[0]
    assert (alias.matched_name, alias.legal_name) == ("Vostrek Marine", "Vostrek Maritime LLC")

    typo = queries.resolve_entity(store, "Acme Tradng FZE")[0]
    assert (typo.entity_id, typo.match_type) == ("lei:FXS01TARGET000000000", "NAME_FUZZY")
    assert RESOLVE_FLOOR <= typo.confidence < 1

    assert queries.resolve_entity(store, "Zzyzx Nonexistent Ventures") == []
    assert queries.resolve_entity(store, "...") == []


def test_jurisdiction_ranks_homonyms_and_never_filters(store):
    name = "Silverline Commodities Ltd"
    cyprus, malta = "lei:FXS16TARGET000000000", "lei:FXS24TARGET000000000"

    both = queries.resolve_entity(store, name)
    assert [(m.entity_id, m.confidence) for m in both] == [(cyprus, 1.0), (malta, 1.0)]
    assert [m.entity_id for m in queries.resolve_entity(store, name, jurisdiction="MT")] == [
        malta,
        cyprus,
    ]
    assert [m.entity_id for m in queries.resolve_entity(store, name, jurisdiction="CY")] == [
        cyprus,
        malta,
    ]
    assert len(queries.resolve_entity(store, name, max_candidates=1)) == 1
    # A jurisdiction no candidate has changes nothing.
    assert queries.resolve_entity(store, name, jurisdiction="ZZ") == both


def test_resolution_meets_the_bar_on_the_labelled_set(store):
    report = evaluate_labelled(store, ER_PATH)
    assert report.total >= 35
    assert report.precision_at_1 >= 0.95, report.misses
    # No negative row returns a candidate at or above the floor.
    assert report.false_candidates == []
    assert RESOLVE_FLOOR == LINK_FLOOR
