"""OpenSanctions: the loader's declarations and the mappers, from the sample file to Silver."""

from datetime import date
from decimal import Decimal

import duckdb
import pytest
from real_format import SAMPLES

from ubo_sentinel.pipeline.loaders import LOADERS
from ubo_sentinel.pipeline.loaders.opensanctions_loader import OS_DATASETS
from ubo_sentinel.pipeline.loaders.real_base import ingest_real
from ubo_sentinel.pipeline.loaders.real_loader import REAL_DATASETS, ingest_opensanctions
from ubo_sentinel.pipeline.mappers import MAPPERS, os_percentage
from ubo_sentinel.pipeline.transformer import build_silver

OS = "opensanctions:NK-fx"


@pytest.fixture
def built(con, ontology):
    """OpenSanctions' datasets alone, as a set of their own: what its mappers make."""
    snapshot_set = ingest_real(con, SAMPLES, OS_DATASETS, [d.name for d in OS_DATASETS])
    summary = build_silver(con, snapshot_set.snapshot_set, ontology)
    return snapshot_set.snapshot_set.snapshot_set_id, summary.counts


def rows(con, sql, set_id):
    return con.execute(sql, [set_id]).fetchall()


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
    assert edges[(f"{OS}Sarvanta", f"{OS}SarvRefining")][1] == Decimal("50")
    assert edges[(f"{OS}Mirovane", f"{OS}MiroTrading")][1] is None
    assert edges[(f"{OS}Sarvanta", f"{OS}SarvLogistics")][1] is None
    assert edges[(f"{OS}Lomanov", f"{OS}Sarvanta")] == ("CONTROLS", None, "directorship")
    assert counts["filtered"]["os_relationships"] == {"UNSUPPORTED_REL_TYPE": 1}
    # "majority" is a share in words; an Ownership that states none is not counted.
    assert counts["unreadable"]["pct"] == 1


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ("50", "50.0000"),
        ("50%", "50.0000"),
        ("50.0", "50.0000"),
        ("12,5 %", "12.5000"),
        ("25-50", "50.0000"),
        ("25 – 50%", "50.0000"),
        (">50", "50.0000"),
        ("<10%", "10.0000"),
        ("100", "100.0000"),
        ("majority", None),
        ("0", None),
        ("150", None),
        ("50-", None),
        ("", None),
        (None, None),
    ],
)
def test_percentages_are_read_on_the_cautious_side(written, read):
    value = (
        duckdb.connect()
        .execute(f"SELECT {os_percentage('v')} FROM (SELECT CAST(? AS VARCHAR) AS v)", [written])
        .fetchone()[0]
    )
    assert value == (None if read is None else Decimal(read))
