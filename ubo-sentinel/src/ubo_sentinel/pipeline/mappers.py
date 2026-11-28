"""Bronze `_raw` -> typed staging tables, one mapper per dataset.

Bronze keeps each record in its source's own shape. A mapper is the only code
that knows that shape; everything after staging is the same for every source.
"""

import re
from dataclasses import dataclass
from typing import get_args

import duckdb

from ubo_sentinel.models.entity import LeiStatus
from ubo_sentinel.models.fixture_rows import ALIAS_SEPARATOR
from ubo_sentinel.pipeline.bronze import BRONZE_TABLES

# Every staging table starts with the dataset the row came from and its provenance.
STAGE_PROVENANCE = (
    "dataset TEXT, source TEXT, source_record_id TEXT, snapshot_id TEXT,"
    " as_of DATE, confidence DOUBLE"
)

# Staging table -> its business columns. A `*_ref` is `<source>:<source_record_id>`
# of an entity record.
STAGE_TABLES: dict[str, str] = {
    # `successor_lei`: the LEI that replaced this record's (a duplicate or merged
    # LEI); the two records are one entity. `*_raw` is what the source wrote, kept
    # only to count the values the typed column could not read.
    "stg_entities": (
        "lei TEXT, registration_authority_id TEXT, registration_number TEXT, legal_name TEXT,"
        " aliases TEXT[], jurisdiction TEXT, status TEXT, lei_status TEXT, entity_type TEXT,"
        " successor_lei TEXT, jurisdiction_raw TEXT, lei_status_raw TEXT,"
        # A person's dates of birth (ISO, possibly partial: `1970`, `1970-03`) and
        # nationalities (ISO alpha-2), as the source states them (Step 19).
        " birth_dates TEXT[], nationalities TEXT[]"
    ),
    "stg_relationships": (
        "subject_ref TEXT, object_ref TEXT, rel_type TEXT, pct DECIMAL(7,4), basis TEXT,"
        " is_ultimate BOOLEAN, pct_raw TEXT,"
        # A stake as a range (Step 16): a point has both ends equal and `[]`; an
        # unknown stake has none. `interest` is `equity` or `voting`.
        " pct_low DECIMAL(7,4), pct_high DECIMAL(7,4), pct_bounds TEXT, interest TEXT"
    ),
    "stg_sanctions": (
        "entity_ref TEXT, program TEXT, list_date DATE, list_source TEXT, is_active BOOLEAN"
    ),
    "stg_repex": "entity_ref TEXT, category TEXT, reason TEXT",
}

# Records a mapper does not stage, counted by dataset and reason.
FILTERED_TABLE = "stg_filtered"
# Staged rows a mapper notes, counted by dataset and note (Step 17).
NOTED_TABLE = "stg_noted"


@dataclass(frozen=True)
class Mapper:
    stage_table: str
    # SQL expressions over a Bronze row, one per business column of the staging table.
    columns: tuple[str, ...]
    # (reason, SQL condition): a row meeting a condition is not staged, and is
    # counted under the first reason it meets. Bronze keeps it.
    filters: tuple[tuple[str, str], ...] = ()
    # A SQL condition: rows that are not this mapper's to stage at all, left out
    # without a count (another mapper of the dataset stages or counts them).
    skip: str | None = None
    # A SQL list: one staged row per element, read as `item` (one Bronze record
    # that states several things, as a PSC its natures of control).
    unnest: str | None = None
    # (note, SQL condition): staged rows meeting it are counted under the note.
    notes: tuple[tuple[str, str], ...] = ()


def _text(field: str) -> str:
    return f"_raw->>'{field}'"


def _cast(field: str, sql_type: str) -> str:
    return f"CAST(_raw->>'{field}' AS {sql_type})"


def _jurisdiction(field: str) -> str:
    """The country code, through `normalise_jurisdiction` (registered by the transformer)."""
    return f"ubo_jurisdiction(_raw->>'{field}')"


def _in(expression: str, values: tuple[str, ...] | list[str]) -> str:
    return f"{expression} IN ({', '.join(repr(v) for v in values)})"


# An ISO date, possibly partial: a year, a year and month, or a full date.
ISO_PARTIAL_DATE = r"[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?"
NO_VALUES = "CAST([] AS TEXT[])"


def _dates(values: str) -> str:
    """SQL: the ISO dates (`ISO_PARTIAL_DATE`) of a list, distinct and in order."""
    return (
        f"list_sort(list_distinct(list_filter(coalesce({values}, {NO_VALUES}),"
        f" d -> regexp_full_match(trim(d), '{ISO_PARTIAL_DATE}'))))"
    )


def _nationalities(values: str) -> str:
    """SQL: the countries a list of nationalities names (`ubo_nationality`), distinct
    and in order; a value that names no country is left out."""
    return (
        f"list_sort(list_distinct(list_filter(list_transform(coalesce({values}, {NO_VALUES}),"
        " n -> ubo_nationality(n)), c -> c IS NOT NULL)))"
    )


