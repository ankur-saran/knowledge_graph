"""GLEIF: the loader's declarations and the mappers, from the sample files to Gold."""

import shutil

import pytest
from e2e import cli
from real_format import SAMPLES, lei, restamp

from ubo_sentinel.pipeline.bronze import resolve_set
from ubo_sentinel.pipeline.entity_linking import KEY_SUCCESSOR_LEI, resolve_groups
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders import LOADERS
from ubo_sentinel.pipeline.loaders.gleif_loader import GLEIF_DATASETS
from ubo_sentinel.pipeline.loaders.real_base import ingest_real
from ubo_sentinel.pipeline.loaders.real_loader import REAL_DATASETS, ingest_gleif
from ubo_sentinel.pipeline.mappers import MAPPERS
from ubo_sentinel.pipeline.transformer import build_silver


def build(con, ontology, path=SAMPLES):
    """GLEIF's datasets alone, as a set of their own: what the GLEIF mappers make."""
    names = [d.name for d in GLEIF_DATASETS]
    snapshot_set = ingest_real(con, path, GLEIF_DATASETS, names).snapshot_set
    summary = build_silver(con, snapshot_set, ontology)
    build_gold(con, snapshot_set)
    return snapshot_set.snapshot_set_id, summary.counts


@pytest.fixture
def built(con, ontology):
    return build(con, ontology)


def entities(con, set_id):
    return {
        row[0]: row[1:]
        for row in con.execute(
            "SELECT id, lei, registration_authority_id, registration_number, jurisdiction,"
            " status, lei_status, entity_type FROM silver_entities WHERE snapshot_set_id = ?",
            [set_id],
        ).fetchall()
    }


def test_every_gleif_dataset_has_a_mapper_and_a_loader():
    assert {d.name for d in GLEIF_DATASETS} <= set(MAPPERS)
    assert {d.name for d in GLEIF_DATASETS} <= set(REAL_DATASETS)
    assert LOADERS["gleif"] is ingest_gleif


def test_lei_records_become_entities(con, built):
    set_id, counts = built
    found = entities(con, set_id)
    assert counts["entity_records"] == 20 and counts["entities"] == 19
    # lei, authority, number, jurisdiction, status, lei_status, type
    assert found[f"lei:{lei('HOLD')}"] == (
        lei("HOLD"),
        "RA000585",
        "FXR-HOLD",
        "GB",
        "ACTIVE",
        "ISSUED",
        "LegalEntity",
    )
    # A subdivision is not a jurisdiction of its own.
    assert found[f"lei:{lei('ORV')}"][3] == "US"
    # The entity is inactive and its LEI retired: two statuses, both kept.
    assert found[f"lei:{lei('ELS')}"][4:6] == ("INACTIVE", "RETIRED")
    assert found[f"lei:{lei('HALV')}"][4:6] == ("ACTIVE", "LAPSED")
    # GLEIF writes NULL for an entity status it does not know.
    assert found[f"lei:{lei('ARDV')}"][4] == "UNKNOWN"
    # A sole proprietor is a person; a code that names no register is not a key.
    assert found[f"lei:{lei('SOLE')}"] == (
        lei("SOLE"),
        None,
        None,
        "FR",
        "ACTIVE",
        "ISSUED",
        "Person",
    )
    assert found[f"lei:{lei('ARDV')}"][1:3] == (None, "FXR-ARDV")


def test_other_and_transliterated_names_are_aliases(con, built):
    set_id, _ = built
    aliases = dict(
        con.execute(
            "SELECT id, aliases FROM silver_entities WHERE snapshot_set_id = ?", [set_id]
        ).fetchall()
    )
    assert aliases[f"lei:{lei('SUB')}"] == ["Velling Energia SA", "Velling Iberia S.A."]
    assert aliases[f"lei:{lei('THAL')}"] == ["THALASSOVIA NAFTILIAKI A.E."]
    assert aliases[f"lei:{lei('PEMB')}"] == ['"Pemberwick" Advisory']


def test_a_duplicate_lei_is_its_successor(con, built):
    """The successor's LEI names the entity, and its record describes it."""
    set_id, counts = built
    found = entities(con, set_id)
    assert f"lei:{lei('DUP')}" not in found
    assert found[f"lei:{lei('KOLM')}"][0] == lei("KOLM")
    assert found[f"lei:{lei('KOLM')}"][4:6] == ("ACTIVE", "ISSUED")
    assert counts["merged_records"] == 1
    assert con.execute(
        "SELECT entity_id, joined_by, is_primary FROM silver_entity_xref"
        " WHERE snapshot_set_id = ? AND source_record_id = ?",
        [set_id, lei("DUP")],
    ).fetchone() == (f"lei:{lei('KOLM')}", KEY_SUCCESSOR_LEI, False)
    # The two share a register entry too; that join is refused (two LEIs) and
    # leaves no link, since they are one entity.
    assert counts["links"] == 0


