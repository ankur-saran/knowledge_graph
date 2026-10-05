"""Role views: what an analyst, a reviewer, an auditor and an engineer see of the graph."""

import json

import pytest
from conftest import T1, build_graph

from ubo_sentinel.graph import queries
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.graph.views import MASK, GraphView
from ubo_sentinel.models import canonical_json

TARGET = "lei:FXS03TARGET000000000"
PERSON = "fx_list:s03-owner-a"
OTHER_PERSON = "fx_list:s03-owner-b"
# Everything the fixtures say about the two persons who own SCEN-03's target.
PII = ["Dmitri Volkanov", "Дмитрий Волканов", "Yusuf Al-Qahdari", "يوسف القحدري"]


@pytest.fixture
def store(con, graph_t0):
    with GraphStore.open(graph_t0, con) as opened:
        yield opened


@pytest.fixture
def view(store, ontology):
    def make(role):
        return GraphView(store, role, ontology)

    return make


def text(value):
    """Everything in a result, as one string to search."""
    if isinstance(value, list):
        return "".join(text(item) for item in value)
    return canonical_json(value).decode("utf-8")


def subgraph(target_view):
    return target_view.get_ownership_subgraph(TARGET, 5, 1)


# --- analyst ---------------------------------------------------------------------------


def test_an_analyst_sees_the_whole_graph_without_person_details(store, view):
    plain = queries.get_ownership_subgraph(store, TARGET, 5, 1)
    masked = subgraph(view("analyst"))

    assert all(name in text(plain) for name in PII)
    assert not any(name in text(masked) for name in PII)
    assert PERSON not in text(masked) and "s03-owner-a" not in text(masked)

    # The shape is the one a reviewer sees: same nodes, edges and depths.
    assert len(masked.nodes) == len(plain.nodes) == 4
    assert [edge.id for edge in masked.edges] == [edge.id for edge in plain.edges]
    assert sorted(masked.depth_up.values()) == sorted(plain.depth_up.values())

    persons = [node for node in masked.nodes if node.entity_type == "Person"]
    assert len(persons) == 2
    for person in persons:
        assert person.legal_name == MASK and person.aliases == [MASK]
        assert person.id.startswith("person:")
        assert person.provenance.source_record_id == MASK
        # What is not personal stays.
        assert person.jurisdiction in {"RU", "AE"}
    # A company is not masked.
    company = masked.node(TARGET)
    assert company == plain.node(TARGET)


def test_masked_fields_come_from_the_ontology(store, ontology):
    spec = ontology.roles["analyst"]
    wider = ontology.model_copy(
        update={
            "roles": ontology.roles
            | {"analyst": spec.model_copy(update={"masked_fields": {"Person": ["jurisdiction"]}})}
        }
    )
    masked = subgraph(GraphView(store, "analyst", wider))
    person = next(node for node in masked.nodes if node.entity_type == "Person")
    assert person.jurisdiction == MASK
    assert person.legal_name != MASK


def test_one_person_has_one_pseudonym_everywhere(view):
    analyst = view("analyst")
    masked = subgraph(analyst)
    alias = analyst.pseudonym(PERSON, "Person")
    assert alias != analyst.pseudonym(OTHER_PERSON, "Person")

    assert alias in {node.id for node in masked.nodes}
    assert alias in masked.depth_up
    assert alias in {edge.subject_id for edge in masked.edges}

    matches = analyst.get_sanctioned_nodes(masked)
    assert alias in {match.entity_id for match in matches}
    assert len(matches) == 2
    for match in matches:
        assert match.designated_entity_id == match.entity_id
        assert match.sanction_records[0].entity_id == match.entity_id
        assert match.sanction_records[0].provenance.source_record_id == MASK
    assert not any(name in text(matches) for name in [PERSON, OTHER_PERSON, "s03-des"])

    owners = analyst.get_direct_owners(TARGET)
    assert alias in {item.owner.id for item in owners}
    assert alias in {item.relationship.subject_id for item in owners}
    assert alias in analyst.get_affected_entities(alias, 5)
    assert PERSON not in analyst.get_affected_entities(alias, 5)

    # The analyst can go on working with the pseudonym.
    again = analyst.get_entity_by_id(alias)
    assert (again.id, again.legal_name) == (alias, MASK)
    assert analyst.get_ownership_subgraph(alias, 5, 1).target_id == alias


def test_a_masked_candidate_can_still_be_picked(view):
    found = view("analyst").resolve_entity("Dmitri Volkanov")
    assert len(found) == 1
    candidate = found[0]
    assert (candidate.legal_name, candidate.matched_name) == (MASK, MASK)
    assert candidate.entity_id.startswith("person:")
    assert (candidate.jurisdiction, candidate.confidence) == ("RU", 1.0)

    company = view("analyst").resolve_entity("Halcyon Ridge Minerals Ltd")[0]
    assert (company.entity_id, company.legal_name) == (TARGET, "Halcyon Ridge Minerals Ltd")


