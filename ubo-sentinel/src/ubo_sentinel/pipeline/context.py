"""The context layer: what an investigation reads beside the graph (Step 14).

Addresses come from datasets of their own (`addresses` in fixtures,
`gleif_l1_addresses` and `os_addresses` in real data) and are derived here,
after Gold, into `ctx_addresses`. No rule reads them, so they are outside the
Silver and Gold digests: building this layer moves no `graph_digest` and no
decision id. `context_digest` names what an investigation read, as
`gold_digest` names what a screen read.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from ubo_sentinel import __version__
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.pipeline import gold
from ubo_sentinel.pipeline.address import normalise_address
from ubo_sentinel.pipeline.bronze import SnapshotSet, rows_digest, transaction
from ubo_sentinel.pipeline.digest import fold_tables
from ubo_sentinel.pipeline.jurisdiction import normalise_jurisdiction
from ubo_sentinel.pipeline.loaders.gleif_loader import GLEIF_ADDRESS_KINDS
from ubo_sentinel.pipeline.sources import CONTEXT_DATASETS

# Raise when a change here alters the rows of the layer.
CONTEXT_VERSION = 1


_PROVENANCE = """
    source TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    as_of DATE NOT NULL,
    confidence DOUBLE NOT NULL"""

# One row per entity, kind and place. `address` is the text as one record wrote
# it; `address_norm` and `country` are what two records are compared on.
_KEY = "entity_id, kind, address_norm, country"
_DDL = f"""
CREATE TABLE IF NOT EXISTS ctx_addresses (
    snapshot_set_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    address TEXT NOT NULL,
    address_norm TEXT NOT NULL,
    country TEXT,{_PROVENANCE}
);
CREATE TABLE IF NOT EXISTS ctx_builds (
    snapshot_set_id TEXT PRIMARY KEY,
    gold_digest TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    context_version INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    counts JSON NOT NULL,
    built_at TIMESTAMP NOT NULL
)
"""

_STAGE = """
CREATE OR REPLACE TEMP TABLE stg_ctx_addresses (
    ref_source TEXT, ref_record TEXT, kind TEXT, address TEXT, country TEXT,
    source TEXT, source_record_id TEXT, snapshot_id TEXT, as_of DATE, confidence DOUBLE
)
"""

_PROV_COLUMNS = "_source, _source_record_id, _snapshot_id, _as_of, _confidence"


def _fixture_select() -> str:
    ref = "_raw->>'entity_ref'"
    return (
        f"SELECT regexp_extract({ref}, '^([^:]*):', 1), regexp_extract({ref}, '^[^:]*:(.*)$', 1),"
        f" _raw->>'kind', _raw->>'address', _raw->>'country', {_PROV_COLUMNS}"
        " FROM bronze_addresses WHERE _snapshot_id = $snapshot"
    )


def _gleif_select() -> str:
    parts = ("FirstAddressLine", "AdditionalAddressLine.1", "City", "Region", "PostalCode")
    selects = []
    for field, kind in GLEIF_ADDRESS_KINDS.items():
        text = ", ".join(f"_raw->>'$.\"Entity.{field}.{part}\"'" for part in parts)
        selects.append(
            f"SELECT 'gleif', _raw->>'$.\"LEI\"', '{kind}', nullif(concat_ws(', ', {text}), ''),"
            f" _raw->>'$.\"Entity.{field}.Country\"', {_PROV_COLUMNS}"
            " FROM bronze_addresses WHERE _snapshot_id = $snapshot"
        )
    return " UNION ALL ".join(selects)


def _os_select() -> str:
    # The country only when the record states exactly one (as the entity mapper reads it).
    country = (
        "CASE WHEN len(CAST(coalesce(_raw->'$.properties.country', '[]') AS TEXT[])) = 1"
        " THEN _raw->>'$.properties.country[0]' END"
    )
    return (
        "SELECT 'opensanctions', _raw->>'$.id', 'OTHER', address, "
        f"{country}, {_PROV_COLUMNS} FROM bronze_addresses,"
        " unnest(CAST(coalesce(_raw->'$.properties.address', '[]') AS TEXT[])) AS t(address)"
        " WHERE _snapshot_id = $snapshot"
    )


_SELECTS = {
    "addresses": _fixture_select,
    "gleif_l1_addresses": _gleif_select,
    "os_addresses": _os_select,
}


class ContextError(ValueError):
    """The context layer cannot be built for this set. Nothing was written."""


class ContextNotBuilt(LookupError):
    """The set's context layer is missing or stale. The message names the command to run."""


@dataclass(frozen=True)
class ContextSummary:
    snapshot_set_id: str
    # "built", "rebuilt" or "already built".
    status: str
    counts: dict[str, Any]
    context_digest: str


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_DDL)


def _register(con: duckdb.DuckDBPyConnection) -> None:
    present = {
        name
        for (name,) in con.execute(
            "SELECT function_name FROM duckdb_functions()"
            " WHERE function_name IN ('ubo_address', 'ubo_jurisdiction')"
        ).fetchall()
    }
    if "ubo_address" not in present:
        con.create_function("ubo_address", normalise_address, ["VARCHAR"], "VARCHAR")
    if "ubo_jurisdiction" not in present:
        con.create_function("ubo_jurisdiction", normalise_jurisdiction, ["VARCHAR"], "VARCHAR")


