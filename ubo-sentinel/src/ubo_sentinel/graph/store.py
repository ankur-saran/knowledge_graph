"""A graph store: one connection and one built snapshot set.

Every query takes a store, so one screen cannot read two sets. The store is
unmasked; what is shown to a person goes through a `GraphView`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import duckdb

from ubo_sentinel.pipeline import gold, silver
from ubo_sentinel.pipeline.bronze import SnapshotSet, UnknownSnapshotSet, resolve_set
from ubo_sentinel.pipeline.db import connect, db_path


def id_list(placeholder: str = "?") -> str:
    """SQL for a list-of-ids parameter, used as `x IN {id_list()}`. An empty list is allowed."""
    return f"(SELECT unnest(CAST({placeholder} AS VARCHAR[])))"


class GraphNotBuilt(LookupError):
    """The snapshot set has no Gold graph, or Silver changed after it was built."""


@dataclass(frozen=True)
class QueryCall:
    """One call of an allow-listed query, as the store logged it."""

    name: str
    rows: int
    seconds: float


@dataclass
class GraphStore:
    con: duckdb.DuckDBPyConnection
    snapshot_set: SnapshotSet
    # Names the graph the store reads; recorded in a decision.
    gold_digest: str
    # Row count of each Gold table.
    counts: dict[str, int]
    calls: list[QueryCall] = field(default_factory=list)

    @property
    def set_id(self) -> str:
        return self.snapshot_set.snapshot_set_id

    @classmethod
    @contextmanager
    def open(
        cls, snapshot: str, con: duckdb.DuckDBPyConnection | None = None
    ) -> Iterator["GraphStore"]:
        """Open a store on a set id or alias.

        Without `con` the database is opened read-only and closed on exit.
        DuckDB lets a file have one writer or readers, never both, so a store is
        opened for one operation and not kept. A process that already holds a
        connection passes it in.

        Raises `UnknownSnapshotSet` or `GraphNotBuilt`.
        """
        own = con is None
        if own:
            if not db_path().exists():
                raise UnknownSnapshotSet(f"No snapshot set '{snapshot}': there is no database yet.")
            con = connect(read_only=True)
        try:
            yield cls._bind(con, snapshot)
        finally:
            if own:
                con.close()

    @classmethod
    def _bind(cls, con: duckdb.DuckDBPyConnection, snapshot: str) -> "GraphStore":
        try:
            snapshot_set = resolve_set(con, snapshot)
        except duckdb.CatalogException as exc:
            raise UnknownSnapshotSet(f"No snapshot set '{snapshot}'.") from exc
        set_id = snapshot_set.snapshot_set_id
        try:
            built = gold.load_build(con, set_id)
            current = built is not None and gold.is_built(con, set_id)
        except duckdb.CatalogException:
            built, current = None, False
        if built is None:
            raise GraphNotBuilt(
                f"Snapshot set {set_id} has no graph. Run 'ubo normalize --snapshot {set_id}'"
                f" and 'ubo build-graph --snapshot {set_id}'."
            )
        if not current:
            raise GraphNotBuilt(
                f"The graph of snapshot set {set_id} was built from an earlier Silver."
                f" Run 'ubo build-graph --snapshot {set_id}'."
            )
        return cls(con, snapshot_set, built["gold_digest"], built["counts"])

    def rows(self, sql: str, parameters: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run a query and return its rows as dicts. For code under `graph/` only."""
        cursor = self.con.execute(sql, parameters or [])
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def build_info(self) -> dict[str, Any]:
        """What was built for this set, and from what."""
        return {
            "snapshot_set_id": self.set_id,
            "family": self.snapshot_set.family,
            "snapshot_ids": dict(self.snapshot_set.snapshot_ids),
            "silver": silver.load_build(self.con, self.set_id),
            "gold": gold.load_build(self.con, self.set_id),
        }
