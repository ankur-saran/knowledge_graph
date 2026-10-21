"""Loads published files (GLEIF, OpenSanctions) into Bronze, in SQL.

A refresh directory holds the files `ubo download` fetched and its manifest.
Each dataset declares the columns it reads from its file; `_raw` is that
projection of the record, written in SQL exactly as `canonical_json` writes
it, so `_row_hash = sha256(canonical_json(_raw))` as for fixtures and can be
recomputed from the database. A value the record does not state (an empty
cell, an absent or null property) is left out of `_raw`.

Real feeds are not strict. A record that cannot be read, has no key, holds a
value of the wrong type or repeats a key goes to `bronze_rejects` and is
counted; above the reject limit the ingest is refused, because the format has
probably changed. A file whose bytes are not the manifest's, or that lacks a
declared column, is refused outright.

Every file is read and checked before anything is written, outside any
transaction (DuckDB keeps what a transaction creates in its own storage, and
large work over it is slow). One transaction then writes every dataset and
the snapshot set, or nothing.
"""

import csv
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import duckdb

from ubo_sentinel.models.manifest import Manifest, ManifestFile, load_manifest
from ubo_sentinel.pipeline.bronze import (
    ALIAS_LATEST,
    ALIAS_REAL,
    BRONZE_COLUMNS,
    FAMILY_REAL,
    FAMILY_SAMPLE,
    IngestError,
    SnapshotManager,
    create_schema,
    file_sha256,
    snapshot_id_for,
    transaction,
)
from ubo_sentinel.pipeline.digest import json_text
from ubo_sentinel.pipeline.loaders.base import DatasetSummary, IngestSummary, resolve_alias
from ubo_sentinel.pipeline.sources import SOURCE_CONFIDENCE

# Rejected records, with the reason they were not loaded.
# The reader could not parse it: a broken CSV row, a line that is not a JSON object.
UNREADABLE = "UNREADABLE"
BAD_VALUE = "BAD_VALUE"  # a declared value of the wrong JSON type
MISSING_KEY = "MISSING_KEY"
DUPLICATE_KEY = "DUPLICATE_KEY"  # the first record with a key is loaded, later ones are rejected

# Share of a dataset's records that may be rejected before the ingest is refused.
REJECT_LIMIT_ENV_VAR = "UBO_REJECT_LIMIT"
DEFAULT_REJECT_LIMIT = 0.01

# Longest record read; an OpenSanctions entity with many names runs to some 100 kB.
MAX_RECORD_BYTES = 32 * 1024 * 1024

# Parts of a `_source_record_id` built from several values.
KEY_SEPARATOR = "|"


class Format(StrEnum):
    CSV = "csv"  # with a header row
    NDJSON = "ndjson"  # one JSON object per line


class Kind(StrEnum):
    TEXT = "text"
    TEXT_LIST = "text[]"
    BOOL = "bool"


@dataclass(frozen=True)
class Column:
    """A value a dataset reads: its key in `_raw` and where it is in the record.

    CSV: `key` is the header name. NDJSON: `key` is the property of the record,
    or of the object `within` names (`within="properties"` for FtM properties).
    """

    key: str
    within: str | None = None
    kind: Kind = Kind.TEXT


@dataclass(frozen=True)
class RealDataset:
    name: str
    table: str
    source: str
    # The manifest's key for the file this dataset is read from; several may share one.
    file: str
    format: Format
    columns: tuple[Column, ...]
    # Top-level text columns whose values, joined by "|", are `_source_record_id`.
    key: tuple[str, ...]
    # (top-level text column, values): the records of the file this dataset holds.
    # The others belong to another dataset or to none, and are not rejects.
    select: tuple[str, frozenset[str]] | None = None

    def __post_init__(self) -> None:
        keys = [(c.within, c.key) for c in self.columns]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{self.name}: a column is declared twice")
        top_text = {c.key for c in self.columns if c.within is None and c.kind == Kind.TEXT}
        nested = {c.within for c in self.columns if c.within is not None}
        if nested & {c.key for c in self.columns if c.within is None}:
            raise ValueError(f"{self.name}: a key is both a value and an object")
        if not self.key or not set(self.key) <= top_text:
            raise ValueError(f"{self.name}: the key must be declared top-level text columns")
        if self.select is not None and self.select[0] not in top_text:
            raise ValueError(f"{self.name}: the select column must be a declared top-level text")
        if self.format == Format.CSV and any(
            c.within is not None or c.kind != Kind.TEXT for c in self.columns
        ):
            raise ValueError(f"{self.name}: a CSV column is top-level text")


