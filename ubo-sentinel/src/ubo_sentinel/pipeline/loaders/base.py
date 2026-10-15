"""What every loader returns, whatever its source."""

from dataclasses import dataclass
from pathlib import Path

import duckdb

from ubo_sentinel.pipeline.bronze import SnapshotSet, UnknownSnapshotSet, resolve_set


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


def resolve_alias(con: duckdb.DuckDBPyConnection, alias: str) -> str | None:
    try:
        return resolve_set(con, alias).snapshot_set_id
    except UnknownSnapshotSet:
        return None
