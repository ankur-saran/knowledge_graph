"""Bronze: source records as published, one immutable snapshot per ingested file.

A snapshot is one dataset file; its id comes from the file's bytes. A snapshot
set names one snapshot per dataset; its id comes from its members. Nothing here
updates or deletes a row.
"""

import hashlib
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from ubo_sentinel.models.canonical import canonical_json, sha256_hex, short_id

BRONZE_TABLES = ("bronze_entities", "bronze_relationships", "bronze_sanctions", "bronze_repex")

# Every Bronze table has exactly these columns. `_raw` is the source record as
# published; `_source`, `_source_record_id`, `_snapshot_id`, `_as_of` and
# `_confidence` are the row's provenance.
BRONZE_COLUMNS = (
    "_snapshot_id",
    "_source",
    "_source_record_id",
    "_as_of",
    "_confidence",
    "_row_num",
    "_row_hash",
    "_raw",
)

ALIAS_LATEST = "latest"
ALIAS_FIXTURES = "fixtures"
FAMILY_FIXTURES = "fixtures"

_BRONZE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    _snapshot_id TEXT NOT NULL,
    _source TEXT NOT NULL,
    _source_record_id TEXT NOT NULL,
    _as_of DATE NOT NULL,
    _confidence DOUBLE NOT NULL,
    _row_num INTEGER NOT NULL,
    _row_hash TEXT NOT NULL,
    _raw JSON NOT NULL,
    PRIMARY KEY (_snapshot_id, _source, _source_record_id)
)
"""

# `ingested_at` is the only wall-clock value in Bronze. It is kept off the rows
# so that they depend on nothing but the file's bytes; no id or hash reads it.
_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS bronze_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    family TEXT NOT NULL,
    table_name TEXT NOT NULL,
    file_name TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    byte_size BIGINT NOT NULL,
    row_count BIGINT NOT NULL,
    rows_digest TEXT NOT NULL,
    as_of_min DATE,
    as_of_max DATE,
    published_at DATE,
    ingested_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_sets (
    snapshot_set_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    PRIMARY KEY (snapshot_set_id, dataset)
);
CREATE TABLE IF NOT EXISTS snapshot_set_info (
    snapshot_set_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    seq INTEGER NOT NULL UNIQUE,
    is_complete BOOLEAN NOT NULL,
    base_set_id TEXT
);
"""


class IngestError(ValueError):
    """The input cannot be ingested. Nothing was written."""


class UnknownSnapshotSet(LookupError):
    pass


@dataclass(frozen=True)
class SourceRecord:
    """One source record on its way into Bronze: its provenance and its raw form."""

    source: str
    source_record_id: str
    as_of: date
    confidence: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    dataset: str
    family: str
    table_name: str
    file_name: str
    content_sha256: str
    byte_size: int
    # False when this file's bytes were ingested before.
    is_new: bool


@dataclass(frozen=True)
class SnapshotSet:
    snapshot_set_id: str
    family: str
    # Order of creation. `latest` is the set with the highest value.
    seq: int
    # True when one ingest supplied every dataset; False when some were inherited.
    is_complete: bool
    base_set_id: str | None
    # Dataset -> snapshot id.
    snapshot_ids: dict[str, str]
    is_new: bool


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    for table in BRONZE_TABLES:
        con.execute(_BRONZE_DDL.format(table=table))
    con.execute(_REGISTRY_DDL)


@contextmanager
def transaction(con: duckdb.DuckDBPyConnection) -> Iterator[None]:
    """Commit everything in the block, or nothing if it raises."""
    con.begin()
    try:
        yield
    except BaseException:
        con.rollback()
        raise
    con.commit()


def snapshot_id_for(dataset: str, content_sha256: str) -> str:
    return short_id([dataset, content_sha256])


def snapshot_set_id_for(snapshot_ids: dict[str, str]) -> str:
    """The same members give the same id, whatever the order or time of ingest."""
    return short_id([[dataset, snapshot_ids[dataset]] for dataset in sorted(snapshot_ids)])


