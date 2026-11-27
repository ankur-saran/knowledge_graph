"""Step 17: Companies House's company file and PSC snapshot, read into the graph.

The sample refresh directory (`tests/data/real_format/companies_house/`, written by
`tests/real_format.py`) holds every kind of record the two files publish.
"""

import hashlib
import json
import shutil
from datetime import datetime
from decimal import Decimal

import pytest
from publishers import Publishers, split_parts, zipped
from real_format import SAMPLES, build_samples, lei, restamp

from ubo_sentinel.download import DownloadError, download
from ubo_sentinel.download.publishers import (
    COMPANIES_HOUSE_BASE,
    PublisherError,
    companies_house_files,
)
from ubo_sentinel.download.refresh import PARTIAL
from ubo_sentinel.models.manifest import load_manifest
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders.companies_house_loader import CH_DATASETS
from ubo_sentinel.pipeline.loaders.real_loader import REAL_OPTIONAL_DATASETS, ingest_all
from ubo_sentinel.pipeline.mappers import (
    CH_NATURE_EDGES,
    CH_NATURES,
    MAPPERS,
    MORE_MAPPERS,
    NatureEdge,
    ch_nature,
)
from ubo_sentinel.pipeline.silver import read_rows
from ubo_sentinel.pipeline.sources import SOURCE_CONFIDENCE, SOURCE_RANK, SOURCE_TIE
from ubo_sentinel.pipeline.transformer import build_silver, register_functions

CH = "companies_house:"