def test_a_successor_of_another_type_is_linked_not_merged():
    roots, refused = resolve_groups(
        [(KEY_SUCCESSOR_LEI, "L2", ["gleif:A", "gleif:B"])],
        {"gleif:A": ("L1", "Person"), "gleif:B": ("L2", "LegalEntity")},
    )
    assert roots == {"gleif:A": "gleif:A", "gleif:B": "gleif:B"}
    assert refused == [("gleif:A", "gleif:B", KEY_SUCCESSOR_LEI)]
    roots, refused = resolve_groups(
        [(KEY_SUCCESSOR_LEI, "L2", ["gleif:A", "gleif:B"])],
        {"gleif:A": ("L1", "LegalEntity"), "gleif:B": ("L2", "LegalEntity")},
    )
    assert roots == {"gleif:A": "gleif:A", "gleif:B": "gleif:A"} and refused == []


def test_relationship_records_become_edges(con, built):
    set_id, counts = built
    edges = {
        (subject.removeprefix("lei:"), obj.removeprefix("lei:"), rel_type, basis, ultimate)
        for subject, obj, rel_type, basis, ultimate in con.execute(
            "SELECT subject_id, object_id, rel_type, basis, is_ultimate"
            " FROM silver_relationships WHERE snapshot_set_id = ? AND pct IS NULL",
            [set_id],
        ).fetchall()
    }
    consolidation = "accounting_consolidation"
    assert edges == {
        (lei("MID"), lei("HOLD"), "CONSOLIDATED_BY", consolidation, False),
        (lei("MID"), lei("HOLD"), "CONSOLIDATED_BY", consolidation, True),
        (lei("SUB"), lei("MID"), "CONSOLIDATED_BY", consolidation, False),
        (lei("SUB"), lei("HOLD"), "CONSOLIDATED_BY", consolidation, True),
        (lei("ORVDE"), lei("ORV"), "CONSOLIDATED_BY", consolidation, False),
        (lei("ORVDE"), lei("ORV"), "CONSOLIDATED_BY", consolidation, True),
        # A branch is consolidated into its head office, with no percentage.
        (lei("BRANCH"), lei("MID"), "CONSOLIDATED_BY", "other", False),
        (lei("SUBF"), lei("FUND"), "CONSOLIDATED_BY", "other", False),
        # The manager controls the fund: the edge runs from the end node.
        (lei("MGR"), lei("FUND"), "CONTROLS", "other", False),
    }
    assert counts["relationships"] == len(edges)


def test_what_is_not_an_edge_is_counted(con, built):
    _, counts = built
    assert counts["filtered"] == {
        "gleif_rr": {"INACTIVE_RELATIONSHIP": 2, "UNSUPPORTED_REL_TYPE": 1},
        "gleif_repex": {"DELETED": 1},
    }
    # A parent with no LEI record in the set.
    assert counts["rejects"] == {"UNRESOLVED_REF": 1}


def test_reporting_exceptions(con, built):
    set_id, _ = built
    rows = con.execute(
        "SELECT entity_id, category, reason FROM silver_reporting_exceptions"
        " WHERE snapshot_set_id = ? ORDER BY ALL",
        [set_id],
    ).fetchall()
    assert (f"lei:{lei('MGR')}", "DIRECT_PARENT", "NON_CONSOLIDATING; NO_KNOWN_PERSON") in rows
    assert (f"lei:{lei('KOLM')}", "ULTIMATE_PARENT", "NON_PUBLIC") in rows
    # Deleted by GLEIF: not an exception any more.
    assert not [row for row in rows if row[0] == f"lei:{lei('THAL')}"]
    assert len(rows) == 9


def test_a_status_gleif_does_not_define_is_counted_not_kept(con, ontology, tmp_path):
    samples = shutil.copytree(SAMPLES, tmp_path / "refresh")
    path = samples / "gleif/lei2.csv"
    data = path.read_bytes()
    path.write_bytes(data.replace(b'"LAPSED"', b'"WITHDRAWN"', 1))
    restamp(samples)
    set_id, counts = build(con, ontology, samples)
    assert counts["unreadable"] == {"jurisdiction": 0, "lei_status": 1, "pct": 0}
    assert entities(con, set_id)[f"lei:{lei('HALV')}"][5] is None


def test_a_gleif_ingest_needs_a_directory():
    result = cli("ingest", "--source", "gleif")
    assert result.exit_code == 1
    assert "pass --path" in result.output


def test_the_family_is_real(con, built):
    set_id, _ = built
    assert resolve_set(con, set_id).family == "real"