@dataclass(frozen=True)
class _Staged:
    dataset: RealDataset
    entry: ManifestFile
    # Temp table: _row_num, _source_record_id, raw, reason, reject_raw. None when
    # the snapshot is in Bronze already, so its file was checked but not read.
    table: str | None
    rows: int
    rejects: int


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _json_key(key: str) -> str:
    """A SQL literal holding `"key":` as canonical JSON writes it."""
    return _quote(json.dumps(key, ensure_ascii=False) + ":")


def reject_limit() -> float:
    value = os.environ.get(REJECT_LIMIT_ENV_VAR)
    return DEFAULT_REJECT_LIMIT if value is None else float(value)


# --- reading files ----------------------------------------------------------------------


def _check_file(refresh_dir: Path, key: str, entry: ManifestFile) -> Path:
    path = refresh_dir / entry.path
    if not path.is_file():
        raise IngestError(f"{path}: missing; the manifest lists it as '{key}'.")
    if path.stat().st_size != entry.size or file_sha256(path) != entry.sha256:
        raise IngestError(f"{path}: its bytes are not the ones the manifest records.")
    return path


def _csv_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), None)
    if not header:
        raise IngestError(f"{path}: has no header row.")
    repeated = sorted({name for name in header if header.count(name) > 1})
    if repeated:
        raise IngestError(f"{path}: the header repeats {repeated}.")
    return header


def _number_records(
    con: duckdb.DuckDBPyConnection, read: str, rejects: str, first_line: int, into: str
) -> None:
    """Give each record the reader kept its number in the file.

    The reader skips the records it rejects and reports their place in the
    file; the k-th record it keeps is record k + (rejected records before it).
    `rowid` is the order the records were read in, which is file order.
    """
    gaps = f"{into}_gaps"
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {gaps} AS SELECT line - {first_line} + 1 - row_number()"
        f" OVER (ORDER BY line) AS kept_before FROM (SELECT DISTINCT line FROM {rejects})"
    )
    con.execute(
        f"CREATE TEMP TABLE {into} AS SELECT k + (SELECT count(*) FROM {gaps} g"
        f" WHERE g.kept_before < r.k) AS _row_num, r.* EXCLUDE (k)"
        f" FROM (SELECT rowid + 1 AS k, * FROM {read}) r"
    )


def _read_file(
    con: duckdb.DuckDBPyConnection,
    n: int,
    path: Path,
    fmt: Format,
    columns: Sequence[str],
) -> tuple[str, str]:
    """Read a file into a temp table: `_row_num` and the declared columns (CSV) or `line` (NDJSON).

    Returns that table and a table of the records the reader rejected
    (`_row_num`, `reason`, `reject_raw`).
    """
    staged, rejects, scans = f"stg_real_file_{n}", f"stg_real_rejects_{n}", f"stg_real_scans_{n}"
    for table in (staged, rejects, scans, f"{staged}_read"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    common = (
        f"auto_detect=false, store_rejects=true, rejects_table={_quote(rejects)},"
        f" rejects_scan={_quote(scans)}, max_line_size={MAX_RECORD_BYTES},"
        f" buffer_size={2 * MAX_RECORD_BYTES}"
    )
    if fmt == Format.CSV:
        header = _csv_header(path)
        missing = [name for name in columns if name not in header]
        if missing:
            raise IngestError(f"{path}: the header lacks {missing}.")
        types = ", ".join(f"{_quote(name)}: 'VARCHAR'" for name in header)
        selected = ", ".join(
            f"nullif({_ident(name)}, '') AS c{i}" for i, name in enumerate(columns)
        )
        source = f"read_csv({_quote(str(path))}, header=true, columns={{{types}}}, {common})"
        first_line = 2
    else:
        # One column per line: no JSON line holds a raw control character, so the
        # delimiter never splits a valid one, and there is no quoting.
        selected = "line"
        source = (
            f"read_csv({_quote(str(path))}, header=false, columns={{'line': 'VARCHAR'}},"
            f" delim=E'\\x01', quote='', escape='', {common})"
        )
        first_line = 1
    con.execute(f"CREATE TEMP TABLE {staged}_read AS SELECT {selected} FROM {source}")
    _number_records(con, f"{staged}_read", rejects, first_line, staged)
    con.execute(f"DROP TABLE {staged}_read")
    unread = f"stg_real_unread_{n}"
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {unread} AS SELECT line - {first_line} + 1 AS _row_num,"
        f" '{UNREADABLE}: ' || string_agg(DISTINCT error_type, ', ' ORDER BY error_type) AS reason,"
        f" any_value(csv_line) AS reject_raw FROM {rejects} GROUP BY line"
    )
    return staged, unread