# Dataset -> mapper. The fixture files are already in the staging shape, so
# their mappers only read the cells and give them a type.
MAPPERS: dict[str, Mapper] = {
    "entities": Mapper(
        "stg_entities",
        (
            _text("lei"),
            _text("registration_authority_id"),
            _text("registration_number"),
            _text("legal_name"),
            f"coalesce(string_split(_raw->>'aliases', '{ALIAS_SEPARATOR}'), CAST([] AS TEXT[]))",
            _jurisdiction("jurisdiction"),
            _text("status"),
            _text("lei_status"),
            _text("entity_type"),
            "NULL",
            _text("jurisdiction"),
            _text("lei_status"),
            _dates(f"string_split(_raw->>'birth_dates', '{ALIAS_SEPARATOR}')"),
            _nationalities(f"string_split(_raw->>'nationalities', '{ALIAS_SEPARATOR}')"),
        ),
    ),
    "relationships": Mapper(
        "stg_relationships",
        (
            _text("subject_ref"),
            _text("object_ref"),
            _text("rel_type"),
            # A closed range of one value is the point it names.
            f"coalesce({_cast('pct', 'DECIMAL(7,4)')}, CASE WHEN {_text('pct_bounds')} = '[]'"
            f" AND {_cast('pct_low', 'DECIMAL(7,4)')} = {_cast('pct_high', 'DECIMAL(7,4)')}"
            f" THEN {_cast('pct_high', 'DECIMAL(7,4)')} END)",
            _text("basis"),
            _cast("is_ultimate", "BOOLEAN"),
            _text("pct"),
            # A file written before ranges states only `pct`: the point it names.
            f"coalesce({_cast('pct_low', 'DECIMAL(7,4)')}, {_cast('pct', 'DECIMAL(7,4)')})",
            f"coalesce({_cast('pct_high', 'DECIMAL(7,4)')}, {_cast('pct', 'DECIMAL(7,4)')})",
            f"coalesce({_text('pct_bounds')}, CASE WHEN {_text('pct')} IS NOT NULL THEN '[]' END)",
            f"coalesce({_text('interest')}, 'equity')",
        ),
    ),
    "sanctions": Mapper(
        "stg_sanctions",
        (
            _text("entity_ref"),
            _text("program"),
            _cast("list_date", "DATE"),
            _text("list_source"),
            _cast("is_active", "BOOLEAN"),
        ),
    ),
    "repex": Mapper("stg_repex", (_text("entity_ref"), _text("category"), _text("reason"))),
}


# --- GLEIF ------------------------------------------------------------------------------
# Field names are GLEIF's CSV headers (LEI-CDF 3.1, RR-CDF 2.1); `pipeline/loaders/
# gleif_loader.py` declares the ones read here. A ref is `gleif:<LEI>`.


def _g(field: str) -> str:
    """A GLEIF field; the names hold dots, so the path quotes them."""
    return f"(_raw->>'$.\"{field}\"')"


def _gleif_ref(field: str) -> str:
    return f"'gleif:' || {_g(field)}"


# Numbered fields hold up to five values each.
GLEIF_NAME_FIELDS = tuple(
    f"Entity.{group}.{item}.{i}"
    for group, item in (
        ("OtherEntityNames", "OtherEntityName"),
        ("TransliteratedOtherEntityNames", "TransliteratedOtherEntityName"),
    )
    for i in range(1, 6)
)
GLEIF_REPEX_REASON_FIELDS = tuple(f"Exception.Reason.{i}" for i in range(1, 6))

# GLEIF codes for "no registration authority" (RA999999) and "one not on GLEIF's
# list" (RA888888, named in free text). Neither identifies a register, so a
# number under either is not a key two records can share.
GLEIF_NO_AUTHORITY = ("RA999999", "RA888888")

# Registration statuses whose successor LEI replaced the record's own.
GLEIF_SUPERSEDED = ("DUPLICATE", "MERGED")

# RR relationship type -> (rel_type, basis, is_ultimate, subject end). The subject
# is the start node (child, branch, sub-fund) except for a fund's manager, who
# controls the fund. A branch is its head office; with no percentage, the
# presumption of control makes it a review, never a silent clear.
GLEIF_RELATIONS = {
    "IS_DIRECTLY_CONSOLIDATED_BY": ("CONSOLIDATED_BY", "accounting_consolidation", False, "Start"),
    "IS_ULTIMATELY_CONSOLIDATED_BY": ("CONSOLIDATED_BY", "accounting_consolidation", True, "Start"),
    "IS_INTERNATIONAL_BRANCH_OF": ("CONSOLIDATED_BY", "other", False, "Start"),
    "IS_SUBFUND_OF": ("CONSOLIDATED_BY", "other", False, "Start"),
    "IS_FUND-MANAGED_BY": ("CONTROLS", "other", False, "End"),
}

GLEIF_REPEX_CATEGORIES = {
    "DIRECT_ACCOUNTING_CONSOLIDATION_PARENT": "DIRECT_PARENT",
    "ULTIMATE_ACCOUNTING_CONSOLIDATION_PARENT": "ULTIMATE_PARENT",
}

# Why GLEIF records are not staged.
FILTER_INACTIVE = "INACTIVE_RELATIONSHIP"
FILTER_UNSUPPORTED = "UNSUPPORTED_REL_TYPE"
FILTER_NOT_LEI = "NODE_NOT_LEI"
FILTER_DELETED = "DELETED"
FILTER_UNKNOWN_CATEGORY = "UNSUPPORTED_CATEGORY"