def test_an_analyst_cannot_open_a_record_about_a_person(store, view):
    analyst = view("analyst")
    person = queries.get_entity_by_id(store, PERSON)
    owners = queries.get_direct_owners(store, TARGET)
    stake = next(item.relationship for item in owners if item.owner.id == PERSON)
    company_stake = next(item.relationship for item in owners if item.owner.entity_type != "Person")
    matches = queries.get_sanctioned_nodes(
        store, queries.get_ownership_subgraph(store, TARGET, 5, 1)
    )
    designation = matches[0].sanction_records[0]

    for provenance in (person.provenance, stake.provenance, designation.provenance):
        record = analyst.get_source_record(provenance)
        assert record["masked"] is True
        assert not any(name in json.dumps(record, ensure_ascii=False) for name in [*PII, PERSON])
        assert "source_record_id" not in record

    # Records about companies are opened as published.
    for provenance in (
        company_stake.provenance,
        queries.get_entity_by_id(store, TARGET).provenance,
    ):
        assert analyst.get_source_record(provenance) == queries.get_source_record(store, provenance)
    missing = person.provenance.model_copy(update={"source_record_id": "no-such-record"})
    assert analyst.get_source_record(missing) is None


def test_an_analyst_sees_reporting_exceptions_and_may_write(view):
    analyst = view("analyst")
    found = analyst.get_reporting_exceptions("lei:FXS12TARGET000000000")
    assert [record.reason for record in found] == ["NON_PUBLIC"]
    analyst.require_write()


# --- reviewer --------------------------------------------------------------------------


def test_a_reviewer_sees_everything(store, view):
    reviewer = view("reviewer")
    assert subgraph(reviewer) == queries.get_ownership_subgraph(store, TARGET, 5, 1)
    assert reviewer.get_entity_by_id(PERSON).legal_name == "Dmitri Volkanov"
    assert reviewer.resolve_entity("Dmitri Volkanov")[0].entity_id == PERSON
    record = reviewer.get_source_record(reviewer.get_entity_by_id(PERSON).provenance)
    assert record["legal_name"] == "Dmitri Volkanov"
    matches = reviewer.get_sanctioned_nodes(subgraph(reviewer))
    assert {match.entity_id for match in matches} == {PERSON, OTHER_PERSON}
    reviewer.require_write()


# --- auditor ---------------------------------------------------------------------------


def test_an_auditor_reads_and_cannot_write(view):
    auditor = view("auditor")
    assert auditor.get_entity_by_id(PERSON).legal_name == "Dmitri Volkanov"
    with pytest.raises(PermissionError, match="read-only"):
        auditor.require_write()


def test_time_travel_reads_the_set_a_decision_was_made_on(con, graph_t0, ontology, gold):
    t1 = build_graph(con, ontology, T1)
    scenario = next(s for s in gold if s.id == "SCEN-01")

    def designated(view):
        found = view.get_sanctioned_nodes(view.get_ownership_subgraph(scenario.target_id, 5, 1))
        return [match.entity_id for match in found]

    with GraphView.time_travel("auditor", graph_t0, ontology, con) as then:
        assert then.store.set_id == graph_t0
        assert designated(then) == []
    with GraphView.open("auditor", "latest", ontology, con) as now:
        assert now.store.set_id == t1
        assert designated(now) == ["lei:FXS01PARENT000000000"]


# --- engineer --------------------------------------------------------------------------


def test_an_engineer_sees_what_was_built_and_none_of_the_graph(store, view):
    engineer = view("engineer")
    info = engineer.build_info()
    assert info["snapshot_set_id"] == store.set_id
    assert info["gold"]["gold_digest"] == store.gold_digest
    assert info["gold"]["silver_digest"] == info["silver"]["silver_digest"]
    assert info["gold"]["counts"]["graph_nodes"] == info["silver"]["counts"]["entities"]

    person = queries.get_entity_by_id(store, PERSON)
    refused = [
        lambda: engineer.resolve_entity("Acme Trading FZE"),
        lambda: engineer.get_ownership_subgraph(TARGET, 5, 1),
        lambda: engineer.get_sanctioned_nodes(queries.get_ownership_subgraph(store, TARGET, 5, 1)),
        lambda: engineer.get_reporting_exceptions(TARGET),
        lambda: engineer.get_entity_by_id(TARGET),
        lambda: engineer.get_direct_owners(TARGET),
        lambda: engineer.get_affected_entities(PERSON, 5),
        lambda: engineer.get_source_record(person.provenance),
    ]
    for call in refused:
        with pytest.raises(PermissionError, match="metadata only"):
            call()


def test_a_view_offers_exactly_the_allow_listed_queries(store, ontology):
    offered = {name for name in vars(GraphView) if name.startswith(("get_", "resolve_"))}
    assert offered == set(queries.QUERIES)
    with pytest.raises(PermissionError, match="Unknown role"):
        GraphView(store, "intern", ontology)