# --- one dataset ------------------------------------------------------------------------


def _ndjson_path(column: Column) -> str:
    parts = [column.key] if column.within is None else [column.within, column.key]
    return _quote("$." + ".".join(json.dumps(part) for part in parts))


def _value_sql(column: Column, fmt: Format, alias: dict[str, str]) -> tuple[str, str, str]:
    """(present, of the declared type, JSON text) of one column, in SQL."""
    if fmt == Format.CSV:
        value = alias[column.key]
        return f"{value} IS NOT NULL", "true", json_text(value)
    path = _ndjson_path(column)
    json_type = f"json_type(j, {path})"
    present = f"coalesce({json_type} <> 'NULL', false)"
    if column.kind == Kind.TEXT:
        return present, f"{json_type} = 'VARCHAR'", json_text(f"json_extract_string(j, {path})")
    if column.kind == Kind.BOOL:
        value = f"CAST(json_extract(j, {path}) AS BOOLEAN)"
        return (
            present,
            f"{json_type} = 'BOOLEAN'",
            f"CASE WHEN {value} THEN 'true' ELSE 'false' END",
        )
    items = f"json_type(json_extract(j, {path}), '$[*]')"
    typed = (
        f"({json_type} = 'ARRAY' AND coalesce(list_bool_and(list_transform({items},"
        " t -> t = 'VARCHAR')), true))"
    )
    return present, typed, json_text(f"CAST(json_extract(j, {path}) AS VARCHAR[])")


def _object_sql(members: list[tuple[str, str, str]]) -> str:
    """`{...}` of the members present, in key order. Members are (key, present, text)."""
    parts = ", ".join(
        f"CASE WHEN {present} THEN {_json_key(key)} || {text} END"
        for key, present, text in sorted(members)
    )
    return f"'{{' || concat_ws(',', {parts}) || '}}'"


def _raw_sql(dataset: RealDataset, alias: dict[str, str]) -> tuple[str, str]:
    """(the record's `_raw` as canonical JSON, true when every value present has its type)."""
    values = {}
    for c in dataset.columns:
        present, typed, text = _value_sql(c, dataset.format, alias)
        # Written only when present and of its type, so no cast sees a value of another type.
        values[(c.within, c.key)] = (
            f"({present}) AND ({typed})",
            f"(NOT ({present}) OR ({typed}))",
            text,
        )
    top: list[tuple[str, str, str]] = []
    for within in sorted({c.within for c in dataset.columns if c.within is not None}):
        inner = [(key, p, t) for (w, key), (p, _, t) in values.items() if w == within]
        any_present = " OR ".join(f"({p})" for _, p, _ in inner)
        top.append((within, any_present, _object_sql(inner)))
    top += [(key, p, t) for (w, key), (p, _, t) in values.items() if w is None]
    typed = " AND ".join(ok for _, ok, _ in values.values())
    return _object_sql(top), typed