def _case(expression: str, mapping: dict[str, str], default: str = "NULL") -> str:
    whens = " ".join(f"WHEN {key!r} THEN {value}" for key, value in mapping.items())
    return f"CASE {expression} {whens} ELSE {default} END"


def _gleif_entities() -> Mapper:
    status = _g("Registration.RegistrationStatus")
    entity_status = _g("Entity.EntityStatus")
    authority = _g("Entity.RegistrationAuthority.RegistrationAuthorityID")
    names = ", ".join(_g(field) for field in GLEIF_NAME_FIELDS)
    return Mapper(
        "stg_entities",
        (
            _g("LEI"),
            f"CASE WHEN NOT {_in(authority, GLEIF_NO_AUTHORITY)} THEN {authority} END",
            _g("Entity.RegistrationAuthority.RegistrationAuthorityEntityID"),
            _g("Entity.LegalName"),
            f"list_filter([{names}], name -> name IS NOT NULL)",
            f"ubo_jurisdiction({_g('Entity.LegalJurisdiction')})",
            # GLEIF writes NULL when it does not know.
            f"CASE WHEN {_in(entity_status, ('ACTIVE', 'INACTIVE'))} THEN {entity_status}"
            " ELSE 'UNKNOWN' END",
            f"CASE WHEN {_in(status, get_args(LeiStatus))} THEN {status} END",
            # A sole proprietor's legal name is a person's name: a Person, masked as one.
            f"CASE WHEN {_g('Entity.EntityCategory')} = 'SOLE_PROPRIETOR' THEN 'Person'"
            " ELSE 'LegalEntity' END",
            f"CASE WHEN {_in(status, GLEIF_SUPERSEDED)}"
            f" THEN {_g('Entity.SuccessorEntity.1.SuccessorLEI')} END",
            _g("Entity.LegalJurisdiction"),
            status,
            NO_VALUES,
            NO_VALUES,
        ),
    )


def _gleif_relationships() -> Mapper:
    rel_type = _g("Relationship.RelationshipType")
    pick = GLEIF_RELATIONS
    subject = {n: _gleif_ref(f"Relationship.{s[3]}Node.NodeID") for n, s in pick.items()}
    other = {"Start": "End", "End": "Start"}
    obj = {n: _gleif_ref(f"Relationship.{other[s[3]]}Node.NodeID") for n, s in pick.items()}
    return Mapper(
        "stg_relationships",
        (
            _case(rel_type, subject),
            _case(rel_type, obj),
            _case(rel_type, {n: repr(s[0]) for n, s in pick.items()}),
            "NULL",
            _case(rel_type, {n: repr(s[1]) for n, s in pick.items()}),
            _case(rel_type, {n: str(s[2]).lower() for n, s in pick.items()}),
            "NULL",
            "NULL",
            "NULL",
            "NULL",
            "'equity'",
        ),
        filters=(
            (FILTER_DELETED, f"{_g('DeletedAt')} IS NOT NULL"),
            # GLEIF writes INACTIVE, or the word NULL, for a relationship that has ended.
            (FILTER_INACTIVE, f"{_g('Relationship.RelationshipStatus')} IS DISTINCT FROM 'ACTIVE'"),
            (FILTER_UNSUPPORTED, f"NOT {_in(rel_type, tuple(GLEIF_RELATIONS))}"),
            (
                FILTER_NOT_LEI,
                f"{_g('Relationship.StartNode.NodeIDType')} IS DISTINCT FROM 'LEI'"
                f" OR {_g('Relationship.EndNode.NodeIDType')} IS DISTINCT FROM 'LEI'",
            ),
        ),
    )


def _gleif_repex() -> Mapper:
    category = _g("Exception.Category")
    reasons = ", ".join(_g(field) for field in GLEIF_REPEX_REASON_FIELDS)
    return Mapper(
        "stg_repex",
        (
            _gleif_ref("LEI"),
            _case(category, {k: repr(v) for k, v in GLEIF_REPEX_CATEGORIES.items()}),
            # A record may give several reasons; Silver keeps one row per record.
            f"array_to_string(list_filter([{reasons}], r -> r IS NOT NULL), '; ')",
        ),
        filters=(
            (FILTER_DELETED, f"{_g('DeletedAt')} IS NOT NULL"),
            (FILTER_UNKNOWN_CATEGORY, f"NOT {_in(category, tuple(GLEIF_REPEX_CATEGORIES))}"),
        ),
    )


MAPPERS |= {
    "gleif_l1": _gleif_entities(),
    "gleif_rr": _gleif_relationships(),
    "gleif_repex": _gleif_repex(),
}


# --- OpenSanctions ----------------------------------------------------------------------
# FollowTheMoney records (`pipeline/loaders/opensanctions_loader.py` declares the
# fields read). Every property is a list; a ref is `opensanctions:<id>`.


def _os(field: str) -> str:
    """A top-level field of the record."""
    return f"(_raw->>'$.{field}')"


def _first(prop: str) -> str:
    """The first value of a property, in the order published."""
    return f"(_raw->>'$.properties.{prop}[0]')"


def _values(prop: str) -> str:
    return f"coalesce(CAST(_raw->'$.properties.{prop}' AS TEXT[]), CAST([] AS TEXT[]))"


def _os_ref(prop: str) -> str:
    return f"'opensanctions:' || {_first(prop)}"


