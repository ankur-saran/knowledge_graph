"""Loads a directory of fixture CSVs into Bronze.

Strict: one bad file, row or byte and nothing is written.
"""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ubo_sentinel.models.fixture_rows import (
    EntityRow,
    RelationshipRow,
    RepexRow,
    SanctionRow,
    _Row,
    read_raw_rows,
)
from ubo_sentinel.pipeline.bronze import (
    ALIAS_FIXTURES,
    ALIAS_LATEST,
    FAMILY_FIXTURES,
    IngestError,
    SnapshotManager,
    SnapshotSet,
    SourceRecord,
    UnknownSnapshotSet,
    resolve_set,
    transaction,
)

# Commands are run from the repository root.
DEFAULT_PATH = Path("fixtures/snapshot_t0")


@dataclass(frozen=True)
class FixtureDataset:
    name: str
    table: str
    model: type[_Row]


# File name -> dataset. A directory may hold any non-empty subset of these.
DATASETS: dict[str, FixtureDataset] = {
    "entities.csv": FixtureDataset("entities", "bronze_entities", EntityRow),
    "relationships.csv": FixtureDataset("relationships", "bronze_relationships", RelationshipRow),
    "sanctions.csv": FixtureDataset("sanctions", "bronze_sanctions", SanctionRow),
    "repex.csv": FixtureDataset("repex", "bronze_repex", RepexRow),
}


@dataclass(frozen=True)
class DatasetSummary:
    dataset: str
    table: str
    snapshot_id: str
    row_count: int
    # "new", "already present" or "inherited".
    status: str


@dataclass(frozen=True)
class IngestSummary:
    source: str
    path: Path
    datasets: list[DatasetSummary]
    snapshot_set: SnapshotSet
    # Alias -> the set id it resolves to after this ingest; None when it resolves to nothing.
    aliases: dict[str, str | None]


def check_stable_bytes(path: Path) -> None:
    """Snapshot ids hash file bytes, so the bytes must not vary by platform."""
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise IngestError(f"{path}: starts with a BOM; save it as UTF-8 without one.")
    if b"\r" in data:
        raise IngestError(f"{path}: has CR line endings; convert it to LF.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IngestError(f"{path}: is not UTF-8 ({exc}).") from exc


def _read_records(path: Path, dataset: FixtureDataset) -> list[SourceRecord]:
    check_stable_bytes(path)
    try:
        pairs = read_raw_rows(path, dataset.model)
    except ValueError as exc:
        raise IngestError(str(exc)) from exc

    duplicates = [ref for ref, count in Counter(row.ref for _, row in pairs).items() if count > 1]
    if duplicates:
        raise IngestError(f"{path}: duplicate record keys {sorted(duplicates)}")
    return [
        SourceRecord(row.source, row.source_record_id, row.as_of, row.confidence, raw)
        for raw, row in pairs
    ]


def ingest_fixtures(
    con: duckdb.DuckDBPyConnection, path: Path | None = None, base: str | None = None
) -> IngestSummary:
    """Ingest the fixture files in `path`; datasets it lacks come from `base`.

    Ingesting files that are already present changes nothing.
    """
    path = path or DEFAULT_PATH
    if not path.is_dir():
        raise IngestError(f"{path} is not a directory.")
    csv_names = sorted(p.name for p in path.iterdir() if p.is_file() and p.suffix == ".csv")
    unknown = [name for name in csv_names if name not in DATASETS]
    if unknown:
        raise IngestError(f"{path}: unknown files {unknown}; expected only {sorted(DATASETS)}.")
    if not csv_names:
        raise IngestError(f"{path}: holds none of {sorted(DATASETS)}.")

    # Every file is read and checked before anything is written.
    records = {name: _read_records(path / name, DATASETS[name]) for name in csv_names}

    manager = SnapshotManager(con)
    status: dict[str, str] = {}
    supplied: dict[str, str] = {}
    with transaction(con):
        for name in csv_names:
            dataset = DATASETS[name]
            snapshot = manager.open_snapshot(
                dataset.name, FAMILY_FIXTURES, dataset.table, path / name
            )
            if snapshot.is_new:
                manager.insert_rows(snapshot, records[name])
                manager.register_snapshot(snapshot)
            supplied[dataset.name] = snapshot.snapshot_id
            status[dataset.name] = "new" if snapshot.is_new else "already present"
        snapshot_set = manager.commit_set(
            FAMILY_FIXTURES, supplied, required=[d.name for d in DATASETS.values()], base=base
        )

    row_counts = dict(con.execute("SELECT snapshot_id, row_count FROM bronze_snapshots").fetchall())
    datasets = [
        DatasetSummary(
            dataset=dataset.name,
            table=dataset.table,
            snapshot_id=snapshot_set.snapshot_ids[dataset.name],
            row_count=row_counts[snapshot_set.snapshot_ids[dataset.name]],
            status=status.get(dataset.name, "inherited"),
        )
        for dataset in DATASETS.values()
    ]
    return IngestSummary(
        source=FAMILY_FIXTURES,
        path=path,
        datasets=datasets,
        snapshot_set=snapshot_set,
        aliases={alias: _resolve(con, alias) for alias in (ALIAS_FIXTURES, ALIAS_LATEST)},
    )


def _resolve(con: duckdb.DuckDBPyConnection, alias: str) -> str | None:
    try:
        return resolve_set(con, alias).snapshot_set_id
    except UnknownSnapshotSet:
        return None
