"""OpenSanctions: the loader's declarations and the mappers, from the sample file to Silver."""

from datetime import date
from decimal import Decimal

import duckdb
import pytest
from real_format import SAMPLES

from ubo_sentinel.pipeline.bronze import resolve_set
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders import LOADERS
from ubo_sentinel.pipeline.loaders.opensanctions_loader import OS_DATASETS
from ubo_sentinel.pipeline.loaders.real_base import ingest_real
from ubo_sentinel.pipeline.loaders.real_loader import REAL_DATASETS, ingest_opensanctions
from ubo_sentinel.pipeline.mappers import MAPPERS, OS_OTHER_LINK_SCHEMAS, os_percentage
from ubo_sentinel.pipeline.transformer import build_silver, register_functions

OS = "opensanctions:NK-fx"


@pytest.fixture
def built(con, ontology):
    """OpenSanctions' datasets alone, as a set of their own: what its mappers make."""
    snapshot_set = ingest_real(con, SAMPLES, OS_DATASETS, [d.name for d in OS_DATASETS])
    summary = build_silver(con, snapshot_set.snapshot_set, ontology)
    return snapshot_set.snapshot_set.snapshot_set_id, summary.counts


def rows(con, sql, set_id, *more):
    return con.execute(sql, [set_id, *more]).fetchall()


def test_every_opensanctions_dataset_has_a_mapper_and_a_loader():
    assert {d.name for d in OS_DATASETS} <= set(MAPPERS)
    assert {d.name for d in OS_DATASETS} <= set(REAL_DATASETS)
    assert LOADERS["opensanctions"] is ingest_opensanctions


def test_list_records_become_entities(con, built):
    set_id, counts = built
    found = {
        row[0]: row[1:]
        for row in rows(
            con,
            "SELECT id, lei, legal_name, jurisdiction, status, lei_status, entity_type, aliases"
            " FROM silver_entities WHERE snapshot_set_id = ?",
            set_id,
        )
    }
    # The LEI it states names the entity; the caption is its name; status is unknown.
    orvasko = found["lei:FXRORV00000000000000"]
    assert orvasko[:6] == (
        "FXRORV00000000000000",
        "Orvasko Bulk Logistics LLC",
        "US",
        "UNKNOWN",
        None,
        "LegalEntity",
    )
    assert orvasko[6] == ["Orvasko Logistics", "Орваско Балк Логистикс"]
    # Its own jurisdiction before its country; a person is a Person.
    assert found[f"{OS}Sarvanta"][2] == "AE"
    assert found[f"{OS}Lomanov"][5] == "Person"
    # A weak alias is too loose to link on.
    assert "Sarvanta" not in found[f"{OS}Sarvanta"][6]
    # No country: no jurisdiction.
    assert found[f"{OS}Mirovane"][2] is None
    # Every schema that is an owner or owned in this graph.
    assert {found[f"{OS}{n}"][5] for n in ("Mirovane", "Kestovar", "PortAuthority")} == {
        "LegalEntity"
    }
    # A vessel is in Bronze, not in the graph.
    assert f"{OS}Vessel" not in found
    assert counts["filtered"]["os_entities"] == {"UNSUPPORTED_SCHEMA": 1}


def test_a_person_s_birth_dates_and_nationalities_reach_silver(con, built):
    """Step 19: read as corroborators of a name match."""
    set_id, _ = built
    found = dict(
        (row[0], row[1:])
        for row in rows(
            con,
            "SELECT id, birth_dates, nationalities FROM silver_entities WHERE snapshot_set_id = ?",
            set_id,
        )
    )
    assert found[f"{OS}Lomanov"] == (["1966-04-12"], ["AE"])
    assert found[f"{OS}Sarvanta"] == ([], [])