def rows_digest(con: duckdb.DuckDBPyConnection, table: str, snapshot_id: str) -> str:
    """SHA-256 over a snapshot's row hashes in file order.

    Stored at ingest; recomputing it later shows whether the rows have changed.
    """
    _check_table(table)
    digest = hashlib.sha256()
    con.execute(
        f"SELECT _row_hash FROM {table} WHERE _snapshot_id = ? ORDER BY _row_num", [snapshot_id]
    )
    while batch := con.fetchmany(10_000):
        for (row_hash,) in batch:
            digest.update(row_hash.encode("ascii") + b"\n")
    return digest.hexdigest()


def resolve_set(con: duckdb.DuckDBPyConnection, id_or_alias: str) -> SnapshotSet:
    """Resolve a set id or an alias.

    `latest` is the newest set. `fixtures` is the newest fixture set that one
    complete directory supplied, so ingesting a changed list alone moves
    `latest` and leaves `fixtures` where it was.
    """
    if id_or_alias == ALIAS_LATEST:
        row = con.execute(
            "SELECT snapshot_set_id FROM snapshot_set_info ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    elif id_or_alias == ALIAS_FIXTURES:
        row = con.execute(
            "SELECT snapshot_set_id FROM snapshot_set_info"
            " WHERE family = ? AND is_complete ORDER BY seq DESC LIMIT 1",
            [FAMILY_FIXTURES],
        ).fetchone()
    else:
        row = (id_or_alias,)
    found = _load_set(con, row[0]) if row else None
    if found is None:
        raise UnknownSnapshotSet(f"No snapshot set '{id_or_alias}'.")
    return found


class SnapshotManager:
    """Registers snapshots and snapshot sets.

    A loader calls `open_snapshot`, fills the Bronze table for a new snapshot
    (with `insert_rows`, or with its own bulk SQL), calls `register_snapshot`,
    and finally `commit_set`, all inside one `transaction`.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        create_schema(con)

    def open_snapshot(self, dataset: str, family: str, table_name: str, path: Path) -> Snapshot:
        _check_table(table_name)
        with path.open("rb") as f:
            content_sha256 = hashlib.file_digest(f, "sha256").hexdigest()
        snapshot_id = snapshot_id_for(dataset, content_sha256)
        present = self.con.execute(
            "SELECT 1 FROM bronze_snapshots WHERE snapshot_id = ?", [snapshot_id]
        ).fetchone()
        return Snapshot(
            snapshot_id=snapshot_id,
            dataset=dataset,
            family=family,
            table_name=table_name,
            file_name=path.name,
            content_sha256=content_sha256,
            byte_size=path.stat().st_size,
            is_new=present is None,
        )

    def insert_rows(self, snapshot: Snapshot, records: Iterable[SourceRecord]) -> None:
        """Write records in file order. For sources small enough to hold in memory."""
        rows = [
            (
                snapshot.snapshot_id,
                record.source,
                record.source_record_id,
                record.as_of,
                record.confidence,
                row_num,
                sha256_hex(record.raw),
                canonical_json(record.raw).decode("utf-8"),
            )
            for row_num, record in enumerate(records, start=1)
        ]
        if rows:
            placeholders = ", ".join("?" * len(BRONZE_COLUMNS))
            self.con.executemany(
                f"INSERT INTO {snapshot.table_name} ({', '.join(BRONZE_COLUMNS)})"
                f" VALUES ({placeholders})",
                rows,
            )

    def register_snapshot(self, snapshot: Snapshot, published_at: date | None = None) -> None:
        """Record a snapshot whose rows are now in its Bronze table."""
        row_count, as_of_min, as_of_max = self.con.execute(
            f"SELECT count(*), min(_as_of), max(_as_of) FROM {snapshot.table_name}"
            " WHERE _snapshot_id = ?",
            [snapshot.snapshot_id],
        ).fetchone()
        self.con.execute(
            "INSERT INTO bronze_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                snapshot.snapshot_id,
                snapshot.dataset,
                snapshot.family,
                snapshot.table_name,
                snapshot.file_name,
                snapshot.content_sha256,
                snapshot.byte_size,
                row_count,
                rows_digest(self.con, snapshot.table_name, snapshot.snapshot_id),
                as_of_min,
                as_of_max,
                published_at,
                datetime.now(UTC).replace(tzinfo=None),
            ],
        )

    def commit_set(
        self,
        family: str,
        snapshot_ids: dict[str, str],
        required: Collection[str],
        base: str | None = None,
    ) -> SnapshotSet:
        """Record the set made of `snapshot_ids`, one per dataset in `required`.

        Datasets this ingest did not supply are taken from `base`, or from the
        newest set of the same family. Sets of another family are never used, so
        fixture data and real data cannot end up in one set.
        """
        unknown = sorted(set(snapshot_ids) - set(required))
        if unknown:
            raise IngestError(f"Datasets {unknown} do not belong to the '{family}' family.")

        members = dict(snapshot_ids)
        missing = sorted(set(required) - set(snapshot_ids))
        base_set = self._base_set(family, base, missing) if missing else None
        if base_set is not None:
            for dataset in missing:
                if dataset not in base_set.snapshot_ids:
                    raise IngestError(
                        f"Snapshot set {base_set.snapshot_set_id} has no '{dataset}' dataset."
                    )
                members[dataset] = base_set.snapshot_ids[dataset]

        set_id = snapshot_set_id_for(members)
        existing = _load_set(self.con, set_id)
        if existing is not None:
            return existing

        seq = self.con.execute(
            "SELECT coalesce(max(seq), 0) + 1 FROM snapshot_set_info"
        ).fetchone()[0]
        base_set_id = base_set.snapshot_set_id if base_set else None
        self.con.execute(
            "INSERT INTO snapshot_set_info VALUES (?, ?, ?, ?, ?)",
            [set_id, family, seq, base_set is None, base_set_id],
        )
        self.con.executemany(
            "INSERT INTO snapshot_sets VALUES (?, ?, ?)",
            [(set_id, dataset, members[dataset]) for dataset in sorted(members)],
        )
        return SnapshotSet(
            snapshot_set_id=set_id,
            family=family,
            seq=seq,
            is_complete=base_set is None,
            base_set_id=base_set_id,
            snapshot_ids=dict(sorted(members.items())),
            is_new=True,
        )

    def _base_set(self, family: str, base: str | None, missing: list[str]) -> SnapshotSet:
        if base is not None:
            try:
                base_set = resolve_set(self.con, base)
            except UnknownSnapshotSet as exc:
                raise IngestError(str(exc)) from exc
            if base_set.family != family:
                raise IngestError(
                    f"Snapshot set {base_set.snapshot_set_id} is of family"
                    f" '{base_set.family}', not '{family}'."
                )
            return base_set

        row = self.con.execute(
            "SELECT snapshot_set_id FROM snapshot_set_info"
            " WHERE family = ? ORDER BY seq DESC LIMIT 1",
            [family],
        ).fetchone()
        if row is None:
            raise IngestError(
                f"The input has no {missing} and there is no earlier '{family}' snapshot set"
                " to take them from. Ingest a complete set first."
            )
        return _load_set(self.con, row[0])


def _check_table(table: str) -> None:
    if table not in BRONZE_TABLES:
        raise ValueError(f"'{table}' is not a Bronze table")


def _load_set(con: duckdb.DuckDBPyConnection, set_id: str) -> SnapshotSet | None:
    info = con.execute(
        "SELECT family, seq, is_complete, base_set_id FROM snapshot_set_info"
        " WHERE snapshot_set_id = ?",
        [set_id],
    ).fetchone()
    if info is None:
        return None
    members = con.execute(
        "SELECT dataset, snapshot_id FROM snapshot_sets WHERE snapshot_set_id = ? ORDER BY dataset",
        [set_id],
    ).fetchall()
    family, seq, is_complete, base_set_id = info
    return SnapshotSet(
        snapshot_set_id=set_id,
        family=family,
        seq=seq,
        is_complete=is_complete,
        base_set_id=base_set_id,
        snapshot_ids=dict(members),
        is_new=False,
    )
