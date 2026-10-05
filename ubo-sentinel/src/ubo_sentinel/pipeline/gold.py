"""Gold: the graph a screen reads, for one snapshot set.

Gold holds what Silver does not have: each edge's upper and lower end, each
node's degrees, links in both directions and the name blocks the resolver
searches. It is derived from Silver and is rebuilt whenever Silver changes.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from ubo_sentinel import __version__
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.pipeline import silver
from ubo_sentinel.pipeline.bronze import SnapshotSet, transaction
from ubo_sentinel.pipeline.entity_linking import BLOCK_PREFIX_LENGTH

_PROVENANCE = """
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    as_of DATE NOT NULL,
    confidence DOUBLE NOT NULL"""

# Table -> (columns after `snapshot_set_id`, key within a set). The key orders
# the rows for `gold_digest`.
_TABLES: dict[str, tuple[str, str]] = {
    # `up_degree` counts the edges the node is the lower end of (its owners,
    # parents and controllers); `down_degree` the edges it is the upper end of.
    "graph_nodes": (
        f"""
    id TEXT NOT NULL,
    label TEXT NOT NULL,
    lei TEXT,
    registration_authority_id TEXT,
    registration_number TEXT,
    legal_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL,
    jurisdiction TEXT,
    status TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    repex_reason TEXT,{_PROVENANCE},
    supporting_records JSON NOT NULL,
    up_degree INTEGER NOT NULL,
    down_degree INTEGER NOT NULL""",
        "id",
    ),
    # `upper_id` is the owner, parent or controller; `lower_id` is the other end.
    # This is the one place that applies the direction of each relation.
    "graph_edges": (
        f"""
    id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    pct DECIMAL(7,4),
    basis TEXT NOT NULL,
    is_ultimate BOOLEAN NOT NULL,{_PROVENANCE},
    supporting_records JSON NOT NULL,
    upper_id TEXT NOT NULL,
    lower_id TEXT NOT NULL""",
        "id",
    ),
    "graph_sanctions": (
        f"""
    id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    designated_node_id TEXT NOT NULL,
    program TEXT NOT NULL,
    list_date DATE,
    list_source TEXT NOT NULL,
    is_active BOOLEAN NOT NULL,
    match_type TEXT NOT NULL,
    match_confidence DOUBLE NOT NULL,{_PROVENANCE}""",
        "id",
    ),
    # Every link once from each end.
    "graph_links": (
        """
    node_id TEXT NOT NULL,
    linked_node_id TEXT NOT NULL,
    match_type TEXT NOT NULL,
    confidence DOUBLE NOT NULL,
    name TEXT NOT NULL,
    linked_name TEXT NOT NULL""",
        "node_id, linked_node_id",
    ),
    # Every name under each of its blocks: the first letters of each word.
    "graph_name_blocks": (
        """
    block TEXT NOT NULL,
    node_id TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    kind TEXT NOT NULL""",
        "block, node_id, name",
    ),
}

GOLD_TABLES = tuple(_TABLES)

# `built_at` is the only wall-clock value in Gold; no id or hash reads it.
_BUILDS_DDL = """
CREATE TABLE IF NOT EXISTS gold_builds (
    snapshot_set_id TEXT PRIMARY KEY,
    silver_digest TEXT NOT NULL,
    gold_digest TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    counts JSON NOT NULL,
    built_at TIMESTAMP NOT NULL
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS graph_edges_lower ON graph_edges (snapshot_set_id, lower_id)",
    "CREATE INDEX IF NOT EXISTS graph_edges_upper ON graph_edges (snapshot_set_id, upper_id)",
    "CREATE INDEX IF NOT EXISTS graph_sanctions_node ON graph_sanctions (snapshot_set_id, node_id)",
)

_PROV = "source, source_record_id, snapshot_id, as_of, confidence"


class GoldError(ValueError):
    """Gold cannot be built for this snapshot set. Nothing was written."""


@dataclass(frozen=True)
class GoldSummary:
    snapshot_set_id: str
    # "built", "rebuilt" or "already built".
    status: str
    # Row count of each Gold table.
    counts: dict[str, int]
    gold_digest: str


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    for table, (columns, key) in _TABLES.items():
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (\n    snapshot_set_id TEXT NOT NULL,{columns},"
            f"\n    PRIMARY KEY (snapshot_set_id, {key})\n)"
        )
    con.execute(_BUILDS_DDL)
    for index in _INDEXES:
        con.execute(index)