def context_digest(con: duckdb.DuckDBPyConnection, set_id: str) -> str:
    """The digest of a set's current context layer. Raises `ContextNotBuilt`."""
    if not gold.is_built(con, set_id):
        raise ContextNotBuilt(
            f"Set {set_id} has no current graph: run `ubo build-graph --snapshot {set_id}`."
        )
    built = _load(con, set_id)
    if built is None or not _is_current(built, gold.load_build(con, set_id)["gold_digest"]):
        raise ContextNotBuilt(
            f"Set {set_id} has no current context layer: run `ubo build-graph --snapshot {set_id}`."
        )
    return built["context_digest"]


def _load(con: duckdb.DuckDBPyConnection, set_id: str) -> dict[str, Any] | None:
    if not con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'ctx_builds'"
    ).fetchone():
        return None
    row = con.execute(
        "SELECT gold_digest, context_digest, context_version, engine_version, counts"
        " FROM ctx_builds WHERE snapshot_set_id = ?",
        [set_id],
    ).fetchone()
    if row is None:
        return None
    gold_digest, digest, version, engine, counts = row
    return {
        "gold_digest": gold_digest,
        "context_digest": digest,
        "context_version": version,
        "engine_version": engine,
        "counts": json.loads(counts),
    }


def _is_current(built: dict[str, Any], gold_digest: str) -> bool:
    return (built["gold_digest"], built["context_version"], built["engine_version"]) == (
        gold_digest,
        CONTEXT_VERSION,
        __version__,
    )


def build_context(con: duckdb.DuckDBPyConnection, snapshot_set: SnapshotSet) -> ContextSummary:
    """Build the context layer of a set whose Gold is current. Building it again changes nothing.

    A set with no context dataset gets an empty layer, so every built set has one.
    """
    set_id = snapshot_set.snapshot_set_id
    if not gold.is_built(con, set_id):
        raise ContextError(
            f"Set {set_id} has no current graph: run `ubo build-graph --snapshot {set_id}`."
        )
    gold_digest = gold.load_build(con, set_id)["gold_digest"]
    create_schema(con)
    existing = _load(con, set_id)
    if existing is not None and _is_current(existing, gold_digest):
        return ContextSummary(
            set_id, "already built", existing["counts"], existing["context_digest"]
        )

    _register(con)
    con.execute(_STAGE)
    for dataset, snapshot_id in sorted(snapshot_set.snapshot_ids.items()):
        if dataset not in CONTEXT_DATASETS:
            continue
        table, recorded = con.execute(
            "SELECT table_name, rows_digest FROM bronze_snapshots WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone()
        if rows_digest(con, table, snapshot_id) != recorded:
            raise ContextError(
                f"Snapshot {snapshot_id} ({dataset}): the rows in {table} are not the rows"
                " that were ingested."
            )
        con.execute(
            f"INSERT INTO stg_ctx_addresses {_SELECTS[dataset]()}", {"snapshot": snapshot_id}
        )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE stg_ctx_resolved AS
        SELECT x.entity_id, s.kind, s.address, ubo_address(s.address) AS address_norm,
               ubo_jurisdiction(s.country) AS country,
               s.source, s.source_record_id, s.snapshot_id, s.as_of, s.confidence
        FROM stg_ctx_addresses s
        LEFT JOIN (
            SELECT DISTINCT source, source_record_id, entity_id FROM silver_entity_xref
            WHERE snapshot_set_id = $set
        ) x ON x.source = s.ref_source AND x.source_record_id = s.ref_record
        WHERE s.address IS NOT NULL
        """,
        {"set": set_id},
    )
    staged, unresolved, unreadable = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE entity_id IS NULL),"
        " count(*) FILTER (WHERE entity_id IS NOT NULL AND address_norm IS NULL)"
        " FROM stg_ctx_resolved"
    ).fetchone()

    with transaction(con):
        con.execute("DELETE FROM ctx_addresses WHERE snapshot_set_id = ?", [set_id])
        con.execute("DELETE FROM ctx_builds WHERE snapshot_set_id = ?", [set_id])
        # One row per entity, kind and place; the record that states it first by
        # (source, record id) is its provenance.
        con.execute(
            f"""
            INSERT INTO ctx_addresses
            SELECT $set, entity_id, kind, address, address_norm, country,
                   source, source_record_id, snapshot_id, as_of, confidence
            FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY {_KEY} ORDER BY source, source_record_id, address
                ) AS n
                FROM stg_ctx_resolved
                WHERE entity_id IS NOT NULL AND address_norm IS NOT NULL
            )
            WHERE n = 1
            """,
            {"set": set_id},
        )
        rows = con.execute(
            "SELECT count(*) FROM ctx_addresses WHERE snapshot_set_id = ?", [set_id]
        ).fetchone()[0]
        counts = {
            "ctx_addresses": rows,
            "staged": staged,
            "unresolved": unresolved,
            "unreadable": unreadable,
        }
        digest = fold_tables(con, [("ctx_addresses", _KEY)], set_id, gold_digest)
        con.execute(
            "INSERT INTO ctx_builds VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                set_id,
                gold_digest,
                digest,
                CONTEXT_VERSION,
                __version__,
                canonical_json(counts).decode("utf-8"),
                datetime.now(UTC).replace(tzinfo=None),
            ],
        )
    return ContextSummary(set_id, "rebuilt" if existing else "built", counts, digest)
