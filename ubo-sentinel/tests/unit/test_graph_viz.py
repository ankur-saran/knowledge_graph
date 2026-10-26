"""The evidence graph: drawn from the decision's memo, as the role sees it, and safe to embed."""

import re

import pytest
from e2e import screen_gold

from ubo_sentinel.agents.supervisor import Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.graph.views import mask_decision, masked_values
from ubo_sentinel.ui.graph_viz import LEGEND, STATE_STYLE, edge_rows, evidence_graph, to_html


@pytest.fixture
def decide(fresh_con, ontology, gold):
    """Screen gold scenarios on t0 and return their decisions."""
    con, (t0,) = fresh_con("t0")
    supervisor = Supervisor(ontology, AuditLog())
    scenarios = {s.id: s for s in gold}
    return lambda sid: screen_gold(supervisor, scenarios[sid], t0, con).decision


def spec_of(decision, ontology, role="reviewer", **kwargs):
    return evidence_graph(mask_decision(decision, role, ontology), **kwargs)


def nodes(spec):
    return {node.id: node for node in spec.nodes}


def test_blocked_by_two_owners_is_drawn_with_its_paths(decide, ontology):
    # SCEN-03: two designated persons hold 30 % each.
    spec = spec_of(decide("SCEN-03"), ontology)
    drawn = nodes(spec)
    target = "lei:FXS03TARGET000000000"
    assert drawn[target].is_target and drawn[target].state == "blocked"
    assert {drawn[n].state for n in drawn if n != target} == {"designated"}
    assert all(edge.on_path and edge.label == "OWNS 30%" for edge in spec.edges)
    assert all(node.level == (0 if node.is_target else 1) for node in spec.nodes)


def test_three_hops_up_are_three_levels(decide, ontology):
    spec = spec_of(decide("SCEN-06"), ontology)
    levels = {node.id: node.level for node in spec.nodes}
    assert levels == {
        "lei:FXS06TARGET000000000": 0,
        "lei:FXS06INTER2000000000": 1,
        "lei:FXS06INTER1000000000": 2,
        "lei:FXS06TOP000000000000": 3,
    }
    assert nodes(spec)["lei:FXS06TOP000000000000"].state == "designated"


def test_an_edge_without_a_share_is_a_question_mark_and_a_gap(decide, ontology):
    (edge,) = spec_of(decide("SCEN-04"), ontology).edges
    assert edge.label == "CONSOLIDATED_BY ? ⚠"
    assert edge.gaps == ("NULL_PCT_EDGE",) and edge.on_path


def test_a_review_band_match_is_contested_and_its_designation_is_outside(decide, ontology):
    spec = spec_of(decide("SCEN-14"), ontology)
    drawn = nodes(spec)
    owner = drawn["lei:FXS14OWNER0000000000"]
    assert owner.contested and owner.state == "possibly_blocked"
    assert owner.gaps == ("REVIEW_BAND_MATCH",)
    assert drawn["fx_list:s14-listed"].state == "outside"
    (match,) = [edge for edge in spec.edges if edge.kind == "MATCH"]
    assert (match.upper_id, match.lower_id) == ("fx_list:s14-listed", owner.id)
    assert match.label.startswith("FUZZY_NAME 0.")


def test_a_cycle_is_drawn_once_when_everything_checked_is_shown(decide, ontology):
    spec = spec_of(decide("SCEN-15"), ontology, scope="checked")
    ids = [edge.id for edge in spec.edges]
    assert len(ids) == len(set(ids))
    pairs = {(edge.upper_id, edge.lower_id) for edge in spec.edges}
    assert any((lower, upper) in pairs for upper, lower in pairs)


def test_a_truncated_owner_is_drawn_with_its_chain_to_the_target(decide, ontology):
    spec = spec_of(decide("SCEN-22"), ontology)
    top = nodes(spec)["lei:FXS22INTER5000000000"]
    assert top.truncated and top.level == 5 and "DEPTH LIMIT" in top.label
    assert len(spec.nodes) == 6 and len(spec.edges) == 5


