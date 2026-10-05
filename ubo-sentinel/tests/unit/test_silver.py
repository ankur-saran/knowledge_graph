"""Silver: canonical ids, merging, linking, edge reconciliation and determinism."""

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ubo_sentinel.cli.app import app
from ubo_sentinel.models import canonical_json
from ubo_sentinel.pipeline import transformer
from ubo_sentinel.pipeline.bronze import BRONZE_TABLES, resolve_set
from ubo_sentinel.pipeline.db import DB_ENV_VAR, connect
from ubo_sentinel.pipeline.entity_linking import LINK_FLOOR, resolve_groups
from ubo_sentinel.pipeline.loaders.fixture_loader import DATASETS, ingest_fixtures
from ubo_sentinel.pipeline.mappers import MAPPERS
from ubo_sentinel.pipeline.name_match import normalise_name
from ubo_sentinel.pipeline.silver import (
    SILVER_TABLES,
    SilverError,
    is_built,
    read_entities,
    read_relationships,
    read_reporting_exceptions,
    read_rows,
    read_sanctions,
    silver_digest,
)
from ubo_sentinel.pipeline.transformer import build_silver

# Commands are run from the repository root.
T0 = Path("fixtures/snapshot_t0")
T1 = Path("fixtures/snapshot_t1")

# The OFAC pack's match threshold (BUILD_PLAN 6.1). Read it from
# `rules/ofac.yaml` once Step 6 has created it.
MATCH_THRESHOLD = 0.92

runner = CliRunner()


@pytest.fixture
def t0(con, ontology):
    """The t0 fixtures, ingested and normalised. Returns the set id."""
    return build(con, ontology)


def build(con, ontology, path=None, **kwargs):
    snapshot_set = ingest_fixtures(con, path).snapshot_set
    build_silver(con, snapshot_set, ontology, **kwargs)
    return snapshot_set.snapshot_set_id


def append(path, *lines):
    with path.open("a", encoding="utf-8", newline="") as f:
        f.writelines(line + "\n" for line in lines)


def by_id(rows):
    return {row.id: row for row in rows}


def links(con, set_id):
    return {
        (row["entity_id_a"], row["entity_id_b"]): row
        for row in read_rows(con, "silver_entity_links", set_id)
    }


