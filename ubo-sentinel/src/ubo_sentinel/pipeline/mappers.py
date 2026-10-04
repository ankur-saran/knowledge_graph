"""Bronze `_raw` -> typed staging tables, one mapper per dataset.

Bronze keeps each record in its source's own shape. A mapper is the only code
that knows that shape; everything after staging is the same for every source.
"""

from dataclasses import dataclass

import duckdb

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
    "stg_entities": (
        "lei TEXT, registration_authority_id TEXT, registration_number TEXT, legal_name TEXT,"
        " aliases TEXT[], jurisdiction TEXT, status TEXT, entity_type TEXT"
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


@dataclass(frozen=True)
class Mapper:
    stage_table: str
    # SQL expressions over a Bronze row, one per business column of the staging table.
    columns: tuple[str, ...]


def _text(field: str) -> str:
    return f"_raw->>'{field}'"


def _cast(field: str, sql_type: str) -> str:
    return f"CAST(_raw->>'{field}' AS {sql_type})"


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
            _text("jurisdiction"),
            _text("status"),
            _text("entity_type"),
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


def create_stage_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Empty staging tables, so a set without some dataset still normalises."""
    for table, columns in STAGE_TABLES.items():
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} ({STAGE_PROVENANCE}, {columns})")


def stage(
    con: duckdb.DuckDBPyConnection, dataset: str, bronze_table: str, snapshot_id: str
) -> None:
    """Copy one snapshot's rows into its staging table."""
    if bronze_table not in BRONZE_TABLES:
        raise ValueError(f"'{bronze_table}' is not a Bronze table")
    mapper = MAPPERS[dataset]
    con.execute(
        f"INSERT INTO {mapper.stage_table}"
        " SELECT ?, _source, _source_record_id, _snapshot_id, _as_of, _confidence,"
        f" {', '.join(mapper.columns)}"
        f" FROM {bronze_table} WHERE _snapshot_id = ? ORDER BY _row_num",
        [dataset, snapshot_id],
    )