# FtM schema -> the node label of the graph. Other things a list names (a vessel,
# an aircraft, a security) are not owners or owned in this graph.
OS_ENTITY_SCHEMAS = {
    "Company": "LegalEntity",
    "Organization": "LegalEntity",
    "LegalEntity": "LegalEntity",
    "PublicBody": "LegalEntity",
    "Person": "Person",
}
OS_OTHER_SCHEMAS = ("Vessel", "Airplane", "Security")
# Link schemas: those that map, and the others, which are counted.
OS_LINK_SCHEMAS = {
    # schema -> (rel_type, basis, subject property, object property)
    "Ownership": ("OWNS", "shareholding", "owner", "asset"),
    "Directorship": ("CONTROLS", "directorship", "director", "organization"),
    # No direction: Silver puts the ends in id order (Step 20).
    "Family": ("RELATED_TO", "family", "person", "relative"),
    "Associate": ("RELATED_TO", "associate", "person", "associate"),
}
OS_OTHER_LINK_SCHEMAS = (
    "Employment",
    "Membership",
    "Occupancy",
    "Representation",
    "Succession",
    "UnknownLink",
)

FILTER_UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
FILTER_NO_ENTITY = "NO_ENTITY"
FILTER_NO_ENDS = "NO_ENDS"


def os_percentage(text: str) -> tuple[str, str, str, str]:
    """SQL reading an FtM `percentage` as a stake: (pct, pct_low, pct_high, pct_bounds).

    `50`, `50%`, `50.0` and `50,5` are the point they write (`pct` and both ends
    equal, `[]`). A range is kept as one, never rounded to a bound: `25-50` is
    `[25, 50]`, `<10` is `(0, 10)`, `≤10` `(0, 10]`, `>50` `(50, 100]` and `≥50`
    `[50, 100]`; `pct` is NULL. Anything else (`majority`) is NULL in all four:
    an edge of unknown share, a `NULL_PCT_EDGE` gap.
    """
    number = r"\d+(\.\d+)?"
    clean = f"trim(replace(replace({text}, '%', ''), ',', '.'))"
    single = f"regexp_full_match({clean}, '[<>≤≥~]?\\s*{number}')"
    pair = f"regexp_full_match({clean}, '{number}\\s*[-–]\\s*{number}')"
    sign = f"regexp_extract({clean}, '^([<>≤≥~]?)', 1)"
    first = f"TRY_CAST(regexp_extract({clean}, '{number}') AS DECIMAL(9,4))"
    second = f"TRY_CAST(regexp_extract({clean}, '[-–]\\s*({number})$', 1) AS DECIMAL(9,4))"
    low = (
        f"CASE WHEN {single} THEN CASE WHEN {sign} IN ('<', '≤') THEN 0 ELSE {first} END"
        f" WHEN {pair} THEN {first} END"
    )
    high = (
        f"CASE WHEN {single} THEN CASE WHEN {sign} IN ('>', '≥') THEN 100 ELSE {first} END"
        f" WHEN {pair} THEN {second} END"
    )
    bounds = (
        f"CASE WHEN {single} THEN CASE {sign} WHEN '<' THEN '()' WHEN '≤' THEN '(]'"
        f" WHEN '>' THEN '(]' ELSE '[]' END WHEN {pair} THEN '[]' END"
    )
    # A stake within (0, 100], low end first; an open range of no width is no range.
    valid = (
        f"(({low}) >= 0 AND ({high}) > 0 AND ({high}) <= 100 AND ({low}) <= ({high})"
        f" AND NOT (({low}) = ({high}) AND ({bounds}) <> '[]'))"
    )
    return (
        f"CASE WHEN {valid} AND ({low}) = ({high}) THEN CAST({high} AS DECIMAL(7,4)) END",
        f"CASE WHEN {valid} THEN CAST({low} AS DECIMAL(7,4)) END",
        f"CASE WHEN {valid} THEN CAST({high} AS DECIMAL(7,4)) END",
        f"CASE WHEN {valid} THEN {bounds} END",
    )


def _os_entities() -> Mapper:
    schema = _os("schema")
    jurisdiction = (
        # A record's own jurisdiction; else its country, when it gives only one.
        f"coalesce({_first('jurisdiction')}, CASE WHEN len({_values('country')}) = 1"
        f" THEN {_first('country')} END)"
    )
    return Mapper(
        "stg_entities",
        (
            f"upper(trim({_first('leiCode')}))",
            "NULL",
            "NULL",
            f"coalesce({_os('caption')}, {_first('name')})",
            # A weak alias (a short or partial name) is too loose to link on; it stays in Bronze.
            f"list_concat({_values('name')}, {_values('alias')}, {_values('previousName')})",
            f"ubo_jurisdiction({jurisdiction})",
            # A list says an entity is designated, not whether it still exists.
            "'UNKNOWN'",
            # A list states an LEI, not its status.
            "NULL",
            _case(schema, {k: repr(v) for k, v in OS_ENTITY_SCHEMAS.items()}),
            "NULL",
            jurisdiction,
            "NULL",
            # FtM `birthDate` is an ISO date, often partial; `nationality` and
            # `citizenship` are country codes. Records read before Step 19 state none.
            _dates(_values("birthDate")),
            _nationalities(f"list_concat({_values('nationality')}, {_values('citizenship')})"),
        ),
        filters=((FILTER_UNSUPPORTED_SCHEMA, f"NOT {_in(schema, tuple(OS_ENTITY_SCHEMAS))}"),),
    )