def gold_digest(con: duckdb.DuckDBPyConnection, snapshot_set_id: str, silver_digest: str) -> str:
    """SHA-256 over the Silver digest and every Gold row of a set, in key order.

    It names the graph a screen read: the same Bronze rows and the same code
    give the same digest on any machine.
    """
    digest = hashlib.sha256()
    digest.update(silver_digest.encode("ascii") + b"\n")
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
        "SELECT silver_digest, gold_digest, engine_version, counts FROM gold_builds"
        " WHERE snapshot_set_id = ?",
        [snapshot_set_id],
    ).fetchone()
    if row is None:
        return None
    silver_digest, digest, engine_version, counts = row
    return {
        "silver_digest": silver_digest,
        "gold_digest": digest,
        "engine_version": engine_version,
        # Stored as canonical JSON, so in key order; given back in table order.
        "counts": {
            table: rows
            for table in GOLD_TABLES
            if (rows := json.loads(counts).get(table)) is not None
        },
    }


def is_built(con: duckdb.DuckDBPyConnection, snapshot_set_id: str) -> bool:
    """True when Gold exists for the set and was built from the Silver it has now."""
    built = load_build(con, snapshot_set_id)
    source = silver.load_build(con, snapshot_set_id)
    return built is not None and source is not None and _is_current(built, source["silver_digest"])


def _is_current(built: dict[str, Any], silver_digest: str) -> bool:
    return (built["silver_digest"], built["engine_version"]) == (silver_digest, __version__)


def build_gold(con: duckdb.DuckDBPyConnection, snapshot_set: SnapshotSet) -> GoldSummary:
    """Build Gold for one snapshot set. Building a current set changes nothing."""
    silver.create_schema(con)
    create_schema(con)
    set_id = snapshot_set.snapshot_set_id

    source = silver.load_build(con, set_id)
    if source is None:
        raise GoldError(
            f"Silver is not built for snapshot set {set_id}."
            f" Run 'ubo normalize --snapshot {set_id}' first."
        )
    silver_digest = source["silver_digest"]

    existing = load_build(con, set_id)
    if existing is not None and _is_current(existing, silver_digest):
        return GoldSummary(set_id, "already built", existing["counts"], existing["gold_digest"])

    # Deleted in a transaction of its own: DuckDB does not let one transaction
    # delete a key and insert it again.
    if existing is not None:
        with transaction(con):
            for table in (*GOLD_TABLES, "gold_builds"):
                con.execute(f"DELETE FROM {table} WHERE snapshot_set_id = ?", [set_id])

    with transaction(con):
        _write_nodes(con, set_id)
        _write_edges(con, set_id)
        _write_sanctions(con, set_id)
        _write_links(con, set_id)
        _write_name_blocks(con, set_id)
        _check(con, set_id)
        counts = {table: _count(con, table, set_id) for table in GOLD_TABLES}
        digest = gold_digest(con, set_id, silver_digest)
        con.execute(
            "INSERT INTO gold_builds VALUES (?, ?, ?, ?, ?, ?)",
            [
                set_id,
                silver_digest,
                digest,
                __version__,
                canonical_json(counts).decode("utf-8"),
                datetime.now(UTC).replace(tzinfo=None),
            ],
        )
    return GoldSummary(set_id, "rebuilt" if existing else "built", counts, digest)


def _count(con: duckdb.DuckDBPyConnection, table: str, set_id: str) -> int:
    return con.execute(
        f"SELECT count(*) FROM {table} WHERE snapshot_set_id = ?", [set_id]
    ).fetchone()[0]