def _stage_dataset(
    con: duckdb.DuckDBPyConnection,
    n: int,
    dataset: RealDataset,
    file_table: str,
    unread: str,
    columns: Sequence[str],
) -> str:
    """A temp table of the dataset's records: `_row_num`, `_source_record_id`, `raw`,
    `reason` (null for a record that is loaded) and `reject_raw`."""
    if dataset.format == Format.CSV:
        alias = {name: f"c{i}" for i, name in enumerate(columns)}
        text = {c: alias[c] for c in {*dataset.key, *(dataset.select or ())[:1]}}
        # `j` is not null when the record can be read; the reader dropped those it could not.
        readable = f"SELECT *, NULL AS line, true AS j FROM {file_table}"
    else:
        alias = {}
        text = {
            c: f"nullif(json_extract_string(j, {_ndjson_path(Column(c))}), '')"
            for c in {*dataset.key, *(dataset.select or ())[:1]}
        }
        # Nested so that no JSON function sees a line that is not JSON.
        readable = (
            "SELECT _row_num, line, CASE WHEN json_valid(line) THEN"
            " CASE WHEN json_type(line) = 'OBJECT' THEN line END END AS j"
            f" FROM {file_table} WHERE line IS NOT NULL"
        )
    if dataset.select is None:
        selected = "true"
    else:
        column, values = dataset.select
        selected = f"{text[column]} IN ({', '.join(_quote(v) for v in sorted(values))})"
    raw, typed = _raw_sql(dataset, alias)
    key_parts = [text[c] for c in dataset.key]
    has_key = " AND ".join(f"{part} IS NOT NULL" for part in key_parts)
    record_id = f"concat_ws({_quote(KEY_SEPARATOR)}, {', '.join(key_parts)})"

    table = f"stg_real_dataset_{n}"
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {table} AS
        WITH readable AS ({readable}),
        records AS (
            SELECT _row_num, line,
                CASE WHEN j IS NOT NULL THEN CASE WHEN {typed} THEN {raw} END END AS raw,
                CASE WHEN j IS NOT NULL THEN CASE WHEN {has_key} THEN {record_id} END END
                    AS _source_record_id,
                CASE
                    WHEN j IS NULL THEN '{UNREADABLE}: not a JSON object'
                    WHEN NOT ({typed}) THEN '{BAD_VALUE}'
                    WHEN NOT ({has_key}) THEN '{MISSING_KEY}'
                END AS reason
            FROM readable
            WHERE j IS NULL OR coalesce({selected}, false)
        ), ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY _source_record_id, reason IS NULL ORDER BY _row_num
            ) AS nth
            FROM records
        )
        SELECT _row_num, _source_record_id, raw,
            CASE WHEN reason IS NULL AND nth > 1 THEN '{DUPLICATE_KEY}' ELSE reason END AS reason,
            coalesce(line, raw) AS reject_raw
        FROM ranked
        UNION ALL
        SELECT _row_num, NULL, NULL, reason, reject_raw FROM {unread}
        """
    )
    return table


# --- the ingest -------------------------------------------------------------------------


@contextmanager
def _scratch(con: duckdb.DuckDBPyConnection) -> Iterator[None]:
    """Drop the temp tables of a load, whatever happens."""
    try:
        yield
    finally:
        for (name,) in con.execute(
            "SELECT table_name FROM duckdb_tables()"
            " WHERE temporary AND table_name LIKE 'stg_real_%'"
        ).fetchall():
            con.execute(f"DROP TABLE IF EXISTS {name}")


def _stage(
    con: duckdb.DuckDBPyConnection,
    refresh_dir: Path,
    manifest: Manifest,
    datasets: Sequence[RealDataset],
    limit: float,
) -> list[_Staged]:
    by_file: dict[str, list[RealDataset]] = {}
    for dataset in datasets:
        if dataset.file not in manifest.files:
            raise IngestError(f"The manifest has no file '{dataset.file}' ({dataset.name}).")
        by_file.setdefault(dataset.file, []).append(dataset)

    staged: list[_Staged] = []
    for n, (key, members) in enumerate(sorted(by_file.items())):
        entry = manifest.files[key]
        formats = {d.format for d in members}
        if len(formats) != 1:
            raise IngestError(f"Datasets read '{key}' in more than one format.")
        path = _check_file(refresh_dir, key, entry)
        # A refresh often brings one source's new files and the other's unchanged
        # ones; a snapshot already in Bronze is not read again.
        present = _present(con, members, entry.sha256)
        staged += [_Staged(d, entry, None, 0, 0) for d in members if d.name in present]
        members = [d for d in members if d.name not in present]
        if not members:
            continue
        columns = sorted({c.key for d in members for c in d.columns})
        file_table, unread = _read_file(con, n, path, formats.pop(), columns)
        for dataset in members:
            table = _stage_dataset(con, len(staged), dataset, file_table, unread, columns)
            rows, rejects = con.execute(
                f"SELECT count(*) FILTER (WHERE reason IS NULL),"
                f" count(*) FILTER (WHERE reason IS NOT NULL) FROM {table}"
            ).fetchone()
            # An empty dataset in a publisher's file means the file is broken; a
            # small sample may simply hold none.
            if rows == 0 and manifest.family != FAMILY_SAMPLE:
                raise IngestError(f"{path}: holds no {dataset.name} records.")
            if rejects > limit * (rows + rejects):
                reasons = con.execute(
                    f"SELECT reason, count(*) FROM {table} WHERE reason IS NOT NULL"
                    " GROUP BY reason ORDER BY count(*) DESC, reason LIMIT 5"
                ).fetchall()
                raise IngestError(
                    f"{path}: {rejects} of {rows + rejects} {dataset.name} records cannot be"
                    f" loaded (limit {limit:.2%}; {dict(reasons)}). Has the format changed?"
                )
            staged.append(_Staged(dataset, entry, table, rows, rejects))
    return staged


def _present(
    con: duckdb.DuckDBPyConnection, datasets: Sequence[RealDataset], content_sha256: str
) -> set[str]:
    """The datasets whose snapshot of this file is in Bronze already."""
    create_schema(con)
    ids = {snapshot_id_for(d.name, content_sha256): d.name for d in datasets}
    found = con.execute(
        "SELECT snapshot_id FROM bronze_snapshots WHERE list_contains(?, snapshot_id)",
        [list(ids)],
    ).fetchall()
    return {ids[snapshot_id] for (snapshot_id,) in found}


def ingest_real(
    con: duckdb.DuckDBPyConnection,
    refresh_dir: Path,
    datasets: Sequence[RealDataset],
    required: Sequence[str],
    base: str | None = None,
    source: str = FAMILY_REAL,
) -> IngestSummary:
    """Ingest `datasets` from a refresh directory; datasets of `required` it lacks come from `base`.

    The set is of the manifest's family: `real`, or `sample` for a directory
    `ubo sample` cut. Ingesting files that are already present changes nothing.
    """
    if not refresh_dir.is_dir():
        raise IngestError(f"{refresh_dir} is not a directory.")
    try:
        manifest = load_manifest(refresh_dir)
    except (OSError, ValueError) as exc:
        raise IngestError(f"{refresh_dir}: no readable manifest ({exc}).") from exc

    manager = SnapshotManager(con)
    status: dict[str, str] = {}
    supplied: dict[str, str] = {}
    with _scratch(con):
        staged = _stage(con, refresh_dir, manifest, datasets, reject_limit())
        with transaction(con):
            for item in staged:
                dataset, entry = item.dataset, item.entry
                snapshot = manager.open_snapshot(
                    dataset.name,
                    manifest.family,
                    dataset.table,
                    refresh_dir / entry.path,
                    content_sha256=entry.sha256,
                )
                if snapshot.is_new:
                    if item.table is None:
                        raise IngestError(f"Snapshot {snapshot.snapshot_id} vanished; try again.")
                    _write(con, snapshot.snapshot_id, item)
                    manager.register_snapshot(
                        snapshot,
                        published_at=entry.published_at.date(),
                        reject_count=item.rejects,
                        source_meta={
                            "file": dataset.file,
                            "refresh_id": manifest.refresh_id,
                            **entry.model_dump(mode="json"),
                        },
                    )
                supplied[dataset.name] = snapshot.snapshot_id
                status[dataset.name] = "new" if snapshot.is_new else "already present"
            snapshot_set = manager.commit_set(
                manifest.family, supplied, required=required, base=base
            )

    info = {
        snapshot_id: (table, rows, rejects)
        for snapshot_id, table, rows, rejects in con.execute(
            "SELECT snapshot_id, table_name, row_count, reject_count FROM bronze_snapshots"
        ).fetchall()
    }
    summaries = []
    for name in sorted(required):
        snapshot_id = snapshot_set.snapshot_ids[name]
        table, rows, rejects = info[snapshot_id]
        summaries.append(
            DatasetSummary(
                dataset=name,
                table=table,
                snapshot_id=snapshot_id,
                row_count=rows,
                status=status.get(name, "inherited"),
                reject_count=rejects,
            )
        )
    return IngestSummary(
        source=source,
        path=refresh_dir,
        datasets=summaries,
        snapshot_set=snapshot_set,
        aliases={alias: resolve_alias(con, alias) for alias in (ALIAS_LATEST, ALIAS_REAL)},
    )


def _write(con: duckdb.DuckDBPyConnection, snapshot_id: str, item: _Staged) -> None:
    dataset = item.dataset
    confidence = SOURCE_CONFIDENCE[dataset.source]
    con.execute(
        f"INSERT INTO {dataset.table} ({', '.join(BRONZE_COLUMNS)})"
        f" SELECT ?, ?, _source_record_id, ?, ?, _row_num, sha256(raw), CAST(raw AS JSON)"
        f" FROM {item.table} WHERE reason IS NULL ORDER BY _row_num",
        [snapshot_id, dataset.source, item.entry.published_at.date(), confidence],
    )
    con.execute(
        "INSERT INTO bronze_rejects (snapshot_id, row_num, reason, raw)"
        f" SELECT ?, _row_num, reason, coalesce(reject_raw, '') FROM {item.table}"
        " WHERE reason IS NOT NULL ORDER BY _row_num",
        [snapshot_id],
    )
