import copy
from typing import get_args

import pytest
import yaml
from pydantic import ValidationError

from ubo_sentinel.cli.app import ONTOLOGY_PATH
from ubo_sentinel.models import (
    DecisionStatus,
    Entity,
    EntityType,
    Provenance,
    Relationship,
    RelType,
    Role,
    Sanction,
    load_ontology,
)
from ubo_sentinel.models.ontology import DecisionRight, OntologyConfig

PROVENANCE_CARRIERS = {"provenance", "supporting_records"}


@pytest.fixture
def shipped():
    return load_ontology(ONTOLOGY_PATH)


@pytest.fixture
def raw():
    return yaml.safe_load(ONTOLOGY_PATH.read_text(encoding="utf-8"))


def own_fields(model):
    return set(model.model_fields) - PROVENANCE_CARRIERS


# --- the shipped file -------------------------------------------------------


def test_shipped_ontology_is_valid(shipped):
    assert isinstance(shipped, OntologyConfig)


def test_labels_edge_types_and_roles_match_the_models(shipped):
    assert set(shipped.node_labels) == set(get_args(EntityType))
    assert set(shipped.edge_types) == set(get_args(RelType))
    assert set(shipped.roles) == set(get_args(Role))


def test_properties_match_the_models(shipped):
    for label, spec in shipped.node_labels.items():
        assert set(spec.properties) == own_fields(Entity), label
    for rel_type, spec in shipped.edge_types.items():
        assert set(spec.properties) == own_fields(Relationship), rel_type
    assert set(shipped.designations.properties) == own_fields(Sanction)
    assert shipped.provenance.fields == list(Provenance.model_fields)


def test_decision_rights_are_the_review_transitions():
    assert set(get_args(DecisionRight)) == set(get_args(DecisionStatus)) - {"RECOMMENDED"}


def test_edge_directions_and_endpoints(shipped):
    edges = shipped.edge_types
    assert (edges["OWNS"].subject.role, edges["OWNS"].object.role) == ("owner", "asset")
    assert (edges["CONSOLIDATED_BY"].subject.role, edges["CONSOLIDATED_BY"].object.role) == (
        "child",
        "parent",
    )
    assert (edges["CONTROLS"].subject.role, edges["CONTROLS"].object.role) == (
        "controller",
        "controlled",
    )
    assert edges["CONSOLIDATED_BY"].subject.labels == ["LegalEntity"]
    assert set(edges["OWNS"].subject.labels) == {"LegalEntity", "Person"}
    for spec in edges.values():
        assert spec.object.labels == ["LegalEntity"]


def test_designations_are_a_table_keyed_by_node(shipped):
    assert shipped.designations.table == "graph_sanctions"
    assert shipped.designations.keyed_by == "node_id"


def test_role_matrix(shipped):
    roles = shipped.roles
    assert roles["analyst"].masked_fields == {
        "Person": ["legal_name", "aliases", "registration_number"]
    }
    assert roles["reviewer"].masked_fields == {}
    assert roles["auditor"].read_only is True
    assert set(roles["reviewer"].decision_rights) == {"APPROVED", "OVERRIDDEN", "ESCALATED"}
    for name in ("analyst", "auditor", "engineer"):
        assert roles[name].decision_rights == []


# --- malformed files --------------------------------------------------------


def _missing_role(data):
    del data["roles"]["auditor"]


def _extra_role(data):
    data["roles"]["intern"] = copy.deepcopy(data["roles"]["engineer"])


def _unknown_top_level_key(data):
    data["colour"] = "blue"


def _unknown_nested_key(data):
    data["roles"]["analyst"]["can_delete"] = True


def _missing_node_label(data):
    del data["node_labels"]["Person"]


def _missing_edge_type(data):
    del data["edge_types"]["CONTROLS"]


def _edge_without_direction(data):
    del data["edge_types"]["OWNS"]["object"]


def _edge_without_endpoints(data):
    data["edge_types"]["OWNS"]["subject"]["labels"] = []


def _edge_with_unknown_label(data):
    data["edge_types"]["OWNS"]["subject"]["labels"] = ["Trust"]


def _missing_provenance_field(data):
    data["provenance"]["fields"].remove("as_of")


def _masked_field_not_a_property(data):
    data["roles"]["analyst"]["masked_fields"]["Person"].append("date_of_birth")


def _decision_rights_on_non_reviewer(data):
    data["roles"]["analyst"]["decision_rights"] = ["APPROVED"]


def _unknown_decision_right(data):
    data["roles"]["reviewer"]["decision_rights"] = ["DELETED"]


def _role_without_commands(data):
    del data["roles"]["engineer"]["commands"]


@pytest.mark.parametrize(
    "mutate",
    [
        _missing_role,
        _extra_role,
        _unknown_top_level_key,
        _unknown_nested_key,
        _missing_node_label,
        _missing_edge_type,
        _edge_without_direction,
        _edge_without_endpoints,
        _edge_with_unknown_label,
        _missing_provenance_field,
        _masked_field_not_a_property,
        _decision_rights_on_non_reviewer,
        _unknown_decision_right,
        _role_without_commands,
    ],
    ids=lambda mutate: mutate.__name__.lstrip("_"),
)
def test_malformed_ontology_is_rejected(raw, tmp_path, mutate):
    mutate(raw)
    path = tmp_path / "ontology.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_ontology(path)


def test_unmutated_copy_is_accepted(raw, tmp_path):
    path = tmp_path / "ontology.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_ontology(path) == load_ontology(ONTOLOGY_PATH)


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "ontology.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_ontology(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        load_ontology(tmp_path / "missing.yaml")


def test_commands_may_be_added_to_a_role(raw, tmp_path):
    raw["roles"]["engineer"]["commands"] = ["ingest"]
    path = tmp_path / "ontology.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_ontology(path).roles["engineer"].commands == ["ingest"]