# The `description` keys of companieshouse/api-enumerations `psc_descriptions.yml`,
# read 2026-11-23, in its order.
PUBLISHED_NATURES = """
ownership-of-shares-25-to-50-percent
ownership-of-shares-50-to-75-percent
ownership-of-shares-75-to-100-percent
ownership-of-shares-25-to-50-percent-as-trust
ownership-of-shares-50-to-75-percent-as-trust
ownership-of-shares-75-to-100-percent-as-trust
ownership-of-shares-25-to-50-percent-as-firm
ownership-of-shares-50-to-75-percent-as-firm
ownership-of-shares-75-to-100-percent-as-firm
ownership-of-shares-more-than-25-percent-registered-overseas-entity
ownership-of-shares-more-than-25-percent-as-trust-registered-overseas-entity
ownership-of-shares-more-than-25-percent-as-firm-registered-overseas-entity
ownership-of-shares-more-than-25-percent-as-control-over-trust-registered-overseas-entity
ownership-of-shares-more-than-25-percent-as-control-over-firm-registered-overseas-entity
voting-rights-25-to-50-percent
voting-rights-50-to-75-percent
voting-rights-75-to-100-percent
voting-rights-25-to-50-percent-as-trust
voting-rights-50-to-75-percent-as-trust
voting-rights-75-to-100-percent-as-trust
voting-rights-25-to-50-percent-as-firm
voting-rights-50-to-75-percent-as-firm
voting-rights-75-to-100-percent-as-firm
voting-rights-more-than-25-percent-registered-overseas-entity
voting-rights-more-than-25-percent-as-trust-registered-overseas-entity
voting-rights-more-than-25-percent-as-firm-registered-overseas-entity
voting-rights-more-than-25-percent-as-control-over-trust-registered-overseas-entity
voting-rights-more-than-25-percent-as-control-over-firm-registered-overseas-entity
right-to-appoint-and-remove-directors
right-to-appoint-and-remove-directors-as-trust
right-to-appoint-and-remove-directors-as-firm
right-to-appoint-and-remove-directors-as-control-over-trust-registered-overseas-entity
right-to-appoint-and-remove-directors-as-control-over-firm-registered-overseas-entity
significant-influence-or-control
significant-influence-or-control-as-trust
significant-influence-or-control-as-firm
right-to-share-surplus-assets-25-to-50-percent-limited-liability-partnership
right-to-share-surplus-assets-50-to-75-percent-limited-liability-partnership
right-to-share-surplus-assets-75-to-100-percent-limited-liability-partnership
right-to-share-surplus-assets-25-to-50-percent-as-trust-limited-liability-partnership
right-to-share-surplus-assets-50-to-75-percent-as-trust-limited-liability-partnership
right-to-share-surplus-assets-75-to-100-percent-as-trust-limited-liability-partnership
right-to-share-surplus-assets-25-to-50-percent-as-firm-limited-liability-partnership
right-to-share-surplus-assets-50-to-75-percent-as-firm-limited-liability-partnership
right-to-share-surplus-assets-75-to-100-percent-as-firm-limited-liability-partnership
voting-rights-25-to-50-percent-limited-liability-partnership
voting-rights-50-to-75-percent-limited-liability-partnership
voting-rights-75-to-100-percent-limited-liability-partnership
voting-rights-25-to-50-percent-as-trust-limited-liability-partnership
voting-rights-50-to-75-percent-as-trust-limited-liability-partnership
voting-rights-75-to-100-percent-as-trust-limited-liability-partnership
voting-rights-25-to-50-percent-as-firm-limited-liability-partnership
voting-rights-50-to-75-percent-as-firm-limited-liability-partnership
voting-rights-75-to-100-percent-as-firm-limited-liability-partnership
right-to-appoint-and-remove-members-limited-liability-partnership
right-to-appoint-and-remove-members-as-trust-limited-liability-partnership
right-to-appoint-and-remove-members-as-firm-limited-liability-partnership
significant-influence-or-control-limited-liability-partnership
significant-influence-or-control-as-trust-limited-liability-partnership
significant-influence-or-control-as-firm-limited-liability-partnership
significant-influence-or-control-registered-overseas-entity
significant-influence-or-control-as-trust-registered-overseas-entity
significant-influence-or-control-as-firm-registered-overseas-entity
significant-influence-or-control-as-control-over-trust-registered-overseas-entity
significant-influence-or-control-as-control-over-firm-registered-overseas-entity
part-right-to-share-surplus-assets-25-to-50-percent
part-right-to-share-surplus-assets-50-to-75-percent
part-right-to-share-surplus-assets-75-to-100-percent
part-right-to-share-surplus-assets-25-to-50-percent-as-trust
part-right-to-share-surplus-assets-50-to-75-percent-as-trust
part-right-to-share-surplus-assets-75-to-100-percent-as-trust
part-right-to-share-surplus-assets-25-to-50-percent-as-firm
part-right-to-share-surplus-assets-50-to-75-percent-as-firm
part-right-to-share-surplus-assets-75-to-100-percent-as-firm
right-to-appoint-and-remove-person
right-to-appoint-and-remove-person-as-firm
right-to-appoint-and-remove-person-as-trust
right-to-appoint-and-remove-directors-registered-overseas-entity
right-to-appoint-and-remove-directors-as-trust-registered-overseas-entity
right-to-appoint-and-remove-directors-as-firm-registered-overseas-entity
registered-owner-as-nominee-person-england-wales-registered-overseas-entity
registered-owner-as-nominee-person-scotland-registered-overseas-entity
registered-owner-as-nominee-person-northern-ireland-registered-overseas-entity
registered-owner-as-nominee-another-entity-england-wales-registered-overseas-entity
registered-owner-as-nominee-another-entity-scotland-registered-overseas-entity
registered-owner-as-nominee-another-entity-northern-ireland-registered-overseas-entity
""".split()


# --- the natures of control -----------------------------------------------------------


def owns(interest, low, high, bounds, trust_or_firm=False):
    return NatureEdge("OWNS", "shareholding", interest, low, high, bounds, trust_or_firm)


def test_every_published_nature_of_control_is_known():
    assert len(PUBLISHED_NATURES) == 86
    assert sorted(CH_NATURES) == sorted(PUBLISHED_NATURES)