@pytest.mark.parametrize(
    ("properties", "dates", "countries"),
    [
        ('{"birthDate":["1970"],"nationality":["ru"]}', ["1970"], ["RU"]),
        # Several dates, partial or full, and the citizenship beside the nationality.
        (
            '{"birthDate":["1970-03","1969-12-31"],"nationality":["ru"],"citizenship":["cy","ru"]}',
            ["1969-12-31", "1970-03"],
            ["CY", "RU"],
        ),
        # What is no ISO date or no country is left out.
        ('{"birthDate":["circa 1970"],"nationality":["suhh"]}', [], []),
        ("{}", [], []),
    ],
)
def test_birth_dates_and_nationalities_are_read_as_iso(con, properties, dates, countries):
    register_functions(con)
    columns = MAPPERS["os_entities"].columns
    found = con.execute(
        f"SELECT {columns[12]}, {columns[13]} FROM (SELECT CAST(? AS JSON) AS _raw)",
        ['{"schema":"Person","properties":' + properties + "}"],
    ).fetchone()
    assert found == (dates, countries)


@pytest.mark.parametrize(
    ("properties", "read"),
    [
        ('{"country":["ru","cy"]}', None),
        ('{"country":["ru"]}', "ru"),
        ('{"jurisdiction":["cy"],"country":["ru","cy"]}', "cy"),
    ],
)
def test_a_record_with_several_countries_has_no_jurisdiction(properties, read):
    """Which of two countries is the jurisdiction is not for the mapper to guess."""
    written = MAPPERS["os_entities"].columns[10]
    found = duckdb.connect().execute(
        f"SELECT {written} FROM (SELECT CAST(? AS JSON) AS _raw)",
        ['{"schema":"Company","properties":' + properties + "}"],
    )
    assert found.fetchone() == (read,)


def test_designations(con, built):
    set_id, counts = built
    found = {
        row[0]: row[1:]
        for row in rows(
            con,
            "SELECT source_record_id, entity_id, program, list_date, list_source, is_active"
            " FROM silver_sanctions WHERE snapshot_set_id = ? AND match_type = 'DIRECT'",
            set_id,
        )
    }
    assert found["os-fx-orv-sdn"] == (
        "lei:FXRORV00000000000000",
        "US-FXR-EO1",
        date(2024, 3, 11),
        "us_ofac_sdn",
        True,
    )
    # The list is the designation record's own dataset.
    assert found["os-fx-north-eu"][3] == "eu_fsf"
    # Ended before the publish date; and one that ends after it.
    assert found["os-fx-teverin-uk"][4] is False
    assert found["os-fx-kestovar-uk"][4] is True
    # A listing date, else a start date.
    assert found["os-fx-miro-sdn"][2] == date(2025, 5, 2)
    # The designated vessel is not in the graph.
    assert counts["rejects"]["UNRESOLVED_REF"] >= 1
    assert counts["designations"] == 9


def test_links(con, built):
    set_id, counts = built
    edges = {
        (subject, obj): (rel_type, pct, basis)
        for subject, obj, rel_type, pct, basis in rows(
            con,
            "SELECT subject_id, object_id, rel_type, pct, basis FROM silver_relationships"
            " WHERE snapshot_set_id = ?",
            set_id,
        )
    }
    assert edges[(f"{OS}Northgate", "lei:FXRHALV0000000000000")] == (
        "OWNS",
        Decimal("60"),
        "shareholding",
    )
    # "25-50" is a range, never its upper bound (Step 16).
    assert edges[(f"{OS}Sarvanta", f"{OS}SarvRefining")][1] is None
    assert rows(
        con,
        "SELECT pct_low, pct_high, pct_bounds, interest FROM silver_relationships"
        " WHERE snapshot_set_id = ? AND subject_id = ? AND object_id = ?",
        set_id,
        f"{OS}Sarvanta",
        f"{OS}SarvRefining",
    ) == [(Decimal("25"), Decimal("50"), "[]", "equity")]
    assert edges[(f"{OS}Mirovane", f"{OS}MiroTrading")][1] is None
    assert edges[(f"{OS}Sarvanta", f"{OS}SarvLogistics")][1] is None
    assert edges[(f"{OS}Lomanov", f"{OS}Sarvanta")] == ("CONTROLS", None, "directorship")
    # The unknown link; and an associate record that names one end only (Step 20).
    assert counts["filtered"]["os_relationships"] == {"NO_ENDS": 1, "UNSUPPORTED_REL_TYPE": 1}
    # "majority" is a share in words; an Ownership that states none is not counted.
    assert counts["unreadable"]["pct"] == 1


