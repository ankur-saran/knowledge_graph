"""Bronze `_raw` -> typed staging tables, one mapper per dataset.

Bronze keeps each record in its source's own shape. A mapper is the only code
that knows that shape; everything after staging is the same for every source.
"""

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
        " successor_lei TEXT, jurisdiction_raw TEXT, lei_status_raw TEXT"
    ),
    "stg_relationships": (
        "subject_ref TEXT, object_ref TEXT, rel_type TEXT, pct DECIMAL(7,4), basis TEXT,"
        " is_ultimate BOOLEAN"
    ),
    "stg_sanctions": (
        "entity_ref TEXT, program TEXT, list_date DATE, list_source TEXT, is_active BOOLEAN"
    ),
    "stg_repex": "entity_ref TEXT, category TEXT, reason TEXT",
}

# Records a mapper does not stage, counted by dataset and reason.
FILTERED_TABLE = "stg_filtered"


@dataclass(frozen=True)
class Mapper:
    stage_table: str
    # SQL expressions over a Bronze row, one per business column of the staging table.
    columns: tuple[str, ...]
    # (reason, SQL condition): a row meeting a condition is not staged, and is
    # counted under the first reason it meets. Bronze keeps it.
    filters: tuple[tuple[str, str], ...] = ()


def _text(field: str) -> str:
    return f"_raw->>'{field}'"


def _cast(field: str, sql_type: str) -> str:
    return f"CAST(_raw->>'{field}' AS {sql_type})"


def _jurisdiction(field: str) -> str:
    """The country code, through `normalise_jurisdiction` (registered by the transformer)."""
    return f"ubo_jurisdiction(_raw->>'{field}')"


def _in(expression: str, values: tuple[str, ...] | list[str]) -> str:
    return f"{expression} IN ({', '.join(repr(v) for v in values)})"


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
        ),
    ),
    "relationships": Mapper(
        "stg_relationships",
        (
            _text("subject_ref"),
            _text("object_ref"),
            _text("rel_type"),
            _cast("pct", "DECIMAL(7,4)"),
            _text("basis"),
            _cast("is_ultimate", "BOOLEAN"),
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


def create_stage_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Empty staging tables, so a set without some dataset still normalises."""
    for table, columns in STAGE_TABLES.items():
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} ({STAGE_PROVENANCE}, {columns})")
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {FILTERED_TABLE} (dataset TEXT, reason TEXT, n BIGINT)"
    )


def stage(
    con: duckdb.DuckDBPyConnection, dataset: str, bronze_table: str, snapshot_id: str
) -> None:
    """Copy one snapshot's rows into its staging table, and count those it filters."""
    if bronze_table not in BRONZE_TABLES:
        raise ValueError(f"'{bronze_table}' is not a Bronze table")
    mapper = MAPPERS[dataset]
    keep = "true"
    if mapper.filters:
        whens = " ".join(f"WHEN coalesce({c}, false) THEN '{r}'" for r, c in mapper.filters)
        filtered = f"CASE {whens} END"
        keep = f"({filtered}) IS NULL"
        con.execute(
            f"INSERT INTO {FILTERED_TABLE} SELECT ?, reason, count(*) FROM ("
            f" SELECT {filtered} AS reason FROM {bronze_table} WHERE _snapshot_id = ?)"
            " WHERE reason IS NOT NULL GROUP BY reason",
            [dataset, snapshot_id],
        )
    con.execute(
        f"INSERT INTO {mapper.stage_table}"
        " SELECT ?, _source, _source_record_id, _snapshot_id, _as_of, _confidence,"
        f" {', '.join(mapper.columns)}"
        f" FROM {bronze_table} WHERE _snapshot_id = ? AND {keep} ORDER BY _row_num",
        [dataset, snapshot_id],
    )
