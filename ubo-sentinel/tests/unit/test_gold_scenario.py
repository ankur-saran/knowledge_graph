import pytest
import yaml
from pydantic import ValidationError

from ubo_sentinel.models import GoldScenario, load_gold_scenarios, relationship_id
from ubo_sentinel.models.fixture_rows import EntityRow, RelationshipRow, read_rows
from ubo_sentinel.models.gold_scenario import ExpectedGap, ExpectedPath


def make_scenario(**overrides):
    fields = {
        "id": "SCEN-01",
        "query": "Acme Trading FZE",
        "target_id": "t",
        "expected_recommendation": "CLEAR",
        "rationale": "No blocked owner.",
    }
    return GoldScenario(**(fields | overrides))


def make_relationship_row(**overrides):
    fields = {
        "source": "fx_registry",
        "source_record_id": "s01-rel-1",
        "as_of": "2026-09-01",
        "confidence": "1.0",
        "subject_ref": "fx_registry:s01-parent",
        "object_ref": "fx_registry:s01-target",
        "rel_type": "OWNS",
        "pct": "60",
        "basis": "shareholding",
        "is_ultimate": "false",
    }
    return RelationshipRow(**(fields | overrides))


# --- paths and gaps -----------------------------------------------------------


def test_path_derives_edge_ids_in_the_ontology_direction():
    path = ExpectedPath(nodes=["x", "i", "t"], via=["OWNS", "CONSOLIDATED_BY"])
    assert path.to_evidence_path().edge_ids == [
        relationship_id("x", "i", "OWNS", False),
        # Stored child -> parent, although the path runs parent -> child.
        relationship_id("t", "i", "CONSOLIDATED_BY", False),
    ]


def test_a_path_may_be_the_target_alone():
    assert ExpectedPath(nodes=["t"]).to_evidence_path().edge_ids == []


def test_path_needs_one_relation_per_hop():
    with pytest.raises(ValidationError):
        ExpectedPath(nodes=["x", "t"], via=[])


def test_gap_is_about_a_node_or_an_edge_not_both():
    edge = {"subject_id": "t", "object_id": "p", "rel_type": "CONSOLIDATED_BY"}
    assert ExpectedGap(code="LAPSED_LEI", node="i", decision_relevant=True).subject_id() == "i"
    assert ExpectedGap(
        code="NULL_PCT_EDGE", edge=edge, decision_relevant=True
    ).subject_id() == relationship_id("t", "p", "CONSOLIDATED_BY", False)
    with pytest.raises(ValidationError):
        ExpectedGap(code="LAPSED_LEI", decision_relevant=True)
    with pytest.raises(ValidationError):
        ExpectedGap(code="LAPSED_LEI", node="i", edge=edge, decision_relevant=True)


# --- scenario consistency -------------------------------------------------------


def test_escalate_is_expected_exactly_when_the_target_is_blocked():
    make_scenario(expected_recommendation="ESCALATE", expected_blocked=["t"])
    with pytest.raises(ValidationError, match="ESCALATE"):
        make_scenario(expected_recommendation="ESCALATE")
    with pytest.raises(ValidationError, match="ESCALATE"):
        make_scenario(expected_recommendation="REVIEW", expected_blocked=["t"])


def test_scenario_without_a_target_needs_the_pipeline_suite():
    make_scenario(target_id=None, suite="pipeline", expected_recommendation="REVIEW")
    with pytest.raises(ValidationError, match="rules suite"):
        make_scenario(target_id=None, expected_recommendation="REVIEW")


def test_scenario_rejects_unknown_fields_codes_and_overlapping_sets():
    with pytest.raises(ValidationError):
        make_scenario(pick=1)
    with pytest.raises(ValidationError):
        make_scenario(expected_reasons=["BECAUSE"])
    with pytest.raises(ValidationError, match="overlap"):
        make_scenario(expected_blocked=["a"], expected_possibly_blocked=["a"])
    with pytest.raises(ValidationError):
        make_scenario(id="SCENARIO-1")


def test_loader_rejects_duplicate_ids(tmp_path):
    entry = make_scenario().model_dump(mode="json")
    path = tmp_path / "gold.yaml"
    path.write_text(yaml.safe_dump([entry, entry]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_gold_scenarios(path)


# --- fixture rows -----------------------------------------------------------------


def test_relationship_row_reads_csv_strings():
    row = make_relationship_row()
    assert (str(row.pct), row.is_ultimate, row.ref) == ("60.0000", False, "fx_registry:s01-rel-1")


@pytest.mark.parametrize(
    "change",
    [
        {"object_ref": "fx_registry:s01-parent"},
        {"basis": "directorship"},
        {"is_ultimate": "true"},
        {"rel_type": "CONTROLS", "basis": "directorship"},
        {"pct": "0"},
    ],
    ids=["self_loop", "basis_mismatch", "ultimate_owns", "controls_with_pct", "zero_pct"],
)
def test_relationship_row_rejects(change):
    with pytest.raises(ValidationError):
        make_relationship_row(**change)


def test_read_rows_treats_empty_cells_as_null_and_checks_the_header(tmp_path):
    header = ",".join(EntityRow.model_fields)
    path = tmp_path / "entities.csv"
    path.write_text(
        f"{header}\nfx_list,s01-p,2026-09-15,1.0,,,,Jo Bloggs,J Bloggs|Joe,GB,UNKNOWN,Person\n",
        encoding="utf-8",
    )
    (row,) = read_rows(path, EntityRow)
    assert (row.lei, row.aliases, row.canonical_id()) == (
        None,
        ["J Bloggs", "Joe"],
        "fx_list:s01-p",
    )

    path.write_text(header.replace("lei,", "") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        read_rows(path, EntityRow)