def _os_sanctions() -> Mapper:
    end = _first("endDate")
    return Mapper(
        "stg_sanctions",
        (
            _os_ref("entity"),
            f"coalesce({_first('programId')}, {_first('program')}, {_os('datasets[0]')})",
            # FtM dates may be a year or a month alone; those give no list date.
            f"TRY_CAST(coalesce({_first('listingDate')}, {_first('startDate')}) AS DATE)",
            # The list the designation is on: the record's own dataset.
            _os("datasets[0]"),
            # In force on the publish date (`_as_of`), never the clock. ISO dates
            # compare as text, so a partial end date (`2023`) is read too.
            f"{end} IS NULL OR {end} > CAST(_as_of AS VARCHAR)",
        ),
        filters=((FILTER_NO_ENTITY, f"{_first('entity')} IS NULL"),),
    )


def _os_relationships() -> Mapper:
    schema = _os("schema")
    specs = OS_LINK_SCHEMAS
    stake = os_percentage(_first("percentage"))
    return Mapper(
        "stg_relationships",
        (
            _case(schema, {s: _os_ref(spec[2]) for s, spec in specs.items()}),
            _case(schema, {s: _os_ref(spec[3]) for s, spec in specs.items()}),
            _case(schema, {s: repr(spec[0]) for s, spec in specs.items()}),
            f"CASE WHEN {schema} = 'Ownership' THEN {stake[0]} END",
            _case(schema, {s: repr(spec[1]) for s, spec in specs.items()}),
            "false",
            _first("percentage"),
            *(f"CASE WHEN {schema} = 'Ownership' THEN {end} END" for end in stake[1:]),
            "'equity'",
        ),
        filters=(
            (FILTER_UNSUPPORTED, f"NOT {_in(schema, tuple(specs))}"),
            (
                FILTER_NO_ENDS,
                " OR ".join(
                    f"({schema} = '{s}' AND {_first(spec[2])} IS NULL)"
                    f" OR ({schema} = '{s}' AND {_first(spec[3])} IS NULL)"
                    for s, spec in specs.items()
                ),
            ),
        ),
    )


MAPPERS |= {
    "os_entities": _os_entities(),
    "os_sanctions": _os_sanctions(),
    "os_relationships": _os_relationships(),
}


# --- Companies House (Step 17) ------------------------------------------------------------
# The free company data product (`ch_companies`, CSV; some headers begin with a
# space, as published) and the PSC snapshot (`ch_psc`, one JSON object per line).
# A company's ref is `companies_house:<company number>`; a person with significant
# control is its own record, `companies_house:<links.self>`.

CH_SOURCE = "companies_house"
# GLEIF's registration authorities for Companies House: Northern Ireland's
# register (numbers `NI…`, `NC…`, `R0…`) is RA000586, the others RA000585
# (checked against GLEIF's L1 file, 2026-11-23).
CH_AUTHORITY = "RA000585"
CH_AUTHORITY_NI = "RA000586"
CH_PREVIOUS_NAMES = tuple(f" PreviousName_{i}.CompanyName" for i in range(1, 11))

# PSC kinds. A holder is an individual, a corporate entity or a legal person
# (a beneficial owner of an overseas entity likewise); a super-secure PSC is not
# named, so it is a statement, not a holder.
CH_INDIVIDUAL = ("individual-person-with-significant-control", "individual-beneficial-owner")
CH_CORPORATE = (
    "corporate-entity-person-with-significant-control",
    "corporate-entity-beneficial-owner",
    "legal-person-person-with-significant-control",
    "legal-person-beneficial-owner",
)
CH_SUPER_SECURE = ("super-secure-person-with-significant-control", "super-secure-beneficial-owner")
CH_STATEMENT = "persons-with-significant-control-statement"
CH_KINDS = (*CH_INDIVIDUAL, *CH_CORPORATE, *CH_SUPER_SECURE, CH_STATEMENT, "exemptions")

# Statements that say a person with significant control exists and is not known
# or not confirmed: a `REPORTING_EXCEPTION`, as a GLEIF reporting exception is.
CH_GAP_STATEMENTS = tuple(
    code + suffix
    for code in (
        "psc-exists-but-not-identified",
        "psc-details-not-confirmed",
        "steps-to-find-psc-not-yet-completed",
        "psc-contacted-but-no-response",
        "psc-has-failed-to-confirm-changed-details",
        "restrictions-notice-issued-to-psc",
    )
    for suffix in ("", "-partnership")
) + (
    "no-beneficial-owner-identified",
    "at-least-one-beneficial-owner-unidentified",
    "information-not-provided-for-at-least-one-beneficial-owner",
    "at-least-one-beneficial-owner-unidentified-and-information-not-provided-for-at-least-one-beneficial-owner",
    "awaiting-confirmation-from-psc",
)
# "Nobody is registrable": recorded (counted), and no gap. The code is spelt as published.
CH_NO_PSC_STATEMENTS = (
    "no-individual-or-entity-with-signficant-control",
    "no-individual-or-entity-with-signficant-control-partnership",
)