@pytest.mark.parametrize(
    ("code", "edge"),
    [
        ("ownership-of-shares-25-to-50-percent", owns("equity", "25", "50", "(]")),
        ("ownership-of-shares-50-to-75-percent", owns("equity", "50", "75", "()")),
        ("ownership-of-shares-75-to-100-percent", owns("equity", "75", "100", "[]")),
        ("voting-rights-25-to-50-percent", owns("voting", "25", "50", "(]")),
        ("voting-rights-50-to-75-percent", owns("voting", "50", "75", "()")),
        ("voting-rights-75-to-100-percent", owns("voting", "75", "100", "[]")),
        (
            "right-to-share-surplus-assets-50-to-75-percent-limited-liability-partnership",
            owns("equity", "50", "75", "()"),
        ),
        (
            "part-right-to-share-surplus-assets-75-to-100-percent",
            owns("equity", "75", "100", "[]"),
        ),
        (
            "ownership-of-shares-more-than-25-percent-registered-overseas-entity",
            owns("equity", "25", "100", "(]"),
        ),
        ("right-to-appoint-and-remove-directors", NatureEdge("CONTROLS", "board_appointment")),
        (
            "right-to-appoint-and-remove-members-limited-liability-partnership",
            NatureEdge("CONTROLS", "board_appointment"),
        ),
        ("right-to-appoint-and-remove-person", NatureEdge("CONTROLS", "board_appointment")),
        ("significant-influence-or-control", NatureEdge("CONTROLS", "significant_influence")),
        # A trustee's or a firm member's holding maps as its plain form, marked (Step 21).
        (
            "voting-rights-25-to-50-percent-as-trust",
            owns("voting", "25", "50", "(]", trust_or_firm=True),
        ),
        (
            "significant-influence-or-control-as-control-over-firm-registered-overseas-entity",
            NatureEdge("CONTROLS", "significant_influence", trust_or_firm=True),
        ),
    ],
)  # fmt: skip
def test_each_nature_of_control_is_the_edge_the_register_defines(code, edge):
    assert ch_nature(code) == edge


def test_only_a_nominee_registered_owner_states_no_edge():
    unmapped = [code for code in CH_NATURES if code not in CH_NATURE_EDGES]
    assert unmapped and all(code.startswith("registered-owner-as-nominee-") for code in unmapped)
    # Every band is a range: the register never states a figure.
    for edge in CH_NATURE_EDGES.values():
        assert (edge.low is None) == (edge.rel_type == "CONTROLS")
        if edge.rel_type == "OWNS":
            assert edge.low != edge.high


# --- sources and datasets --------------------------------------------------------------


def test_companies_house_is_a_registry_after_gleif_and_its_datasets_are_optional():
    assert SOURCE_RANK["companies_house"] == SOURCE_RANK["gleif"]
    assert SOURCE_CONFIDENCE["companies_house"] == 1.0
    assert SOURCE_TIE.get("gleif", 0) < SOURCE_TIE["companies_house"]
    assert {d.name for d in CH_DATASETS} == {"ch_companies", "ch_psc"}
    assert {d.name for d in CH_DATASETS} <= set(REAL_OPTIONAL_DATASETS)
    assert {"ch_companies", "ch_psc"} <= set(MAPPERS)
    assert len(MORE_MAPPERS["ch_psc"]) == 2


@pytest.fixture
def built(con, ontology):
    snapshot_set = ingest_all(con, SAMPLES).snapshot_set
    build_silver(con, snapshot_set, ontology)
    build_gold(con, snapshot_set)
    return snapshot_set.snapshot_set_id


def rows(con, table, set_id, **where):
    return [r for r in read_rows(con, table, set_id) if all(r[k] == v for k, v in where.items())]


def test_a_uk_company_is_one_entity_with_gleif_s_record_of_it(con, built):
    (hold,) = rows(con, "silver_entities", built, id=f"lei:{lei('HOLD')}")
    # Merged on registration authority and number; GLEIF's record describes it.
    assert (hold["source"], hold["registration_authority_id"], hold["registration_number"]) == (
        "gleif",
        "RA000585",
        "FXR-HOLD",
    )
    joined = rows(con, "silver_entity_xref", built, entity_id=f"lei:{lei('HOLD')}")
    assert {(r["source"], r["joined_by"]) for r in joined} == {
        ("gleif", "SELF"),
        ("companies_house", "REGISTRATION"),
    }
    names = {r["name"] for r in rows(con, "silver_entity_names", built, entity_id=hold["id"])}
    assert "BRACKWATER HOLDINGS PLC" in names


