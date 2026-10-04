"""Integrity of the synthetic fixtures, read straight from the files (no pipeline code)."""

import csv
from collections import Counter, defaultdict
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import get_args

import pytest

from ubo_sentinel.cli.app import ONTOLOGY_PATH
from ubo_sentinel.models import (
    Basis,
    EntityRow,
    EntityStatus,
    EntityType,
    GapCode,
    ReasonCode,
    Recommendation,
    RelationshipRow,
    RelType,
    RepexCategory,
    RepexRow,
    SanctionRow,
    load_gold_scenarios,
    load_ontology,
    read_rows,
    relationship_id,
)
from ubo_sentinel.models.sanction import DESIGNATION_FIELDS
from ubo_sentinel.pipeline.name_match import name_similarity

# Commands are run from the repository root.
T0 = Path("fixtures/snapshot_t0")
T1 = Path("fixtures/snapshot_t1")
GOLD_PATH = Path("eval/gold_scenarios.yaml")
ER_PATH = Path("eval/er_labelled.csv")
DATA_FILES = sorted([*T0.glob("*.csv"), *T1.glob("*.csv"), GOLD_PATH, ER_PATH])

# The OFAC pack's match thresholds (BUILD_PLAN 6.1). Read them from
# `rules/ofac.yaml` once Step 6 has created it.
REVIEW_BAND_LOW = 0.75
MATCH_THRESHOLD = 0.92

# Reasons that only the agent pipeline can produce, so no fixture can cover them.
PIPELINE_FAILURE_REASONS = {"GUARDRAIL_FAILED", "PIPELINE_ERROR"}
BACKGROUND = "s00"
ER_COLUMNS = ["query_name", "lei", "jurisdiction", "expected_entity_id", "variant_type"]
ER_VARIANT_TYPES = {
    "exact",
    "case",
    "punctuation",
    "legal_form",
    "alias",
    "transliteration",
    "typo",
    "word_order",
    "homonym",
    "lei",
    "merged_record",
    "negative",
}


def scenario_of(record_id_or_ref: str) -> str:
    """`fx_list:s03-owner-a` or `s03-owner-a` -> `s03`."""
    return record_id_or_ref.rsplit(":", 1)[-1].split("-", 1)[0]


@pytest.fixture(scope="module")
def entities():
    return read_rows(T0 / "entities.csv", EntityRow)


@pytest.fixture(scope="module")
def relationships():
    return read_rows(T0 / "relationships.csv", RelationshipRow)


@pytest.fixture(scope="module")
def sanctions():
    return read_rows(T0 / "sanctions.csv", SanctionRow)


@pytest.fixture(scope="module")
def sanctions_t1():
    return read_rows(T1 / "sanctions.csv", SanctionRow)


@pytest.fixture(scope="module")
def repex():
    return read_rows(T0 / "repex.csv", RepexRow)


@pytest.fixture(scope="module")
def gold():
    return load_gold_scenarios(GOLD_PATH)


@pytest.fixture(scope="module")
def er_rows():
    with ER_PATH.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == ER_COLUMNS
        return list(reader)


@pytest.fixture(scope="module")
def canonical(entities):
    """Record ref -> canonical entity id.

    Records that share an LEI, or a registration authority and number, are one
    entity. Its id is the LEI id when a member has an LEI, otherwise the
    smallest member id.
    """
    parent = {row.ref: row.ref for row in entities}

    def find(ref):
        while parent[ref] != ref:
            ref = parent[ref]
        return ref

    first_with_key = {}
    for row in entities:
        keys = [("lei", row.lei)] if row.lei else []
        if row.registration_authority_id and row.registration_number:
            keys.append(("reg", row.registration_authority_id, row.registration_number))
        for key in keys:
            parent[find(row.ref)] = find(first_with_key.setdefault(key, row.ref))

    members = defaultdict(list)
    for row in entities:
        members[find(row.ref)].append(row)
    ids = {}
    for group in members.values():
        with_lei = [row.canonical_id() for row in group if row.lei]
        group_id = min(with_lei or [row.canonical_id() for row in group])
        ids.update({row.ref: group_id for row in group})
    return ids