def assert_no_silver_rows(con):
    for table in (*SILVER_TABLES, "silver_builds"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


# --- canonical rows -----------------------------------------------------------------


def test_every_row_is_a_canonical_model_whose_provenance_is_a_bronze_row(con, t0):
    bronze_keys = set()
    for table in BRONZE_TABLES:
        bronze_keys.update(
            con.execute(f"SELECT _snapshot_id, _source, _source_record_id FROM {table}").fetchall()
        )
    snapshot_ids = set(resolve_set(con, t0).snapshot_ids.values())

    rows = [
        *read_entities(con, t0),
        *read_relationships(con, t0),
        *read_sanctions(con, t0),
        *read_reporting_exceptions(con, t0),
    ]
    assert rows
    for row in rows:
        cited = [row.provenance, *getattr(row, "supporting_records", [])]
        for provenance in cited:
            key = (provenance.snapshot_id, provenance.source, provenance.source_record_id)
            assert key in bronze_keys, row
            assert provenance.snapshot_id in snapshot_ids, row
        # The record a row is taken from is one of the records that support it.
        if hasattr(row, "supporting_records"):
            assert row.provenance in row.supporting_records, row


def test_json_columns_are_canonical_json(con, t0):
    for table, column in [
        ("silver_entities", "supporting_records"),
        ("silver_relationships", "supporting_records"),
        ("silver_gaps", "detail"),
    ]:
        for row in read_rows(con, table, t0):
            assert row[column].encode("utf-8") == canonical_json(json.loads(row[column])), table


def test_staging_columns_are_the_fixture_row_fields(con, t0):
    for dataset in DATASETS.values():
        table = MAPPERS[dataset.name].stage_table
        columns = [row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()]
        # After the dataset and the provenance come the row's own fields.
        assert columns[6:] == list(dataset.model.model_fields)[4:], table


# --- entities -----------------------------------------------------------------------


def test_canonical_ids_are_the_ones_the_files_imply(con, t0, canonical):
    xref = {
        f"{row['source']}:{row['source_record_id']}": row["entity_id"]
        for row in read_rows(con, "silver_entity_xref", t0)
    }
    assert xref == canonical
    assert {entity.id for entity in read_entities(con, t0)} == set(canonical.values())


def test_each_entity_has_one_describing_record(con, t0):
    primary = Counter(
        row["entity_id"] for row in read_rows(con, "silver_entity_xref", t0) if row["is_primary"]
    )
    assert set(primary.values()) == {1}
    assert len(primary) == len(read_entities(con, t0))


def test_a_merged_entity_is_described_by_its_registry_record(con, t0):
    entities = by_id(read_entities(con, t0))

    # Merged by LEI. The list's record is newer, and still does not describe the entity.
    vostrek = entities["lei:FXS02TARGET000000000"]
    assert vostrek.legal_name == "Vostrek Maritime LLC"
    assert vostrek.aliases == ["Vostrek Marine", "Vostrek Maritime L.L.C."]
    assert vostrek.provenance.source == "fx_registry"
    assert [p.source for p in vostrek.supporting_records] == ["fx_list", "fx_registry"]

    # Merged by registration authority and number.
    zhelezny = entities["lei:FXS06TOP000000000000"]
    assert zhelezny.legal_name == "Zhelezny Bereg Invest OOO"
    assert "Zhelezny Bereg Invest" in zhelezny.aliases
    assert (zhelezny.registration_authority_id, zhelezny.registration_number) == (
        "RA-FX-RU",
        "06-TOP",
    )


def test_status_is_kept_as_published(con, t0, entities, canonical):
    silver = by_id(read_entities(con, t0))
    for row in entities:
        if list(canonical.values()).count(canonical[row.ref]) == 1:
            assert silver[canonical[row.ref]].status == row.status
    assert {"LAPSED", "RETIRED"} <= {entity.status for entity in silver.values()}


def test_registry_status_survives_a_newer_list_record(con, ontology, t0_copy):
    path = t0_copy / "entities.csv"
    text = path.read_text(encoding="utf-8")
    changed = text.replace(
        "02-TARGET,Vostrek Maritime LLC,,AE,ACTIVE", "02-TARGET,Vostrek Maritime LLC,,AE,LAPSED"
    )
    assert changed != text
    path.write_text(changed, encoding="utf-8", newline="")

    entity = by_id(read_entities(con, build(con, ontology, t0_copy)))["lei:FXS02TARGET000000000"]
    assert entity.status == "LAPSED"


def test_names_hold_every_name_with_its_normalised_form(con, t0):
    names = read_rows(con, "silver_entity_names", t0)
    for row in names:
        assert row["name_norm"] == normalise_name(row["name"])
    for entity in read_entities(con, t0):
        own = {row["name"]: row["kind"] for row in names if row["entity_id"] == entity.id}
        assert own == {entity.legal_name: "legal_name", **dict.fromkeys(entity.aliases, "alias")}


def test_repex_reason_summarises_the_reporting_exceptions(con, t0):
    entities = by_id(read_entities(con, t0))
    exceptions = read_reporting_exceptions(con, t0)
    assert len(exceptions) == 4
    assert entities["lei:FXS00HUB000000000000"].repex_reason == (
        "DIRECT_PARENT:NATURAL_PERSONS; ULTIMATE_PARENT:NATURAL_PERSONS"
    )
    assert entities["lei:FXS12TARGET000000000"].repex_reason == "DIRECT_PARENT:NON_PUBLIC"
    with_exception = {exception.entity_id for exception in exceptions}
    assert {id_ for id_, entity in entities.items() if entity.repex_reason} == with_exception


# --- merge guard --------------------------------------------------------------------


def test_two_leis_that_share_a_registration_are_linked_not_merged(con, ontology, t0_copy):
    append(
        t0_copy / "entities.csv",
        "fx_registry,s99-twin,2026-09-01,1.0,FXS99TWIN00000000000,RA-FX-AE,02-TARGET,"
        "Zennor Unrelated Castings Ltd,,AE,ACTIVE,LegalEntity",
    )
    set_id = build(con, ontology, t0_copy)

    entities = by_id(read_entities(con, set_id))
    assert {"lei:FXS02TARGET000000000", "lei:FXS99TWIN00000000000"} <= set(entities)
    link = links(con, set_id)["lei:FXS02TARGET000000000", "lei:FXS99TWIN00000000000"]
    assert (link["match_type"], link["confidence"]) == ("REGISTRATION", 1.0)

    # The designation of the first reaches the second through the link.
    reached = {
        sanction.entity_id: sanction.match_type
        for sanction in read_sanctions(con, set_id)
        if sanction.provenance.source_record_id == "s02-des"
    }
    assert reached == {
        "lei:FXS02TARGET000000000": "LEI",
        "lei:FXS99TWIN00000000000": "REGISTRATION",
    }
    # Both rows say whose designation it is, which tells the refused merge apart.
    assert {
        sanction.designated_entity_id
        for sanction in read_sanctions(con, set_id)
        if sanction.provenance.source_record_id == "s02-des"
    } == {"lei:FXS02TARGET000000000"}


def test_resolve_groups_keeps_the_first_join_and_refuses_the_conflicting_one():
    attributes = {
        "a": ("LEI1", "LegalEntity"),
        "b": ("LEI2", "LegalEntity"),
        "c": (None, "LegalEntity"),
        "d": (None, "Person"),
    }
    roots, refused = resolve_groups([("REGISTRATION", "key", ["d", "c", "b", "a"])], attributes)
    assert roots == {"a": "a", "b": "b", "c": "a", "d": "d"}
    assert refused == [("a", "b", "REGISTRATION"), ("a", "d", "REGISTRATION")]


# --- links --------------------------------------------------------------------------


def test_declared_name_pairs_are_linked_and_no_other_pair_is(con, t0, gold):
    found = links(con, t0)
    entity_ids = {entity.id for entity in read_entities(con, t0)}
    expected = set()
    for scenario in gold:
        for pair in scenario.declared_name_pairs:
            key = tuple(sorted([pair.a, pair.b]))
            # A declared pair is two entities: similar names never merge records.
            assert set(key) <= entity_ids, scenario.id
            if pair.band == "homonym":
                assert key not in found, scenario.id
                continue
            expected.add(key)
            link = found[key]
            assert link["match_type"] == "FUZZY_NAME"
            if pair.band == "review":
                assert LINK_FLOOR <= link["confidence"] < MATCH_THRESHOLD, scenario.id
            else:
                assert link["confidence"] >= MATCH_THRESHOLD, scenario.id
    assert set(found) == expected


# --- edges --------------------------------------------------------------------------


def test_there_is_one_edge_per_owner_asset_and_relation(con, t0):
    edges = read_relationships(con, t0)
    keys = Counter((e.subject_id, e.object_id, e.rel_type, e.is_ultimate) for e in edges)
    assert set(keys.values()) == {1}

    # SCEN-18: two sources report the same 30 % stake of one owner.
    (stake,) = [
        e
        for e in edges
        if (e.subject_id, e.object_id) == ("lei:FXS18OWNER0000000000", "lei:FXS18TARGET000000000")
    ]
    assert str(stake.pct) == "30.0000"
    assert [p.source_record_id for p in stake.supporting_records] == ["s18-rel-2", "s18-rel-1"]
    assert stake.provenance.source_record_id == "s18-rel-2"


def test_the_newest_record_with_a_percentage_decides_an_edge(con, ontology, t0_copy):
    edge = "fx_registry:s01-parent,fx_registry:s01-target,OWNS"
    append(
        t0_copy / "relationships.csv",
        f"fx_list,s01-rel-8,2026-09-18,1.0,{edge},55,shareholding,false",
        f"fx_list,s01-rel-9,2026-09-20,1.0,{edge},,shareholding,false",
    )
    set_id = build(con, ontology, t0_copy)
    (stake,) = [
        e
        for e in read_relationships(con, set_id)
        if (e.subject_id, e.object_id) == ("lei:FXS01PARENT000000000", "lei:FXS01TARGET000000000")
    ]
    assert str(stake.pct) == "55.0000"
    assert stake.provenance.source_record_id == "s01-rel-8"
    assert len(stake.supporting_records) == 3


def test_edges_that_cannot_be_loaded_are_rejected_with_a_reason(con, ontology, t0_copy):
    append(
        t0_copy / "relationships.csv",
        "fx_list,s99-rel-1,2026-09-15,1.0,fx_list:s99-missing,fx_registry:s01-target,OWNS,5,"
        "shareholding,false",
        "fx_list,s99-rel-2,2026-09-15,1.0,fx_list:s02-listed,fx_registry:s02-target,OWNS,5,"
        "shareholding,false",
        "fx_list,s99-rel-3,2026-09-15,1.0,fx_registry:s01-parent,fx_list:s01-minority,OWNS,5,"
        "shareholding,false",
    )
    append(
        t0_copy / "sanctions.csv",
        "fx_list,s99-des,2026-09-15,1.0,fx_list:s99-missing,FX-PROGRAM-ALPHA,2024-01-01,"
        "FX-OFAC-SDN,true",
    )
    append(
        t0_copy / "repex.csv",
        "fx_registry,s99-repex,2026-09-01,1.0,fx_registry:s99-missing,DIRECT_PARENT,NON_PUBLIC",
    )
    set_id = build(con, ontology, t0_copy)

    rejects = {
        (row["dataset"], row["source_record_id"]): (row["reason"], row["detail"])
        for row in read_rows(con, "silver_rejects", set_id)
    }
    assert rejects == {
        ("relationships", "s99-rel-1"): ("UNRESOLVED_REF", "fx_list:s99-missing"),
        ("relationships", "s99-rel-2"): ("SELF_LOOP_AFTER_MERGE", "lei:FXS02TARGET000000000"),
        ("relationships", "s99-rel-3"): ("ENDPOINT_LABEL", "LegalEntity OWNS Person"),
        ("sanctions", "s99-des"): ("UNRESOLVED_REF", "fx_list:s99-missing"),
        ("repex", "s99-repex"): ("UNRESOLVED_REF", "fx_registry:s99-missing"),
    }
    # Nothing rejected was loaded, and nothing else was lost.
    assert len(read_relationships(con, set_id)) == 71
    assert len(read_sanctions(con, set_id)) == 28
    assert len(read_reporting_exceptions(con, set_id)) == 4


# --- designations -------------------------------------------------------------------


def test_match_type_says_how_a_designation_reaches_its_entity(con, t0):
    reached = {
        (sanction.provenance.source_record_id, sanction.entity_id): sanction
        for sanction in read_sanctions(con, t0)
    }
    expected = {
        ("s02-des", "lei:FXS02TARGET000000000"): "LEI",
        ("s06-des", "lei:FXS06TOP000000000000"): "REGISTRATION",
        ("s03-des-a", "fx_list:s03-owner-a"): "DIRECT",
        ("s14-des", "fx_list:s14-listed"): "DIRECT",
        ("s14-des", "lei:FXS14OWNER0000000000"): "FUZZY_NAME",
        ("s26-des", "lei:FXS26OWNER0000000000"): "FUZZY_NAME",
    }
    for key, match_type in expected.items():
        assert reached[key].match_type == match_type, key

    # A row that crossed a link names the designated entity; every other row is its own.
    for (_, entity_id), sanction in reached.items():
        crossed = sanction.match_type == "FUZZY_NAME"
        assert (sanction.designated_entity_id != entity_id) == crossed
    assert (
        reached["s14-des", "lei:FXS14OWNER0000000000"].designated_entity_id == "fx_list:s14-listed"
    )

    for sanction in reached.values():
        if sanction.match_type == "FUZZY_NAME":
            assert LINK_FLOOR <= sanction.match_confidence <= 1
        else:
            assert sanction.match_confidence == 1
    review = reached["s14-des", "lei:FXS14OWNER0000000000"]
    assert review.match_confidence < MATCH_THRESHOLD
    assert reached["s26-des", "lei:FXS26OWNER0000000000"].match_confidence >= MATCH_THRESHOLD
    assert len({sanction.id for sanction in reached.values()}) == len(reached) == 28


# --- gold scenarios -----------------------------------------------------------------


def test_silver_holds_everything_the_gold_scenarios_name(con, t0, gold):
    entity_ids = {entity.id for entity in read_entities(con, t0)}
    edge_ids = {edge.id for edge in read_relationships(con, t0)}
    for scenario in gold:
        nodes = {*scenario.expected_blocked, *scenario.expected_possibly_blocked}
        if scenario.target_id is not None:
            nodes.add(scenario.target_id)
        for path in scenario.expected_paths:
            nodes.update(path.nodes)
            assert set(path.to_evidence_path().edge_ids) <= edge_ids, scenario.id
        for gap in scenario.expected_gaps:
            if gap.node is not None:
                nodes.add(gap.node)
            else:
                assert gap.subject_id() in edge_ids, scenario.id
        assert nodes <= entity_ids, scenario.id


def test_stakes_over_100_are_a_gap_exactly_where_gold_says(con, t0, gold):
    declared = {
        gap.node
        for scenario in gold
        for gap in scenario.expected_gaps
        if gap.code == "PCT_SUM_OVER_100"
    }
    gaps = read_rows(con, "silver_gaps", t0)
    assert {gap["subject_id"] for gap in gaps} == declared
    assert {gap["code"] for gap in gaps} == {"PCT_SUM_OVER_100"}
    assert [json.loads(gap["detail"]) for gap in gaps] == [{"sum_pct": "110.0000"}]


# --- determinism and rebuilds -------------------------------------------------------


def test_the_same_files_give_the_same_silver_in_another_database(
    con, t0, ontology, tmp_path, monkeypatch
):
    monkeypatch.setenv(DB_ENV_VAR, str(tmp_path / "other.duckdb"))
    other = connect()
    try:
        assert build(other, ontology) == t0
        assert silver_digest(other, t0) == silver_digest(con, t0)
        for table in SILVER_TABLES:
            assert read_rows(other, table, t0) == read_rows(con, table, t0)
    finally:
        other.close()


def test_building_a_built_set_changes_nothing(con, t0, ontology):
    snapshot_set = resolve_set(con, t0)
    built_at = con.execute("SELECT built_at FROM silver_builds").fetchall()
    digest = silver_digest(con, t0)

    again = build_silver(con, snapshot_set, ontology)
    assert again.status == "already built"
    assert again.silver_digest == digest == silver_digest(con, t0)
    assert con.execute("SELECT built_at FROM silver_builds").fetchall() == built_at

    rebuilt = build_silver(con, snapshot_set, ontology, rebuild=True)
    assert (rebuilt.status, rebuilt.silver_digest, rebuilt.counts) == (
        "rebuilt",
        digest,
        again.counts,
    )


def test_a_set_built_by_other_code_is_not_reused(con, t0, ontology, monkeypatch):
    snapshot_set = resolve_set(con, t0)
    monkeypatch.setattr(transformer, "TRANSFORM_VERSION", transformer.TRANSFORM_VERSION + 1)
    with pytest.raises(SilverError, match="--rebuild"):
        build_silver(con, snapshot_set, ontology)
    assert build_silver(con, snapshot_set, ontology, rebuild=True).status == "rebuilt"
    assert build_silver(con, snapshot_set, ontology).status == "already built"


def test_two_sets_live_side_by_side(con, t0, ontology):
    before = {table: read_rows(con, table, t0) for table in SILVER_TABLES}
    t1 = build(con, ontology, T1)
    assert t1 != t0 and is_built(con, t0) and is_built(con, t1)
    assert {table: read_rows(con, table, t0) for table in SILVER_TABLES} == before

    def designations(set_id):
        return {
            sanction.provenance.source_record_id: sanction
            for sanction in read_sanctions(con, set_id)
            if sanction.match_type != "FUZZY_NAME"
        }

    old, new = designations(t0), designations(t1)
    assert set(new) - set(old) == {"s01-des"}
    assert set(old) - set(new) == {"s08-des"}
    assert (old["s17-des"].is_active, new["s17-des"].is_active) == (False, True)
    assert (new["s01-des"].entity_id, new["s01-des"].match_type) == (
        "lei:FXS01PARENT000000000",
        "DIRECT",
    )
    # Only the list changed: the entities and edges of both sets are the same.
    assert read_entities(con, t1) == read_entities(con, t0)
    assert read_relationships(con, t1) == read_relationships(con, t0)


# --- refused builds -----------------------------------------------------------------


def test_bronze_rows_that_changed_since_ingest_are_refused(con, ontology):
    snapshot_set = ingest_fixtures(con).snapshot_set
    con.execute("UPDATE bronze_sanctions SET _row_hash = 'x' WHERE _source_record_id = 's02-des'")
    with pytest.raises(SilverError, match="not the rows that were ingested"):
        build_silver(con, snapshot_set, ontology)
    assert_no_silver_rows(con)


def test_a_source_without_a_rank_is_refused(con, ontology, t0_copy):
    append(
        t0_copy / "entities.csv",
        "fx_other,s99-new,2026-09-01,1.0,,,,Quorndon Saltworks Ltd,,GB,ACTIVE,LegalEntity",
    )
    snapshot_set = ingest_fixtures(con, t0_copy).snapshot_set
    with pytest.raises(SilverError, match="fx_other"):
        build_silver(con, snapshot_set, ontology)
    assert_no_silver_rows(con)


# --- command ------------------------------------------------------------------------


def test_normalize_command_prints_counts_and_is_idempotent(con):
    con.close()
    assert runner.invoke(app, ["ingest", "--source", "fixtures"]).exit_code == 0

    first = runner.invoke(app, ["normalize"])
    assert first.exit_code == 0, first.output
    for table in SILVER_TABLES:
        assert table in first.output
    assert "(built)" in first.output and "Digest:" in first.output

    second = runner.invoke(app, ["normalize", "--snapshot", "fixtures"])
    assert second.exit_code == 0, second.output
    assert second.output == first.output.replace("(built)", "(already built)")


def test_normalize_command_needs_a_known_set(con):
    con.close()
    assert runner.invoke(app, ["normalize"]).exit_code == 2
    assert runner.invoke(app, ["ingest", "--source", "fixtures"]).exit_code == 0
    result = runner.invoke(app, ["normalize", "--snapshot", "0000000000000000"])
    assert result.exit_code == 2
    assert "No snapshot set" in result.output


def test_normalize_command_is_for_engineers(con):
    con.close()
    assert runner.invoke(app, ["ingest", "--source", "fixtures"]).exit_code == 0
    assert runner.invoke(app, ["normalize", "--role", "analyst"]).exit_code == 1