# Every nature of control Companies House publishes (`psc_descriptions.yml` of
# companieshouse/api-enumerations, read 2026-11-23).
CH_NATURES = (
    *(
        f"{measure}-{band}-percent{form}"
        for measure in ("ownership-of-shares", "voting-rights")
        for band in ("25-to-50", "50-to-75", "75-to-100")
        for form in ("", "-as-trust", "-as-firm")
    ),
    *(
        f"{measure}-more-than-25-percent{form}-registered-overseas-entity"
        for measure in ("ownership-of-shares", "voting-rights")
        for form in ("", "-as-trust", "-as-firm", "-as-control-over-trust", "-as-control-over-firm")
    ),
    "right-to-appoint-and-remove-directors",
    "right-to-appoint-and-remove-directors-as-trust",
    "right-to-appoint-and-remove-directors-as-firm",
    "right-to-appoint-and-remove-directors-as-control-over-trust-registered-overseas-entity",
    "right-to-appoint-and-remove-directors-as-control-over-firm-registered-overseas-entity",
    "significant-influence-or-control",
    "significant-influence-or-control-as-trust",
    "significant-influence-or-control-as-firm",
    *(
        f"{measure}-{band}-percent{form}-limited-liability-partnership"
        for measure in ("right-to-share-surplus-assets", "voting-rights")
        for band in ("25-to-50", "50-to-75", "75-to-100")
        for form in ("", "-as-trust", "-as-firm")
    ),
    "right-to-appoint-and-remove-members-limited-liability-partnership",
    "right-to-appoint-and-remove-members-as-trust-limited-liability-partnership",
    "right-to-appoint-and-remove-members-as-firm-limited-liability-partnership",
    "significant-influence-or-control-limited-liability-partnership",
    "significant-influence-or-control-as-trust-limited-liability-partnership",
    "significant-influence-or-control-as-firm-limited-liability-partnership",
    *(
        f"significant-influence-or-control{form}-registered-overseas-entity"
        for form in ("", "-as-trust", "-as-firm", "-as-control-over-trust", "-as-control-over-firm")
    ),
    *(
        f"part-right-to-share-surplus-assets-{band}-percent{form}"
        for band in ("25-to-50", "50-to-75", "75-to-100")
        for form in ("", "-as-trust", "-as-firm")
    ),
    "right-to-appoint-and-remove-person",
    "right-to-appoint-and-remove-person-as-firm",
    "right-to-appoint-and-remove-person-as-trust",
    "right-to-appoint-and-remove-directors-registered-overseas-entity",
    "right-to-appoint-and-remove-directors-as-trust-registered-overseas-entity",
    "right-to-appoint-and-remove-directors-as-firm-registered-overseas-entity",
    *(
        f"registered-owner-as-nominee-{who}-{where}-registered-overseas-entity"
        for who in ("person", "another-entity")
        for where in ("england-wales", "scotland", "northern-ireland")
    ),
)

# A band -> (low, high, bounds), as the register defines it: "more than 25 % but
# not more than 50 %", "more than 50 % but less than 75 %", "75 % or more".
CH_BANDS = {
    "25-to-50": ("25", "50", "(]"),
    "50-to-75": ("50", "75", "()"),
    "75-to-100": ("75", "100", "[]"),
    "more-than-25": ("25", "100", "(]"),
}


@dataclass(frozen=True)
class NatureEdge:
    """The edge one nature of control states."""

    rel_type: str
    basis: str
    interest: str = "equity"
    low: str | None = None
    high: str | None = None
    bounds: str | None = None
    # Held as a trustee or a firm member: mapped as the plain form until Step 21.
    trust_or_firm: bool = False


_FORMS = re.compile(r"-as-(control-over-)?(trust|firm)")
_SCOPES = re.compile(r"-(limited-liability-partnership|registered-overseas-entity)$")
_STAKE = re.compile(
    r"(ownership-of-shares|voting-rights|right-to-share-surplus-assets"
    r"|part-right-to-share-surplus-assets)-(25-to-50|50-to-75|75-to-100|more-than-25)-percent"
)


def ch_nature(code: str) -> NatureEdge | None:
    """The edge a nature of control states; None for one the graph does not hold
    (a nominee registered owner of an overseas entity's land)."""
    plain = _FORMS.sub("", code)
    trust_or_firm = plain != code
    base = _SCOPES.sub("", plain)
    found = _STAKE.fullmatch(base)
    if found:
        low, high, bounds = CH_BANDS[found[2]]
        interest = "voting" if found[1] == "voting-rights" else "equity"
        return NatureEdge("OWNS", "shareholding", interest, low, high, bounds, trust_or_firm)
    if re.fullmatch(r"right-to-appoint-and-remove-(directors|members|person)", base):
        return NatureEdge("CONTROLS", "board_appointment", trust_or_firm=trust_or_firm)
    if base == "significant-influence-or-control":
        return NatureEdge("CONTROLS", "significant_influence", trust_or_firm=trust_or_firm)
    return None


CH_NATURE_EDGES = {code: edge for code in CH_NATURES if (edge := ch_nature(code)) is not None}


def _psc(path: str) -> str:
    """A text value of a PSC record: `company_number`, or a dotted path inside `data`."""
    return f"(_raw->>'$.{path}')"


def _ch_ref(number: str) -> str:
    return f"'{CH_SOURCE}:' || {number}"