@pytest.fixture(scope="module")
def edge_ids(relationships, canonical):
    return {
        relationship_id(
            canonical[row.subject_ref], canonical[row.object_ref], row.rel_type, row.is_ultimate
        )
        for row in relationships
    }


# --- files --------------------------------------------------------------------


@pytest.mark.parametrize("path", DATA_FILES, ids=lambda path: path.as_posix())
def test_file_is_utf8_with_lf_endings_and_no_bom(path):
    """Snapshot ids hash file bytes, so the bytes must not vary by platform."""
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{path} starts with a BOM"
    assert b"\r" not in data, f"{path} has CR line endings"
    assert data.endswith(b"\n"), f"{path} does not end with a newline"
    data.decode("utf-8")


def test_expected_files_exist():
    assert {path.name for path in T0.glob("*")} == {
        "entities.csv",
        "relationships.csv",
        "sanctions.csv",
        "repex.csv",
    }
    assert {path.name for path in T1.glob("*")} == {"sanctions.csv"}


# --- rows and references ---------------------------------------------------------


def test_record_keys_are_unique(entities, relationships, sanctions, sanctions_t1, repex):
    for name, rows in [
        ("entities", entities),
        ("relationships", relationships),
        ("sanctions", sanctions),
        ("sanctions (t1)", sanctions_t1),
        ("repex", repex),
    ]:
        duplicates = [ref for ref, count in Counter(row.ref for row in rows).items() if count > 1]
        assert not duplicates, f"{name}: duplicate keys {duplicates}"


def test_an_lei_appears_at_most_once_per_source(entities):
    seen = Counter((row.source, row.lei) for row in entities if row.lei)
    assert not [key for key, count in seen.items() if count > 1]


def test_every_reference_resolves(entities, relationships, sanctions, sanctions_t1, repex):
    known = {row.ref for row in entities}
    for row in relationships:
        assert row.subject_ref in known, f"{row.ref}: unknown subject {row.subject_ref}"
        assert row.object_ref in known, f"{row.ref}: unknown object {row.object_ref}"
    # The changed list may only designate entities that the t0 snapshot holds.
    for row in [*sanctions, *sanctions_t1, *repex]:
        assert row.entity_ref in known, f"{row.ref}: unknown entity {row.entity_ref}"


def test_rows_never_cross_scenarios(entities, relationships, sanctions, sanctions_t1, repex):
    """A shared entity would let one scenario change another's result."""
    for row in relationships:
        refs = {scenario_of(row.ref), scenario_of(row.subject_ref), scenario_of(row.object_ref)}
        assert len(refs) == 1, f"{row.ref} joins scenarios {sorted(refs)}"
    for row in [*sanctions, *sanctions_t1, *repex]:
        assert scenario_of(row.ref) == scenario_of(row.entity_ref), row.ref


def test_merged_records_belong_to_one_scenario(entities, canonical):
    scenarios = defaultdict(set)
    for row in entities:
        scenarios[canonical[row.ref]].add(scenario_of(row.ref))
    assert not {id_: found for id_, found in scenarios.items() if len(found) > 1}


def test_edges_join_the_labels_the_ontology_allows(entities, relationships):
    edge_types = load_ontology(ONTOLOGY_PATH).edge_types
    label = {row.ref: row.entity_type for row in entities}
    for row in relationships:
        spec = edge_types[row.rel_type]
        assert label[row.subject_ref] in spec.subject.labels, row.ref
        assert label[row.object_ref] in spec.object.labels, row.ref