def test_family_and_associates_are_relations_in_id_order(con, built):
    """Step 20: `Family` and `Associate` are RELATED_TO edges, no longer filtered. They
    have no direction: the ends are in id order, whichever side the record names first."""
    set_id, counts = built
    assert "Family" not in OS_OTHER_LINK_SCHEMAS and "Associate" not in OS_OTHER_LINK_SCHEMAS
    found = rows(
        con,
        "SELECT subject_id, object_id, basis, pct_high, interest, is_ultimate, source_record_id"
        " FROM silver_relationships WHERE snapshot_set_id = ? AND rel_type = 'RELATED_TO'",
        set_id,
    )
    # The record names the sister as `person`; her id sorts after his.
    assert found == [
        (f"{OS}Lomanov", f"{OS}Lomanova", "family", None, "equity", False, "NK-fxFam1")
    ]
    # An associate record that names one end only has no ends.
    assert counts["filtered"]["os_relationships"]["NO_ENDS"] == 1


def test_a_relation_is_kept_apart_from_the_graph_edges(con, built, ontology):
    """Gold holds it in `graph_related`, so no traversal walks it, and no degree counts it."""
    set_id, _ = built
    snapshot_set = resolve_set(con, set_id)
    build_gold(con, snapshot_set)
    assert rows(
        con, "SELECT count(*) FROM graph_edges WHERE snapshot_set_id = ? AND rel_type = ?",
        set_id, "RELATED_TO",
    ) == [(0,)]
    assert rows(
        con, "SELECT upper_id, lower_id FROM graph_related WHERE snapshot_set_id = ?", set_id
    ) == [(f"{OS}Lomanov", f"{OS}Lomanova")]
    assert rows(
        con,
        "SELECT up_degree, down_degree FROM graph_nodes WHERE snapshot_set_id = ? AND id = ?",
        set_id,
        f"{OS}Lomanova",
    ) == [(0, 0)]


@pytest.mark.parametrize(
    ("written", "read"),
    [
        # (pct, pct_low, pct_high, pct_bounds)
        ("50", ("50", "50", "50", "[]")),
        ("50%", ("50", "50", "50", "[]")),
        ("50.0", ("50", "50", "50", "[]")),
        ("12,5 %", ("12.5", "12.5", "12.5", "[]")),
        ("100", ("100", "100", "100", "[]")),
        ("25-50", (None, "25", "50", "[]")),
        ("25 – 50%", (None, "25", "50", "[]")),
        ("<10%", (None, "0", "10", "()")),
        ("≤10", (None, "0", "10", "(]")),
        (">50", (None, "50", "100", "(]")),
        ("≥50", (None, "50", "100", "[]")),
        ("≥100", ("100", "100", "100", "[]")),
        ("majority", None),
        ("0", None),
        ("150", None),
        ("50-", None),
        ("60-40", None),
        (">100", None),
        ("<0", None),
        ("", None),
        (None, None),
    ],
)
def test_percentages_are_read_as_points_or_ranges(written, read):
    """A range is kept as one (Step 16): never rounded to a bound."""
    value = (
        duckdb.connect()
        .execute(
            f"SELECT {', '.join(os_percentage('v'))} FROM (SELECT CAST(? AS VARCHAR) AS v)",
            [written],
        )
        .fetchone()
    )
    if read is None:
        assert value == (None, None, None, None)
    else:
        *figures, bounds = read
        expected = tuple(None if f is None else Decimal(f) for f in figures)
        assert value == (*expected, bounds)
