"""Digests of derived tables, computed in the database.

A digest names the rows of one snapshot set: the same rows give the same
digest on any machine, and any changed row changes it. Silver and Gold of a
full registry hold tens of millions of rows, too many to serialise one by one
in Python, so the database writes each row as text, hashes it and folds the
hashes; Python only joins the folded hashes.

Each row is written as `canonical_json(list(row))` would write it (a JSON array
of the row's values in column order: a DECIMAL as a quoted 4-place string, a
DATE as a quoted ISO date, a JSON column as a quoted string). `row_texts()`
returns those strings so a test can hold the two to each other.
"""

import hashlib
from collections.abc import Iterable

import duckdb

# Rows folded into one hash before Python sees it.
BUCKET_ROWS = 100_000

# DuckDB writes these control characters as `\u00XX` with upper-case hex where
# Python's json writes lower case (the others it writes the same way, or as
# `\n`, `\t`, ...). A value that holds one is written again with lower case.
_UPPER_HEX_CONTROLS = (0x0B, 0x0E, 0x0F, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F)
_HAS_UPPER_HEX_CONTROL = "[" + "".join(f"\\x{code:02x}" for code in _UPPER_HEX_CONTROLS) + "]"


class UnsupportedColumn(TypeError):
    pass


def _json_text(expression: str) -> str:
    """A JSON value of a VARCHAR or VARCHAR[] expression, as Python's json writes it."""
    written = f"to_json({expression})"
    fixed = written
    for code in _UPPER_HEX_CONTROLS:
        fixed = f"replace({fixed}, '\\u{code:04X}', '\\u{code:04x}')"
    return (
        f"CASE WHEN {expression} IS NULL THEN 'null'"
        f" WHEN regexp_matches(CAST({expression} AS VARCHAR), '{_HAS_UPPER_HEX_CONTROL}')"
        f" THEN {fixed} ELSE {written} END"
    )


def _value(column: str, sql_type: str) -> str:
    """SQL that writes one column's value as canonical JSON."""
    quoted = f'"{column}"'
    upper = sql_type.upper()
    if upper in ("VARCHAR", "VARCHAR[]"):
        return _json_text(quoted)
    if upper == "JSON":
        # Read back as a string, and written as one.
        return _json_text(f"CAST({quoted} AS VARCHAR)")
    if upper == "BOOLEAN":
        return f"CASE WHEN {quoted} IS NULL THEN 'null' WHEN {quoted} THEN 'true' ELSE 'false' END"
    if upper in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "DOUBLE"):
        # A DOUBLE is written as its shortest round-trip form, as Python's repr.
        return f"coalesce(CAST({quoted} AS VARCHAR), 'null')"
    if upper.startswith("DECIMAL"):
        return (
            f"coalesce('\"' || CAST(CAST({quoted} AS DECIMAL(38,4)) AS VARCHAR) || '\"', 'null')"
        )
    if upper == "DATE":
        return f"coalesce('\"' || CAST({quoted} AS VARCHAR) || '\"', 'null')"
    raise UnsupportedColumn(f"Column {column} has type {sql_type}, which no digest writes.")


def _row_text(con: duckdb.DuckDBPyConnection, table: str) -> str:
    columns = con.execute(f"DESCRIBE {table}").fetchall()
    values = ", ".join(_value(name, sql_type) for name, sql_type, *_ in columns)
    return f"'[' || concat_ws(',', {values}) || ']'"


def row_texts(
    con: duckdb.DuckDBPyConnection, table: str, key: str, snapshot_set_id: str
) -> list[str]:
    """Each row of the set as the digest writes it, in key order. For tests."""
    return [
        text
        for (text,) in con.execute(
            f"SELECT {_row_text(con, table)} FROM {table} WHERE snapshot_set_id = ?"
            f" ORDER BY {key}",
            [snapshot_set_id],
        ).fetchall()
    ]


def fold_tables(
    con: duckdb.DuckDBPyConnection,
    tables: Iterable[tuple[str, str]],
    snapshot_set_id: str,
    prefix: str | None = None,
) -> str:
    """SHA-256 over `prefix`, then each table's name and its rows in key order.

    `tables` is (table, key) pairs. The rows are hashed and folded in buckets of
    `BUCKET_ROWS` in the database.
    """
    digest = hashlib.sha256()
    if prefix is not None:
        digest.update(prefix.encode("ascii") + b"\n")
    for table, key in tables:
        digest.update(table.encode("ascii") + b"\n")
        buckets = con.execute(
            f"""
            WITH rows AS (
                SELECT sha256({_row_text(con, table)}) AS h,
                       row_number() OVER (ORDER BY {key}) - 1 AS n
                FROM {table} WHERE snapshot_set_id = ?
            )
            SELECT sha256(string_agg(h, chr(10) ORDER BY n)) FROM rows
            GROUP BY n // {BUCKET_ROWS} ORDER BY n // {BUCKET_ROWS}
            """,
            [snapshot_set_id],
        ).fetchall()
        for (bucket,) in buckets:
            digest.update(bucket.encode("ascii") + b"\n")
    return digest.hexdigest()