def _ch_number(value: str) -> str:
    """A company number as the register writes it: upper case, all-digit numbers to 8."""
    clean = f"upper(replace(trim({value}), ' ', ''))"
    digits = f"regexp_full_match({clean}, '[0-9]+')"
    return f"CASE WHEN {digits} THEN lpad({clean}, 8, '0') ELSE {clean} END"


_PSC_KIND = _psc("data.kind")
_PSC_CEASED = f"{_psc('data.ceased_on')} IS NOT NULL"
_PSC_NO_COMPANY = f"{_psc('company_number')} IS NULL"
_UK_COUNTRIES = (
    "england", "wales", "scotland", "northern ireland", "england and wales", "england & wales",
    "england/wales", "united kingdom", "uk", "great britain",
)  # fmt: skip
_PSC_REG_NUMBER = _psc("data.identification.registration_number")
_PSC_REG_PLACE = _psc("data.identification.place_registered")
_PSC_REG_COUNTRY = f"lower(trim({_psc('data.identification.country_registered')}))"
# A corporate PSC on a UK register: the edge names that company's own record.
_PSC_UK_CORPORATE = (
    f"({_in(_PSC_KIND, CH_CORPORATE)} AND {_PSC_REG_NUMBER} IS NOT NULL"
    f" AND ({_PSC_REG_PLACE} ILIKE '%companies house%'"
    f" OR {_in(_PSC_REG_COUNTRY, _UK_COUNTRIES)}))"
)


def _nature_case(value: str) -> str:
    """SQL: the given field of the edge each nature of control (`item`) states."""
    whens = " ".join(
        f"WHEN {code!r} THEN {value_sql}"
        for code, edge in sorted(CH_NATURE_EDGES.items())
        if (value_sql := _nature_value(edge, value)) != "NULL"
    )
    return f"CASE item {whens} END" if whens else "NULL"


def _nature_value(edge: NatureEdge, field: str) -> str:
    value = getattr(edge, field)
    if value is None:
        return "NULL"
    if field in ("low", "high"):
        return f"CAST({value} AS DECIMAL(7,4))"
    return repr(value)


def _ch_companies() -> Mapper:
    number = _g(" CompanyNumber")
    status = _g("CompanyStatus")
    names = ", ".join(_g(field) for field in CH_PREVIOUS_NAMES)
    return Mapper(
        "stg_entities",
        (
            "NULL",
            f"CASE WHEN {number} LIKE 'N%' OR {number} LIKE 'R0%'"
            f" THEN '{CH_AUTHORITY_NI}' ELSE '{CH_AUTHORITY}' END",
            number,
            _g("CompanyName"),
            f"list_filter([{names}], name -> name IS NOT NULL)",
            "ubo_jurisdiction('GB')",
            f"CASE WHEN {status} = 'Active' THEN 'ACTIVE'"
            f" WHEN {status} ILIKE 'dissolved%' OR {status} ILIKE 'removed%' THEN 'INACTIVE'"
            " ELSE 'UNKNOWN' END",
            "NULL",
            "'LegalEntity'",
            "NULL",
            "'GB'",
            "NULL",
            NO_VALUES,
            NO_VALUES,
        ),
    )


def _ch_psc_relationships() -> Mapper:
    holder = _ch_ref(
        f"CASE WHEN {_PSC_UK_CORPORATE}"
        f" THEN {_ch_number(_PSC_REG_NUMBER)}"
        f" ELSE {_psc('data.links.self')} END"
    )
    low, high = _nature_case("low"), _nature_case("high")
    return Mapper(
        "stg_relationships",
        (
            holder,
            _ch_ref(_psc("company_number")),
            _nature_case("rel_type"),
            f"CASE WHEN ({low}) = ({high}) THEN {high} END",
            _nature_case("basis"),
            "false",
            "NULL",
            low,
            high,
            _nature_case("bounds"),
            f"coalesce({_nature_case('interest')}, 'equity')",
        ),
        filters=(
            (FILTER_NO_ENDS, _PSC_NO_COMPANY),
            ("CEASED", _PSC_CEASED),
            ("UNSUPPORTED_NATURE", f"({_nature_case('rel_type')}) IS NULL"),
        ),
        skip=f"NOT {_in(_PSC_KIND, (*CH_INDIVIDUAL, *CH_CORPORATE))}",
        unnest=(
            "coalesce(CAST(_raw->'$.data.natures_of_control' AS VARCHAR[]), CAST([] AS VARCHAR[]))"
        ),
        notes=(
            (
                "TRUST_OR_FIRM",
                "item LIKE '%-as-trust%' OR item LIKE '%-as-firm%'"
                " OR item LIKE '%-as-control-over-%'",
            ),
        ),
    )