def test_gleif_describes_a_merged_uk_company_even_when_companies_house_is_newer(
    con, ontology, tmp_path
):
    refresh = shutil.copytree(SAMPLES, tmp_path / "newer")
    manifest = json.loads((refresh / "manifest.json").read_text(encoding="utf-8"))
    for key in ("ch_companies", "ch_psc"):
        manifest["files"][key]["published_at"] = "2026-11-01T00:00:00"
    (refresh / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    restamp(refresh)
    snapshot_set = ingest_all(con, refresh).snapshot_set
    build_silver(con, snapshot_set, ontology)
    (hold,) = rows(con, "silver_entities", snapshot_set.snapshot_set_id, id=f"lei:{lei('HOLD')}")
    assert hold["source"] == "gleif"


def test_companies_are_registry_records_with_status_and_authority(con, built):
    found = {r["id"]: r for r in rows(con, "silver_entities", built, source="companies_house")}
    companies = {k: r for k, r in found.items() if "/company/" not in k}
    status = {k.removeprefix(CH): r["status"] for k, r in companies.items()}
    assert status == {
        "01234567": "INACTIVE",  # Dissolved
        "FX100002": "ACTIVE",
        "FX100003": "ACTIVE",
        "FX100004": "UNKNOWN",  # Liquidation
        "FX100005": "ACTIVE",
        "NI600001": "UNKNOWN",  # Active - Proposal to Strike off
        "OC400001": "ACTIVE",
        "OE000001": "UNKNOWN",  # Registered (an overseas entity)
        "SL000001": "ACTIVE",
    }
    assert found[f"{CH}NI600001"]["registration_authority_id"] == "RA000586"
    assert found[f"{CH}FX100002"]["registration_authority_id"] == "RA000585"
    assert {r["jurisdiction"] for r in companies.values()} == {"GB"}


def test_psc_holders_are_people_or_companies_and_a_uk_company_is_its_own_record(con, built):
    people = {
        r["legal_name"]: r["entity_type"]
        for r in rows(con, "silver_entities", built, source="companies_house")
        if "/persons-with-significant-control/" in r["id"]
    }
    # Ceased (Anna Quarry) and nameless records are no holder; the UK companies
    # that hold others (Pellmoor, Hollinworth) are their own company records.
    assert people == {
        "Mr Viktor Lomanov": "Person",
        "Mr Daniel Okafor": "Person",
        "Mrs Isla Brae": "Person",
        "Sarvanta Metals Trading L.L.C.": "LegalEntity",
        "Vantelli Holdings S.A.": "LegalEntity",
        "Eastmoor Borough Council": "LegalEntity",
    }
    edges = {
        (r["subject_id"], r["object_id"], r["interest"]): r
        for r in rows(con, "silver_relationships", built, source="companies_house")
    }
    # A registration number written without its leading zero is the company's number.
    stake = edges[(f"{CH}01234567", f"{CH}FX100005", "equity")]
    assert (stake["pct_low"], stake["pct_high"], stake["pct_bounds"]) == (
        Decimal("75"),
        Decimal("100"),
        "[]",
    )
    assert (f"{CH}FX100002", f"{CH}OC400001", "voting") in edges


def test_each_band_is_a_range_of_its_measure_and_control_is_by_basis(con, built):
    pellmoor = f"{CH}/company/FX100002/persons-with-significant-control/corporate-entity/fxPsc0201"
    found = {
        (r["interest"], r["rel_type"], r["basis"]): tuple(
            r[name] for name in ("pct", "pct_low", "pct_high", "pct_bounds")
        )
        for r in rows(con, "silver_relationships", built, subject_id=pellmoor)
    }
    assert found == {
        ("equity", "OWNS", "shareholding"): (None, Decimal("50"), Decimal("75"), "()"),
        ("voting", "OWNS", "shareholding"): (None, Decimal("50"), Decimal("75"), "()"),
    }
    controls = {
        r["basis"]
        for r in rows(con, "silver_relationships", built, source="companies_house")
        if r["rel_type"] == "CONTROLS"
    }
    assert controls == {"board_appointment", "significant_influence"}


def test_statements_are_reporting_exceptions_and_the_rest_are_counted(con, built):
    found = {
        (r["entity_id"].removeprefix(CH), r["reason"])
        for r in rows(con, "silver_reporting_exceptions", built, source="companies_house")
    }
    assert found == {
        ("FX100004", "psc-exists-but-not-identified"),
        ("FX100005", "super-secure-person-with-significant-control"),
        ("NI600001", "psc-details-not-confirmed"),
        ("NI600001", "steps-to-find-psc-not-yet-completed"),
    }
    assert {
        r["category"]
        for r in rows(con, "silver_reporting_exceptions", built, source="companies_house")
    } == {"PSC_STATEMENT"}
    (counts,) = con.execute(
        "SELECT counts FROM silver_builds WHERE snapshot_set_id = ?", [built]
    ).fetchone()
    counts = json.loads(counts)
    # A ceased PSC and a ceased statement; a record with no company; the nominee
    # nature; "no registrable person" (recorded, no gap).
    assert counts["filtered"]["ch_psc"] == {
        "CEASED": 2,
        "NO_ENDS": 1,
        "NO_REGISTRABLE_PERSON": 1,
        "UNSUPPORTED_NATURE": 1,
    }
    assert counts["noted"]["ch_psc"] == {"TRUST_OR_FIRM": 3}


def test_a_psc_keeps_its_birth_month_year_and_nationality_in_bronze(con, built):
    raw = json.loads(
        con.execute(
            "SELECT _raw FROM bronze_relationships WHERE _source = 'companies_house'"
            " AND _source_record_id LIKE '%fxPsc0301'"
        ).fetchone()[0]
    )
    assert raw["data"]["date_of_birth"] == {"month": 4, "year": 1966}
    assert raw["data"]["nationality"] == "Emirati"
    # The day is never published, and nothing the record does not state is invented.
    assert "address" not in raw["data"]


def test_a_psc_person_s_birth_month_year_and_nationality_reach_silver(con, built):
    """Step 19: read as corroborators of a name match; the demonym is its country."""
    people = {
        r["legal_name"]: (r["birth_dates"], r["nationalities"])
        for r in rows(con, "silver_entities", built, source="companies_house")
        if "/persons-with-significant-control/" in r["id"]
    }
    assert people["Mr Viktor Lomanov"] == (["1966-04"], ["AE"])
    # A company states neither.
    assert people["Vantelli Holdings S.A."] == ([], [])


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ('"British"', ["GB"]),
        ('"British, Irish"', ["GB", "IE"]),
        ('"Scottish"', ["GB"]),
        ('"Martian"', []),
        ("null", []),
    ],
)
def test_a_psc_nationality_is_read_as_the_country_it_names(con, written, read):
    register_functions(con)
    mapper = MORE_MAPPERS["ch_psc"][0]
    found = con.execute(
        f"SELECT {mapper.columns[13]} FROM (SELECT CAST(? AS JSON) AS _raw)",
        ['{"data":{"nationality":' + written + "}}"],
    ).fetchone()
    assert found == (read,)


