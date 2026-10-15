"""Digests computed in the database write each row as canonical JSON would."""

from datetime import date
from decimal import Decimal

import duckdb
import pytest

from ubo_sentinel.models import canonical_json
from ubo_sentinel.pipeline import digest, gold, silver
from ubo_sentinel.pipeline.digest import UnsupportedColumn, fold_tables, row_texts


def python_texts(con, table, key, set_id):
    rows = con.execute(
        f"SELECT * FROM {table} WHERE snapshot_set_id = ? ORDER BY {key}", [set_id]
    ).fetchall()
    return [canonical_json(list(row)).decode("utf-8") for row in rows]


def test_every_silver_and_gold_row_is_written_as_canonical_json(con, graph_t0):
    written = 0
    for tables in (silver._TABLES, gold._TABLES):
        for table, (_, key) in tables.items():
            texts = row_texts(con, table, key, graph_t0)
            assert texts == python_texts(con, table, key, graph_t0), table
            written += len(texts)
    # Every table but the rejects holds rows on t0.
    assert written > 1000


@pytest.fixture
def awkward():
    """One table with every column type a digest writes, and awkward values."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (snapshot_set_id TEXT, k INTEGER, s TEXT, l TEXT[], j JSON, b BOOLEAN,"
        " d DOUBLE, p DECIMAL(7,4), w DECIMAL(18,4), day DATE)"
    )
    rows = [
        ("x", 1, 'quote " slash \\ tab \t nl \n', ["a", "b c"], '{"a":1}', True, 1.0,
         Decimal("50"), Decimal("110.5"), date(2026, 1, 2)),
        ("x", 2, "ctl \x01 \x0b \x1f \x7f é € 😀 \u2028", [], "[]", False, 1e-05,
         Decimal("0.0001"), Decimal("-3"), None),
        ("x", 3, None, None, None, None, None, None, None, None),
        ("x", 4, "", ["\x1a"], '"text"', True, 0.9333333333333333, Decimal("99.9999"),
         Decimal("0"), date(1999, 12, 31)),
        # The six characters of an escape, not the control character it names.
        ("x", 5, "literal \\u001F text", ["x"], "{}", False, 1e16, Decimal("12.34"),
         Decimal("1"), date(2026, 9, 15)),
    ]  # fmt: skip
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    yield con
    con.close()


def test_awkward_values_are_written_as_canonical_json(awkward):
    assert row_texts(awkward, "t", "k", "x") == python_texts(awkward, "t", "k", "x")


def test_the_digest_follows_the_rows_not_their_order_of_insertion(awkward):
    before = fold_tables(awkward, [("t", "k")], "x")
    rows = awkward.execute("SELECT * FROM t ORDER BY k DESC").fetchall()
    awkward.execute("DELETE FROM t")
    awkward.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    assert fold_tables(awkward, [("t", "k")], "x") == before
    awkward.execute("UPDATE t SET d = 0.5 WHERE k = 3")
    assert fold_tables(awkward, [("t", "k")], "x") != before
    # Only the set asked for is read.
    awkward.execute("INSERT INTO t (snapshot_set_id, k) VALUES ('y', 9)")
    awkward.execute("UPDATE t SET d = NULL WHERE k = 3")
    assert fold_tables(awkward, [("t", "k")], "x") == before


def test_rows_are_folded_in_buckets_in_key_order(awkward, monkeypatch):
    whole = fold_tables(awkward, [("t", "k")], "x")
    monkeypatch.setattr(digest, "BUCKET_ROWS", 2)
    small = fold_tables(awkward, [("t", "k")], "x")
    assert small != whole
    rows = awkward.execute("SELECT * FROM t ORDER BY k DESC").fetchall()
    awkward.execute("DELETE FROM t")
    awkward.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    assert fold_tables(awkward, [("t", "k")], "x") == small


def test_a_column_type_no_digest_writes_is_refused():
    con = duckdb.connect()
    con.execute("CREATE TABLE t (snapshot_set_id TEXT, k INTEGER, seen TIMESTAMP)")
    with pytest.raises(UnsupportedColumn, match="TIMESTAMP"):
        fold_tables(con, [("t", "k")], "x")