def test_a_linked_record_with_owners_of_its_own_is_drawn_beside_the_target(decide, ontology):
    spec = spec_of(decide("SCEN-27"), ontology)
    (linked,) = [edge for edge in spec.edges if edge.kind == "LINKED_RECORD"]
    assert linked.lower_id == "lei:FXS27TARGET000000000"
    assert nodes(spec)[linked.upper_id].state == "outside"


def test_a_designation_on_another_list_has_its_own_state(decide, ontology):
    spec = spec_of(decide("SCEN-30"), ontology)
    assert spec.scope == "paths"
    assert nodes(spec)["lei:FXS30OWNER0000000000"].state == "other_regime"


def test_a_clear_shows_what_was_checked(decide, ontology):
    spec = spec_of(decide("SCEN-01"), ontology)
    assert spec.scope == "checked" and len(spec.nodes) == 3
    assert {node.state for node in spec.nodes} == {"other"}


def test_nothing_found_draws_nothing(decide, ontology):
    spec = spec_of(decide("SCEN-23"), ontology)
    assert spec.nodes == [] and spec.notes


def test_the_cap_keeps_the_target_and_says_what_it_left_out(decide, ontology):
    spec = spec_of(decide("SCEN-22"), ontology, scope="checked", cap=2)
    assert len(spec.nodes) == 2 and spec.not_drawn == 4
    assert any(node.is_target for node in spec.nodes)
    assert "4 more node(s) not drawn" in spec.notes[0]


def test_the_same_decision_draws_the_same_graph(decide, ontology):
    decision = decide("SCEN-14")
    assert spec_of(decision, ontology) == spec_of(decision, ontology)
    assert to_html(spec_of(decision, ontology)) == to_html(spec_of(decision, ontology))


def test_an_analyst_sees_no_person_in_the_graph(decide, ontology):
    for sid in ("SCEN-03", "SCEN-11", "SCEN-28"):
        decision = decide(sid)
        secrets = masked_values(decision, "analyst", ontology)
        assert secrets, sid
        spec = spec_of(decision, ontology, role="analyst")
        html = to_html(spec)
        assert not [value for value in secrets if value in spec.text() or value in html]
        assert any(node.id.startswith("person:") for node in spec.nodes)
        # Masked or not, the role sees the same evidence.
        reviewer = spec_of(decision, ontology)
        assert len(spec.nodes) == len(reviewer.nodes) and len(spec.edges) == len(reviewer.edges)


def test_a_name_cannot_end_the_script_it_is_written_into(decide, ontology):
    view = mask_decision(decide("SCEN-03"), "reviewer", ontology)
    hostile = "Evil </script><script>alert(1)</script> & Co"
    entities = [e.model_copy(update={"legal_name": hostile}) for e in view.memo.entities]
    view = view.model_copy(update={"memo": view.memo.model_copy(update={"entities": entities})})
    html = to_html(evidence_graph(view))
    assert "<script>alert(1)" not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert(1)" in html
    # The popup shows the title as text, so it is not escaped as HTML.
    assert hostile in evidence_graph(view).nodes[0].title


def test_the_page_loads_nothing_from_the_network(decide, ontology):
    html = to_html(spec_of(decide("SCEN-14"), ontology))
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html)
    assert "vis.Network" in html


def test_the_table_and_the_legend_say_what_the_drawing_says(decide, ontology):
    spec = spec_of(decide("SCEN-04"), ontology)
    (row,) = edge_rows(spec)
    assert row["relation"] == "CONSOLIDATED_BY ? ⚠"
    assert row["on an evidence path"] == "yes" and row["gaps"] == "NULL_PCT_EDGE"
    assert row["to"].startswith("TARGET")
    # Every state has a colour, a shape and a word, and the legend names each.
    assert len({style[1] for style in STATE_STYLE.values()}) >= 5
    assert len(LEGEND) >= len(STATE_STYLE)
