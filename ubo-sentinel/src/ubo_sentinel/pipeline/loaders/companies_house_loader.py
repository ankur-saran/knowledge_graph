"""Companies House (Step 17): the free company data product and the PSC snapshot.

Two files of one refresh, two datasets, source `companies_house`, both optional
members of a real set:

- `ch_companies`: every company on the register (CSV, as published: some headers
  begin with a space). The record is keyed by company number.
- `ch_psc`: the people with significant control and the companies' statements
  (one JSON object per line: `company_number` and `data`). A record is keyed by
  its `links.self`, which names the company and the PSC.

Each dataset declares the fields its mappers read (`pipeline/mappers.py`). A
PSC's month and year of birth and nationality are kept in `_raw` for later
matching (Step 19); no mapper reads them.
"""

from ubo_sentinel.pipeline.loaders.real_base import Column, Format, Kind, RealDataset
from ubo_sentinel.pipeline.mappers import CH_KINDS, CH_PREVIOUS_NAMES, CH_SOURCE

COMPANIES_FILE = "ch_companies"
PSC_FILE = "ch_psc"

CH_COMPANIES = RealDataset(
    name="ch_companies",
    table="bronze_entities",
    source=CH_SOURCE,
    file=COMPANIES_FILE,
    format=Format.CSV,
    columns=tuple(
        Column(name)
        for name in (
            "CompanyName",
            " CompanyNumber",
            "CompanyCategory",
            "CompanyStatus",
            "CountryOfOrigin",
            *CH_PREVIOUS_NAMES,
        )
    ),
    key=(" CompanyNumber",),
)

_DATA = "data"
_IDENTIFICATION = "data.identification"

CH_PSC = RealDataset(
    name="ch_psc",
    table="bronze_relationships",
    source=CH_SOURCE,
    file=PSC_FILE,
    format=Format.NDJSON,
    columns=(
        Column("company_number"),
        Column("kind", within=_DATA),
        Column("name", within=_DATA),
        Column("natures_of_control", within=_DATA, kind=Kind.TEXT_LIST),
        Column("notified_on", within=_DATA),
        Column("ceased_on", within=_DATA),
        Column("statement", within=_DATA),
        Column("nationality", within=_DATA),
        Column("month", within="data.date_of_birth", kind=Kind.INT),
        Column("year", within="data.date_of_birth", kind=Kind.INT),
        Column("self", within="data.links"),
        *(
            Column(name, within=_IDENTIFICATION)
            for name in (
                "country_registered",
                "place_registered",
                "registration_number",
                "legal_authority",
                "legal_form",
            )
        ),
    ),
    key=("data.links.self",),
    # The snapshot's summary line (`totals#…`) is in no dataset.
    select=("data.kind", frozenset(CH_KINDS)),
)

CH_DATASETS = (CH_COMPANIES, CH_PSC)
