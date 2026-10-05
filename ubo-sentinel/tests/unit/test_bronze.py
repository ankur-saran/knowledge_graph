"""Bronze: schema, content-derived ids, idempotent and all-or-nothing ingest."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ubo_sentinel.cli.app import app
from ubo_sentinel.models import read_rows, sha256_hex
from ubo_sentinel.pipeline.bronze import (
    BRONZE_COLUMNS,
    BRONZE_TABLES,
    IngestError,
    UnknownSnapshotSet,
    resolve_set,
    rows_digest,
)
from ubo_sentinel.pipeline.db import DB_ENV_VAR, connect, db_path
from ubo_sentinel.pipeline.loaders.fixture_loader import DATASETS, ingest_fixtures

# Commands are run from the repository root.
T0 = Path("fixtures/snapshot_t0")
T1 = Path("fixtures/snapshot_t1")

runner = CliRunner()


def count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def assert_nothing_written(con):
    """A refused ingest leaves no rows; one refused before any write leaves no tables."""
    for (table,) in con.execute("SHOW TABLES").fetchall():
        assert count(con, table) == 0, table


def bronze_rows(con, table):
    return con.execute(f"SELECT * FROM {table} ORDER BY _snapshot_id, _row_num").fetchall()


# --- schema and content -------------------------------------------------------------


def test_database_path_comes_from_the_environment(database):
    assert db_path() == database


def test_every_bronze_table_has_exactly_the_metadata_columns(con):
    ingest_fixtures(con)
    for table in BRONZE_TABLES:
        columns = [row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()]
        assert columns == list(BRONZE_COLUMNS), table


def test_row_counts_equal_the_files(con):
    summary = ingest_fixtures(con)
    assert [d.dataset for d in summary.datasets] == [d.name for d in DATASETS.values()]
    for name, dataset in DATASETS.items():
        expected = len(read_rows(T0 / name, dataset.model))
        assert expected > 0
        assert count(con, dataset.table) == expected, dataset.table
    assert {d.status for d in summary.datasets} == {"new"}
    assert summary.snapshot_set.is_new and summary.snapshot_set.is_complete


def test_raw_round_trips_to_the_fixture_row_and_carries_its_provenance(con):
    ingest_fixtures(con)
    for name, dataset in DATASETS.items():
        expected = read_rows(T0 / name, dataset.model)
        stored = bronze_rows(con, dataset.table)
        assert len(stored) == len(expected)
        for row_num, (row, record) in enumerate(zip(expected, stored, strict=True), start=1):
            _, source, source_record_id, as_of, confidence, stored_num, row_hash, raw = record
            cells = json.loads(raw)
            assert dataset.model.model_validate(cells) == row
            assert (source, source_record_id, as_of, confidence, stored_num) == (
                row.source,
                row.source_record_id,
                row.as_of,
                row.confidence,
                row_num,
            )
            assert row_hash == sha256_hex(cells)


def test_registry_describes_each_snapshot(con):
    summary = ingest_fixtures(con)
    for name, dataset in DATASETS.items():
        snapshot_id = summary.snapshot_set.snapshot_ids[dataset.name]
        row = con.execute(
            "SELECT dataset, family, table_name, file_name, byte_size, row_count, rows_digest,"
            " as_of_min, as_of_max FROM bronze_snapshots WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone()
        rows = read_rows(T0 / name, dataset.model)
        assert row == (
            dataset.name,
            "fixtures",
            dataset.table,
            name,
            (T0 / name).stat().st_size,
            len(rows),
            rows_digest(con, dataset.table, snapshot_id),
            min(r.as_of for r in rows),
            max(r.as_of for r in rows),
        )


# --- ids and idempotency --------------------------------------------------------------


def test_second_ingest_changes_nothing(con):
    first = ingest_fixtures(con)
    before = {table: bronze_rows(con, table) for table in BRONZE_TABLES}
    registry = con.execute("SELECT * FROM bronze_snapshots ORDER BY snapshot_id").fetchall()

    second = ingest_fixtures(con)
    assert second.snapshot_set.snapshot_ids == first.snapshot_set.snapshot_ids
    assert second.snapshot_set.snapshot_set_id == first.snapshot_set.snapshot_set_id
    assert second.snapshot_set.seq == first.snapshot_set.seq
    assert not second.snapshot_set.is_new
    assert {d.status for d in second.datasets} == {"already present"}
    assert {table: bronze_rows(con, table) for table in BRONZE_TABLES} == before
    assert con.execute("SELECT * FROM bronze_snapshots ORDER BY snapshot_id").fetchall() == registry
    assert count(con, "snapshot_set_info") == 1


def test_ids_and_rows_depend_only_on_file_bytes(con, tmp_path, monkeypatch):
    first = ingest_fixtures(con)
    rows = {table: bronze_rows(con, table) for table in BRONZE_TABLES}

    monkeypatch.setenv(DB_ENV_VAR, str(tmp_path / "other.duckdb"))
    other = connect()
    try:
        second = ingest_fixtures(other)
        assert second.snapshot_set.snapshot_ids == first.snapshot_set.snapshot_ids
        assert second.snapshot_set.snapshot_set_id == first.snapshot_set.snapshot_set_id
        assert {table: bronze_rows(other, table) for table in BRONZE_TABLES} == rows
    finally:
        other.close()


def test_a_changed_file_gives_a_new_id_for_that_dataset_only(con, t0_copy):
    first = ingest_fixtures(con)
    with (t0_copy / "repex.csv").open("a", encoding="utf-8", newline="") as f:
        f.write(
            "fx_registry,s00-repex-9,2026-09-01,1.0,fx_registry:s00-mid,DIRECT_PARENT,NON_PUBLIC\n"
        )

    second = ingest_fixtures(con, t0_copy)
    changed = {
        dataset
        for dataset, snapshot_id in second.snapshot_set.snapshot_ids.items()
        if snapshot_id != first.snapshot_set.snapshot_ids[dataset]
    }
    assert changed == {"repex"}
    assert second.snapshot_set.snapshot_set_id != first.snapshot_set.snapshot_set_id
    assert {d.dataset: d.status for d in second.datasets} == {
        "entities": "already present",
        "relationships": "already present",
        "sanctions": "already present",
        "repex": "new",
    }


# --- snapshot sets and aliases ----------------------------------------------------------


def test_a_changed_list_makes_a_new_set_that_reuses_the_other_datasets(con):
    t0 = ingest_fixtures(con)
    t0_rows = {
        table: bronze_rows(con, table) for table in BRONZE_TABLES if "sanctions" not in table
    }
    t0_sanctions = count(con, "bronze_sanctions")

    t1 = ingest_fixtures(con, T1)
    ids0, ids1 = t0.snapshot_set.snapshot_ids, t1.snapshot_set.snapshot_ids
    assert {d for d in ids1 if ids1[d] != ids0[d]} == {"sanctions"}
    assert t1.snapshot_set.snapshot_set_id != t0.snapshot_set.snapshot_set_id
    assert not t1.snapshot_set.is_complete
    assert t1.snapshot_set.base_set_id == t0.snapshot_set.snapshot_set_id
    assert {d.dataset: d.status for d in t1.datasets} == {
        "entities": "inherited",
        "relationships": "inherited",
        "sanctions": "new",
        "repex": "inherited",
    }

    # The changed list moves `latest`; `fixtures` stays on the complete t0 set.
    assert t1.aliases == {
        "fixtures": t0.snapshot_set.snapshot_set_id,
        "latest": t1.snapshot_set.snapshot_set_id,
    }
    assert resolve_set(con, "fixtures") == resolve_set(con, t0.snapshot_set.snapshot_set_id)
    assert resolve_set(con, "latest").snapshot_ids == ids1

    # Snapshots are immutable: the t0 rows are still there, unchanged.
    assert {table: bronze_rows(con, table) for table in t0_rows} == t0_rows
    assert count(con, "bronze_sanctions") == t0_sanctions + len(
        read_rows(T1 / "sanctions.csv", DATASETS["sanctions.csv"].model)
    )

    again = ingest_fixtures(con, T1)
    assert again.snapshot_set.snapshot_set_id == t1.snapshot_set.snapshot_set_id
    assert not again.snapshot_set.is_new


def test_base_names_the_set_to_inherit_from(con, t0_copy):
    t0 = ingest_fixtures(con)
    with (t0_copy / "repex.csv").open("a", encoding="utf-8", newline="") as f:
        f.write(
            "fx_registry,s00-repex-9,2026-09-01,1.0,fx_registry:s00-mid,DIRECT_PARENT,NON_PUBLIC\n"
        )
    newer = ingest_fixtures(con, t0_copy)

    t1 = ingest_fixtures(con, T1, base=t0.snapshot_set.snapshot_set_id)
    assert t1.snapshot_set.snapshot_ids["repex"] == t0.snapshot_set.snapshot_ids["repex"]
    assert t1.snapshot_set.snapshot_ids["repex"] != newer.snapshot_set.snapshot_ids["repex"]

    with pytest.raises(IngestError, match="No snapshot set"):
        ingest_fixtures(con, T1, base="0000000000000000")


def test_unknown_set_does_not_resolve(con):
    ingest_fixtures(con)
    with pytest.raises(UnknownSnapshotSet):
        resolve_set(con, "0000000000000000")


# --- all or nothing ---------------------------------------------------------------------


def test_a_partial_directory_needs_an_earlier_set(con):
    with pytest.raises(IngestError, match="no earlier 'fixtures' snapshot set"):
        ingest_fixtures(con, T1)
    assert_nothing_written(con)


def test_an_unknown_csv_is_refused(con, t0_copy):
    (t0_copy / "sanction.csv").write_bytes((T0 / "sanctions.csv").read_bytes())
    with pytest.raises(IngestError, match="unknown files"):
        ingest_fixtures(con, t0_copy)


def test_a_directory_without_datasets_is_refused(con, tmp_path):
    with pytest.raises(IngestError, match="holds none"):
        ingest_fixtures(con, tmp_path)
    with pytest.raises(IngestError, match="not a directory"):
        ingest_fixtures(con, tmp_path / "missing")


def test_a_malformed_row_is_reported_with_its_line_and_nothing_is_written(con, t0_copy):
    path = t0_copy / "sanctions.csv"
    lines = len(path.read_bytes().splitlines())
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write("fx_list,s99-des,not-a-date,1.0,fx_list:s02-listed,P,2020-01-01,L,true\n")

    with pytest.raises(IngestError, match=rf"sanctions\.csv:{lines + 1}: "):
        ingest_fixtures(con, t0_copy)
    assert_nothing_written(con)


def test_a_duplicate_record_key_is_refused(con, t0_copy):
    path = t0_copy / "repex.csv"
    last = path.read_bytes().splitlines()[-1]
    path.write_bytes(path.read_bytes() + last + b"\n")
    with pytest.raises(IngestError, match="duplicate record keys"):
        ingest_fixtures(con, t0_copy)
    assert_nothing_written(con)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data.replace(b"\n", b"\r\n"), "CR line endings"),
        (lambda data: b"\xef\xbb\xbf" + data, "BOM"),
    ],
)
def test_platform_dependent_bytes_are_refused(con, t0_copy, change, message):
    path = t0_copy / "entities.csv"
    path.write_bytes(change(path.read_bytes()))
    with pytest.raises(IngestError, match=message):
        ingest_fixtures(con, t0_copy)
    assert_nothing_written(con)


# --- command ------------------------------------------------------------------------------


def test_ingest_command_prints_counts_and_ids_and_is_idempotent(con):
    con.close()
    first = runner.invoke(app, ["ingest", "--source", "fixtures"])
    assert first.exit_code == 0, first.output
    for dataset in DATASETS.values():
        assert dataset.table in first.output
    assert "Snapshot set:" in first.output and "latest" in first.output

    second = runner.invoke(app, ["ingest", "--source", "fixtures"])
    assert second.exit_code == 0, second.output
    assert second.output == first.output.replace("  new", "  already present").replace(
        "(new)", "(already present)"
    )


def test_ingest_command_reports_a_failure(con, t0_copy):
    con.close()
    (t0_copy / "extra.csv").write_text("a\n", encoding="utf-8")
    result = runner.invoke(app, ["ingest", "--source", "fixtures", "--path", str(t0_copy)])
    assert result.exit_code == 1
    assert "nothing was written" in result.output


def test_ingest_command_is_for_engineers(con):
    con.close()
    result = runner.invoke(app, ["ingest", "--source", "fixtures", "--role", "analyst"])
    assert result.exit_code == 1


def test_sources_of_later_steps_are_not_available_yet(con):
    con.close()
    result = runner.invoke(app, ["ingest", "--source", "gleif"])
    assert result.exit_code == 2