def _ch_psc_entities() -> Mapper:
    """A PSC holder's own record: a person, or a corporate PSC on no UK register."""
    country = _psc("data.identification.country_registered")
    return Mapper(
        "stg_entities",
        (
            "NULL",
            "NULL",
            "NULL",
            _psc("data.name"),
            "CAST([] AS TEXT[])",
            f"ubo_jurisdiction({country})",
            "'UNKNOWN'",
            "NULL",
            f"CASE WHEN {_in(_PSC_KIND, CH_INDIVIDUAL)} THEN 'Person' ELSE 'LegalEntity' END",
            "NULL",
            country,
            "NULL",
            # The register states a month and year of birth, never the day.
            _dates(
                f"[{_psc('data.date_of_birth.year')}"
                f" || coalesce('-' || lpad({_psc('data.date_of_birth.month')}, 2, '0'), '')]"
            ),
            # Free text ("British"); several are written with commas.
            _nationalities(f"regexp_split_to_array({_psc('data.nationality')}, '\\s*[,/;]\\s*')"),
        ),
        skip=(
            f"NOT ({_in(_PSC_KIND, CH_INDIVIDUAL)} OR ({_in(_PSC_KIND, CH_CORPORATE)}"
            f" AND NOT {_PSC_UK_CORPORATE})) OR {_PSC_CEASED} OR {_PSC_NO_COMPANY}"
            f" OR {_psc('data.name')} IS NULL"
        ),
    )


def _ch_psc_statements() -> Mapper:
    """A statement that a PSC is not known or not confirmed, or a super-secure PSC."""
    statement = _psc("data.statement")
    return Mapper(
        "stg_repex",
        (
            _ch_ref(_psc("company_number")),
            "'PSC_STATEMENT'",
            f"coalesce({statement}, {_PSC_KIND})",
        ),
        filters=(
            (FILTER_NO_ENDS, _PSC_NO_COMPANY),
            ("CEASED", _PSC_CEASED),
            ("NO_REGISTRABLE_PERSON", _in(f"coalesce({statement}, '')", CH_NO_PSC_STATEMENTS)),
            (
                "OTHER_STATEMENT",
                f"{_PSC_KIND} = '{CH_STATEMENT}'"
                f" AND NOT {_in(f'coalesce({statement}, chr(0))', CH_GAP_STATEMENTS)}",
            ),
        ),
        skip=f"NOT {_in(_PSC_KIND, (*CH_SUPER_SECURE, CH_STATEMENT))}",
    )


MAPPERS |= {"ch_companies": _ch_companies(), "ch_psc": _ch_psc_relationships()}
# Further mappers of one dataset, run after `MAPPERS[dataset]`.
MORE_MAPPERS: dict[str, tuple[Mapper, ...]] = {
    "ch_psc": (_ch_psc_entities(), _ch_psc_statements()),
}


# Holdings and holders confirmed from documents (Step 15) are written in the
# fixture shape, with the document's evidence beside it in `_raw`.
MAPPERS |= {
    "doc_entities": MAPPERS["entities"],
    "doc_relationships": MAPPERS["relationships"],
}


def create_stage_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Empty staging tables, so a set without some dataset still normalises."""
    for table, columns in STAGE_TABLES.items():
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} ({STAGE_PROVENANCE}, {columns})")
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {FILTERED_TABLE} (dataset TEXT, reason TEXT, n BIGINT)"
    )
    con.execute(f"CREATE OR REPLACE TEMP TABLE {NOTED_TABLE} (dataset TEXT, note TEXT, n BIGINT)")


def stage(
    con: duckdb.DuckDBPyConnection, dataset: str, bronze_table: str, snapshot_id: str
) -> None:
    """Copy one snapshot's rows into the staging tables of its mappers, and count those
    they filter."""
    if bronze_table not in BRONZE_TABLES:
        raise ValueError(f"'{bronze_table}' is not a Bronze table")
    for mapper in (MAPPERS[dataset], *MORE_MAPPERS.get(dataset, ())):
        _stage_one(con, dataset, mapper, bronze_table, snapshot_id)


def _stage_one(
    con: duckdb.DuckDBPyConnection,
    dataset: str,
    mapper: Mapper,
    bronze_table: str,
    snapshot_id: str,
) -> None:
    item = f", unnest({mapper.unnest}) AS item" if mapper.unnest else ""
    skip = f" AND NOT coalesce({mapper.skip}, false)" if mapper.skip else ""
    rows = f"(SELECT *{item} FROM {bronze_table} WHERE _snapshot_id = ?{skip})"
    order = "_row_num, item" if mapper.unnest else "_row_num"
    keep = "true"
    if mapper.filters:
        whens = " ".join(f"WHEN coalesce({c}, false) THEN '{r}'" for r, c in mapper.filters)
        filtered = f"CASE {whens} END"
        keep = f"({filtered}) IS NULL"
        # A record is counted once under a reason, however many rows it unnests to.
        con.execute(
            f"INSERT INTO {FILTERED_TABLE} SELECT ?, reason, count(DISTINCT _row_num) FROM ("
            f" SELECT _row_num, {filtered} AS reason FROM {rows})"
            " WHERE reason IS NOT NULL GROUP BY reason",
            [dataset, snapshot_id],
        )
    for note, condition in mapper.notes:
        con.execute(
            f"INSERT INTO {NOTED_TABLE} SELECT ?, ?, count(*) FROM {rows}"
            f" WHERE {keep} AND coalesce({condition}, false) HAVING count(*) > 0",
            [dataset, note, snapshot_id],
        )
    con.execute(
        f"INSERT INTO {mapper.stage_table}"
        " SELECT ?, _source, _source_record_id, _snapshot_id, _as_of, _confidence,"
        f" {', '.join(mapper.columns)}"
        f" FROM {rows} WHERE {keep} ORDER BY {order}",
        [dataset, snapshot_id],
    )