def _write_nodes(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    # The upper end of an edge: the subject, except that a child is consolidated
    # by its parent.
    con.execute(
        f"""
        INSERT INTO graph_nodes
        WITH ends AS (
            SELECT CASE WHEN rel_type = 'CONSOLIDATED_BY' THEN object_id ELSE subject_id END
                       AS upper_id,
                   CASE WHEN rel_type = 'CONSOLIDATED_BY' THEN subject_id ELSE object_id END
                       AS lower_id
            FROM silver_relationships WHERE snapshot_set_id = ?
        )
        SELECT e.snapshot_set_id, e.id, e.entity_type, e.lei, e.registration_authority_id,
            e.registration_number, e.legal_name, e.aliases, e.jurisdiction, e.status,
            e.entity_type, e.repex_reason, {", ".join(f"e.{c.strip()}" for c in _PROV.split(","))},
            e.supporting_records,
            (SELECT count(*) FROM ends WHERE lower_id = e.id),
            (SELECT count(*) FROM ends WHERE upper_id = e.id)
        FROM silver_entities e WHERE e.snapshot_set_id = ?
        """,
        [set_id, set_id],
    )


def _write_edges(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    con.execute(
        f"""
        INSERT INTO graph_edges
        SELECT snapshot_set_id, id, subject_id, object_id, rel_type, pct, basis, is_ultimate,
            {_PROV}, supporting_records,
            CASE WHEN rel_type = 'CONSOLIDATED_BY' THEN object_id ELSE subject_id END,
            CASE WHEN rel_type = 'CONSOLIDATED_BY' THEN subject_id ELSE object_id END
        FROM silver_relationships WHERE snapshot_set_id = ?
        """,
        [set_id],
    )


def _write_sanctions(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    con.execute(
        f"""
        INSERT INTO graph_sanctions
        SELECT snapshot_set_id, id, entity_id, designated_entity_id, program, list_date,
            list_source, is_active, match_type, match_confidence, {_PROV}
        FROM silver_sanctions WHERE snapshot_set_id = ?
        """,
        [set_id],
    )


def _write_links(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    con.execute(
        """
        INSERT INTO graph_links
        SELECT snapshot_set_id, entity_id_a, entity_id_b, match_type, confidence, name_a, name_b
        FROM silver_entity_links WHERE snapshot_set_id = ?
        UNION ALL
        SELECT snapshot_set_id, entity_id_b, entity_id_a, match_type, confidence, name_b, name_a
        FROM silver_entity_links WHERE snapshot_set_id = ?
        """,
        [set_id, set_id],
    )


def _write_name_blocks(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    # The same blocks Silver links on, so a name found there is found here.
    con.execute(
        """
        INSERT INTO graph_name_blocks
        SELECT DISTINCT snapshot_set_id, left(token, ?), entity_id, name, name_norm, kind FROM (
            SELECT *, unnest(string_split(name_norm, ' ')) AS token
            FROM silver_entity_names WHERE snapshot_set_id = ?
        ) WHERE token <> ''
        """,
        [BLOCK_PREFIX_LENGTH, set_id],
    )


def _check(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    """Refuse a graph whose rows do not fit together. Every later layer assumes they do."""
    checks = {
        "edges with an end that is not a node": """
            SELECT count(*) FROM graph_edges e WHERE e.snapshot_set_id = $1 AND (
                e.upper_id NOT IN (SELECT id FROM graph_nodes WHERE snapshot_set_id = $1)
                OR e.lower_id NOT IN (SELECT id FROM graph_nodes WHERE snapshot_set_id = $1))""",
        "designations of an entity that is not a node": """
            SELECT count(*) FROM graph_sanctions s WHERE s.snapshot_set_id = $1 AND (
                s.node_id NOT IN (SELECT id FROM graph_nodes WHERE snapshot_set_id = $1)
                OR s.designated_node_id NOT IN
                    (SELECT id FROM graph_nodes WHERE snapshot_set_id = $1))""",
        "links to an entity that is not a node": """
            SELECT count(*) FROM graph_links l WHERE l.snapshot_set_id = $1
                AND l.linked_node_id NOT IN
                    (SELECT id FROM graph_nodes WHERE snapshot_set_id = $1)""",
    }
    for problem, sql in checks.items():
        found = con.execute(sql, [set_id]).fetchone()[0]
        if found:
            raise GoldError(f"Snapshot set {set_id}: {found} {problem}.")

    for gold_table, silver_table in (
        ("graph_nodes", "silver_entities"),
        ("graph_edges", "silver_relationships"),
        ("graph_sanctions", "silver_sanctions"),
    ):
        gold_rows, silver_rows = _count(con, gold_table, set_id), _count(con, silver_table, set_id)
        if gold_rows != silver_rows:
            raise GoldError(
                f"Snapshot set {set_id}: {gold_table} has {gold_rows} rows,"
                f" {silver_table} has {silver_rows}."
            )
