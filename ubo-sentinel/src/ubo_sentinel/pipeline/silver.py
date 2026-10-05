"""Silver: canonical entities, edges and designations for one snapshot set.

Silver merges records across datasets, so it is built per snapshot set; two
sets live side by side. It is derived from Bronze and can be rebuilt.
"""

import hashlib
import json
from typing import Any

import duckdb

from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.models.reporting_exception import ReportingException
from ubo_sentinel.models.sanction import Sanction

_PROVENANCE = """
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    as_of DATE NOT NULL,
    confidence DOUBLE NOT NULL"""

# Table -> (columns after `snapshot_set_id`, key within a set). The key orders
# the rows for `silver_digest`.
_TABLES: dict[str, tuple[str, str]] = {
    "silver_entities": (
        f"""
    id TEXT NOT NULL,
    lei TEXT,
    registration_authority_id TEXT,
    registration_number TEXT,
    legal_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL,
    jurisdiction TEXT,
    status TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    repex_reason TEXT,{_PROVENANCE},
    supporting_records JSON NOT NULL""",
        "id",
    ),
    # Source record -> the entity it belongs to. `joined_by` is SELF for the
    # record that describes the entity, otherwise the identifier it shares.
    "silver_entity_xref": (
        """
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    joined_by TEXT NOT NULL,
    is_primary BOOLEAN NOT NULL""",
        "source, source_record_id",
    ),
    # Every name of an entity with its normalised form; `kind` is legal_name or alias.
    "silver_entity_names": (
        """
    entity_id TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    kind TEXT NOT NULL""",
        "entity_id, name",
    ),
    # Two entities that may be the same one. They are never merged.
    "silver_entity_links": (
        """
    entity_id_a TEXT NOT NULL,
    entity_id_b TEXT NOT NULL,
    match_type TEXT NOT NULL,
    confidence DOUBLE NOT NULL,
    name_a TEXT NOT NULL,
    name_b TEXT NOT NULL""",
        "entity_id_a, entity_id_b",
    ),
    "silver_relationships": (
        f"""
    id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    pct DECIMAL(7,4),
    basis TEXT NOT NULL,
    is_ultimate BOOLEAN NOT NULL,{_PROVENANCE},
    supporting_records JSON NOT NULL""",
        "id",
    ),
    "silver_sanctions": (
        f"""
    id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    designated_entity_id TEXT NOT NULL,
    program TEXT NOT NULL,
    list_date DATE,
    list_source TEXT NOT NULL,
    is_active BOOLEAN NOT NULL,
    match_type TEXT NOT NULL,
    match_confidence DOUBLE NOT NULL,{_PROVENANCE}""",
        "id",
    ),
    "silver_reporting_exceptions": (
        f"""
    entity_id TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,{_PROVENANCE}""",
        "source, source_record_id",
    ),
    # Gaps that belong to the set, not to one screen. `code` is a `GapCode`.
    "silver_gaps": (
        f"""
    code TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    detail JSON NOT NULL,{_PROVENANCE}""",
        "code, subject_id",
    ),
    # Source records that were not loaded, and why.
    "silver_rejects": (
        f"""
    dataset TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,{_PROVENANCE}""",
        "dataset, source, source_record_id",
    ),
}

SILVER_TABLES = tuple(_TABLES)

# `built_at` is the only wall-clock value in Silver; no id or hash reads it.
_BUILDS_DDL = """
CREATE TABLE IF NOT EXISTS silver_builds (
    snapshot_set_id TEXT PRIMARY KEY,
    engine_version TEXT NOT NULL,
    params JSON NOT NULL,
    counts JSON NOT NULL,
    silver_digest TEXT NOT NULL,
    built_at TIMESTAMP NOT NULL
)
"""


class SilverError(ValueError):
    """Silver cannot be built for this snapshot set. Nothing was written."""


def _column_names(columns: str) -> list[str]:
    return ["snapshot_set_id", *(line.split()[0] for line in columns.strip().splitlines())]