def test_stakes_exceed_100_only_where_a_scenario_says_so(relationships, canonical, gold):
    # Several sources may report one stake; the latest record counts once.
    stakes = {}
    for row in sorted(relationships, key=lambda row: row.as_of):
        if row.rel_type == "OWNS" and row.pct is not None:
            stakes[canonical[row.subject_ref], canonical[row.object_ref]] = row.pct
    totals = defaultdict(Decimal)
    for (_, asset), pct in stakes.items():
        totals[asset] += pct

    over = {asset for asset, total in totals.items() if total > 100}
    declared = {
        gap.node
        for scenario in gold
        for gap in scenario.expected_gaps
        if gap.code == "PCT_SUM_OVER_100"
    }
    assert over == declared


# --- gold scenarios -----------------------------------------------------------------


def test_gold_ids_are_contiguous(gold):
    assert [scenario.id for scenario in gold] == [f"SCEN-{n:02d}" for n in range(1, len(gold) + 1)]


def test_gold_refers_only_to_fixture_entities_and_edges(gold, canonical, edge_ids):
    entity_ids = set(canonical.values())
    for scenario in gold:
        nodes = {*scenario.expected_blocked, *scenario.expected_possibly_blocked}
        if scenario.target_id is not None:
            nodes.add(scenario.target_id)
        for path in scenario.expected_paths:
            nodes.update(path.nodes)
            assert path.nodes[-1] == scenario.target_id, f"{scenario.id}: path misses the target"
            for edge_id in path.to_evidence_path().edge_ids:
                assert edge_id in edge_ids, f"{scenario.id}: {path.nodes} is not a fixture path"
        for gap in scenario.expected_gaps:
            if gap.node is not None:
                nodes.add(gap.node)
            else:
                assert gap.subject_id() in edge_ids, f"{scenario.id}: gap on an unknown edge"
        for pair in scenario.declared_name_pairs:
            nodes.update({pair.a, pair.b})
        assert nodes <= entity_ids, f"{scenario.id}: unknown ids {sorted(nodes - entity_ids)}"


def test_each_scenario_screens_its_own_entities(gold, entities, canonical):
    scenario_by_id = {canonical[row.ref]: scenario_of(row.ref) for row in entities}
    with_rows = {scenario_by_id[s.target_id] for s in gold if s.target_id is not None}
    for scenario in gold:
        if scenario.target_id is not None:
            assert scenario_by_id[scenario.target_id] == f"s{scenario.id[-2:]}", scenario.id
    assert {scenario_of(row.ref) for row in entities} - with_rows == {BACKGROUND}


def test_queries_name_their_target(gold, entities, canonical):
    """A query resolves to one entity, or to several where disambiguation is expected."""
    for scenario in gold:
        named = {canonical[row.ref] for row in entities if row.legal_name == scenario.query}
        if scenario.target_id is None:
            assert not named, scenario.id
        elif scenario.expect_disambiguation:
            assert scenario.target_id in named and len(named) > 1, scenario.id
        else:
            assert named == {scenario.target_id}, scenario.id


def test_twins_exist_and_have_another_outcome(gold):
    by_id = {scenario.id: scenario for scenario in gold}
    assert any(scenario.twin_of for scenario in gold)
    for scenario in gold:
        if scenario.twin_of:
            twin = by_id[scenario.twin_of]
            assert twin.expected_recommendation != scenario.expected_recommendation, scenario.id


# --- coverage ---------------------------------------------------------------------


def test_every_recommendation_reason_and_gap_is_covered(gold):
    assert {s.expected_recommendation for s in gold} == set(get_args(Recommendation))
    reasons = {reason for s in gold for reason in s.expected_reasons}
    assert reasons == set(get_args(ReasonCode)) - PIPELINE_FAILURE_REASONS
    gaps = [gap for s in gold for gap in s.expected_gaps]
    assert {gap.code for gap in gaps} == set(get_args(GapCode))
    assert {gap.decision_relevant for gap in gaps} == {True, False}
    assert {s.suite for s in gold} == {"rules", "pipeline"}


def test_every_enum_value_appears_in_the_fixtures(entities, relationships, repex):
    assert {row.status for row in entities} == set(get_args(EntityStatus))
    assert {row.entity_type for row in entities} == set(get_args(EntityType))
    assert {row.rel_type for row in relationships} == set(get_args(RelType))
    assert {row.basis for row in relationships} == set(get_args(Basis))
    assert {row.is_ultimate for row in relationships} == {True, False}
    assert {row.category for row in repex} == set(get_args(RepexCategory))