# --- download --------------------------------------------------------------------------


def test_the_newest_complete_set_of_parts_is_planned():
    served = Publishers()
    with served.client() as client:
        planned = {p.key: p for p in companies_house_files(client)}
    psc = planned["ch_psc"]
    assert psc.parts == tuple(served.ch_parts["ch_psc"])
    assert (psc.url, psc.licence, psc.published_at) == (
        psc.parts[0],
        "OGL-3.0",
        datetime(2026, 10, 1),
    )
    assert planned["ch_companies"].repeats_header and not psc.repeats_header


def test_a_page_that_lists_only_some_parts_is_refused():
    served = Publishers()
    served.ch_parts["ch_psc"] = served.ch_parts["ch_psc"][1:]
    with served.client() as client, pytest.raises(PublisherError, match="not all listed"):
        companies_house_files(client)


def test_parts_are_joined_into_the_published_file_and_recorded(tmp_path):
    served = Publishers()
    with served.client() as client:
        result = download(tmp_path / "downloads", client=client)
    samples = build_samples()
    for path in ("companies_house/companies.csv", "companies_house/psc.txt"):
        assert (result.refresh_dir / path).read_bytes() == samples[path]
    entry = load_manifest(result.refresh_dir).files["ch_companies"]
    assert [p.url for p in entry.parts] == served.ch_parts["ch_companies"]
    assert [p.archive_sha256 for p in entry.parts] == [
        hashlib.sha256(served.blobs[url]).hexdigest() for url in served.ch_parts["ch_companies"]
    ]
    assert entry.licence == "OGL-3.0" and entry.archive_sha256 is None
    assert list((tmp_path / "downloads" / PARTIAL).iterdir()) == []


def test_a_part_whose_header_differs_is_refused(tmp_path):
    served = Publishers()
    url = served.ch_parts["ch_companies"][1]
    parts = split_parts(build_samples()["companies_house/companies.csv"], 2, True)
    served.blobs[url] = zipped("part2.csv", b"Other,Header\n" + parts[1].split(b"\n", 1)[1])
    with served.client() as client, pytest.raises(DownloadError, match="header row"):
        download(tmp_path / "downloads", client=client)


def test_the_download_pages_are_the_companies_house_site():
    assert COMPANIES_HOUSE_BASE == "https://download.companieshouse.gov.uk/"