def _drop_outdated(con: duckdb.DuckDBPyConnection) -> None:
    """Drop Silver when a table has the columns of an earlier version.

    Silver is derived from Bronze, so nothing is lost: `ubo normalize` builds it again.
    """
    existing: dict[str, list[str]] = {}
    for table, column in con.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_name LIKE 'silver_%' ORDER BY table_name, ordinal_position"
    ).fetchall():
        existing.setdefault(table, []).append(column)
    if any(
        table in existing and existing[table] != _column_names(columns)
        for table, (columns, _) in _TABLES.items()
    ):
        for table in (*_TABLES, "silver_builds"):
            con.execute(f"DROP TABLE IF EXISTS {table}")


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    _drop_outdated(con)
    for table, (columns, key) in _TABLES.items():
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (\n    snapshot_set_id TEXT NOT NULL,{columns},"
            f"\n    PRIMARY KEY (snapshot_set_id, {key})\n)"
        )
    con.execute(_BUILDS_DDL)


def silver_digest(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> str:
    """SHA-256 over every Silver row of a set, table by table in key order.

    The same Bronze rows and the same code give the same digest on any machine.
    """
    digest = hashlib.sha256()
    for table, (_, key) in _TABLES.items():
        digest.update(table.encode("ascii") + b"\n")
        con.execute(
            f"SELECT * FROM {table} WHERE snapshot_set_id = ? ORDER BY {key}", [snapshot_set_id]
        )
        while batch := con.fetchmany(10_000):
            for row in batch:
                digest.update(canonical_json(list(row)) + b"\n")
    return digest.hexdigest()


def load_build(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> dict[str, Any] | None:
    """The registry row of a built set, or None when the set is not built."""
    row = con.execute(
        "SELECT engine_version, params, counts, silver_digest FROM silver_builds"
        " WHERE snapshot_set_id = ?",
        [snapshot_set_id],
    ).fetchone()
    if row is None:
        return None
    engine_version, params, counts, digest = row
    return {
        "engine_version": engine_version,
        "params": json.loads(params),
        "counts": json.loads(counts),
        "silver_digest": digest,
    }


def is_built(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> bool:
    return load_build(con, snapshot_set_id) is not None


def read_rows(
    con: duckdb.DuckDBPyConnection, table: str, snapshot_set_id: str
) -> list[dict[str, Any]]:
    """A set's rows of one Silver table as dicts, in key order."""
    if table not in _TABLES:
        raise ValueError(f"'{table}' is not a Silver table")
    cursor = con.execute(
        f"SELECT * EXCLUDE (snapshot_set_id) FROM {table} WHERE snapshot_set_id = ?"
        f" ORDER BY {_TABLES[table][1]}",
        [snapshot_set_id],
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def fold_provenance(row: dict[str, Any]) -> dict[str, Any]:
    """Fold the five provenance columns into one `provenance` value."""
    fields = {field: row.pop(field) for field in Provenance.model_fields}
    if "supporting_records" in row:
        row["supporting_records"] = json.loads(row["supporting_records"])
    return {**row, "provenance": fields}


def read_entities(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> list[Entity]:
    rows = read_rows(con, "silver_entities", snapshot_set_id)
    return [Entity.model_validate(fold_provenance(row)) for row in rows]


def read_relationships(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> list[Relationship]:
    rows = read_rows(con, "silver_relationships", snapshot_set_id)
    return [Relationship.model_validate(fold_provenance(row)) for row in rows]


def read_sanctions(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> list[Sanction]:
    rows = read_rows(con, "silver_sanctions", snapshot_set_id)
    return [Sanction.model_validate(fold_provenance(row)) for row in rows]


def read_reporting_exceptions(
    con: duckdb.DuckDBPyConnection, snapshot_set_id: str
) -> list[ReportingException]:
    rows = read_rows(con, "silver_reporting_exceptions", snapshot_set_id)
    return [ReportingException.model_validate(fold_provenance(row)) for row in rows]