def test_sanctions_use_two_programs_and_one_inactive_record(sanctions):
    assert len({row.program for row in sanctions}) == 2
    assert [row.is_active for row in sanctions].count(False) == 1


def test_fixtures_hold_a_non_latin_alias_and_a_wide_parent(entities, relationships):
    assert any(not alias.isascii() for row in entities for alias in row.aliases)
    children = Counter(row.object_ref for row in relationships if row.rel_type == "CONSOLIDATED_BY")
    assert max(children.values()) >= 8


def test_a_designated_person_is_on_an_evidence_path(gold, entities, canonical):
    persons = {canonical[row.ref] for row in entities if row.entity_type == "Person"}
    on_paths = {node for s in gold for path in s.expected_paths for node in path.nodes}
    assert persons & on_paths


# --- names --------------------------------------------------------------------------


def test_names_are_similar_only_where_a_scenario_declares_it(gold, entities, canonical):
    """An accidental near-match would link a designation to another scenario's entity."""
    names = defaultdict(set)
    for row in entities:
        names[canonical[row.ref]].update({row.legal_name, *row.aliases})

    def score(a, b):
        return max(name_similarity(x, y) for x in names[a] for y in names[b])

    bands = {frozenset({pair.a, pair.b}): pair.band for s in gold for pair in s.declared_name_pairs}
    for pair, band in bands.items():
        value = score(*pair)
        if band == "review":
            assert REVIEW_BAND_LOW <= value < MATCH_THRESHOLD, (sorted(pair), value)
        else:
            assert value >= MATCH_THRESHOLD, (sorted(pair), value)

    for a, b in combinations(sorted(names), 2):
        if frozenset({a, b}) not in bands:
            assert score(a, b) < REVIEW_BAND_LOW, f"{a} and {b} score {score(a, b)}"


# --- the changed sanctions list -------------------------------------------------------


def test_changed_list_adds_removes_and_changes_one_designation(sanctions, sanctions_t1, gold):
    before = {row.ref: row for row in sanctions}
    after = {row.ref: row for row in sanctions_t1}
    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {
        ref
        for ref in set(before) & set(after)
        if any(getattr(before[ref], f) != getattr(after[ref], f) for f in DESIGNATION_FIELDS)
    }
    assert (len(added), len(removed), len(changed)) == (1, 1, 1)
    # Rows that are not part of the change are byte-for-byte the same.
    untouched = set(before) & set(after) - changed
    assert all(before[ref] == after[ref] for ref in untouched)

    touched = {scenario_of(ref) for ref in added | removed | changed}
    expecting_change = {f"s{s.id[-2:]}" for s in gold if s.expected_recommendation_t1}
    assert touched == expecting_change
    for scenario in gold:
        if scenario.expected_recommendation_t1:
            assert scenario.expected_recommendation_t1 != scenario.expected_recommendation


# --- labelled entity-resolution set -----------------------------------------------------


def test_er_set_is_large_enough_and_covers_every_variant(er_rows):
    assert len(er_rows) >= 40
    assert {row["variant_type"] for row in er_rows} == ER_VARIANT_TYPES
    assert len({(row["query_name"], row["lei"], row["jurisdiction"]) for row in er_rows}) == len(
        er_rows
    )


def test_er_set_points_at_fixture_entities(er_rows, entities, canonical):
    entity_ids = set(canonical.values())
    leis = {row.lei for row in entities if row.lei}
    for row in er_rows:
        if row["variant_type"] == "negative":
            assert row["expected_entity_id"] == "", row["query_name"]
        else:
            assert row["expected_entity_id"] in entity_ids, row["query_name"]
        if row["lei"]:
            assert row["lei"] in leis, row["query_name"]
            assert row["expected_entity_id"] == f"lei:{row['lei']}"
