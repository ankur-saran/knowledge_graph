"""Bronze -> Silver for one snapshot set.

Stage the set's Bronze rows, group the records that are one entity, link the
entities that only resemble each other, then build edges, designations,
reporting exceptions and set-level gaps. One transaction: all of it or nothing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from ubo_sentinel import __version__
from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.models.ontology import OntologyConfig
from ubo_sentinel.models.relationship import relationship_id
from ubo_sentinel.models.sanction import sanction_id
from ubo_sentinel.pipeline.bronze import SnapshotSet, rows_digest, transaction
from ubo_sentinel.pipeline.entity_linking import (
    BLOCK_PREFIX_LENGTH,
    KEY_LEI,
    KEY_REGISTRATION,
    LINK_FLOOR,
    resolve_groups,
)
from ubo_sentinel.pipeline.mappers import MAPPERS, create_stage_tables, stage
from ubo_sentinel.pipeline.name_match import name_similarity, normalise_name
from ubo_sentinel.pipeline.silver import (
    SILVER_TABLES,
    SilverError,
    create_schema,
    load_build,
    silver_digest,
)
from ubo_sentinel.pipeline.sources import SOURCE_RANK

# Raise this when a change here alters the rows Silver holds for the same Bronze
# rows. A set built by another version is not reused.
TRANSFORM_VERSION = 1

REJECT_UNRESOLVED_REF = "UNRESOLVED_REF"
REJECT_SELF_LOOP = "SELF_LOOP_AFTER_MERGE"
REJECT_ENDPOINT_LABEL = "ENDPOINT_LABEL"

# The record that describes its entity joined it by nothing but itself.
JOINED_SELF = "SELF"

_PROV = "source, source_record_id, snapshot_id, as_of, confidence"
# Keys in alphabetical order, so `to_json` writes what `canonical_json` would.
_PROV_STRUCT = (
    "{'as_of': as_of, 'confidence': confidence, 'snapshot_id': snapshot_id,"
    " 'source': source, 'source_record_id': source_record_id}"
)
# The order `sort_provenance` gives.
_PROV_ORDER = "source, source_record_id, snapshot_id, as_of"

# SQL name -> (Python function, argument types, return type).
_FUNCTIONS = {
    "ubo_normalise_name": (normalise_name, ["VARCHAR"], "VARCHAR"),
    "ubo_name_similarity": (name_similarity, ["VARCHAR", "VARCHAR"], "DOUBLE"),
    "ubo_relationship_id": (
        relationship_id,
        ["VARCHAR", "VARCHAR", "VARCHAR", "BOOLEAN"],
        "VARCHAR",
    ),
    "ubo_sanction_id": (sanction_id, ["VARCHAR", "VARCHAR", "VARCHAR"], "VARCHAR"),
}


@dataclass(frozen=True)
class SilverSummary:
    snapshot_set_id: str
    # "built", "rebuilt" or "already built".
    status: str
    # Row counts; `rejects` maps a reason to its count.
    counts: dict[str, Any]
    silver_digest: str


def build_params() -> dict[str, Any]:
    """Everything outside Bronze that decides what Silver holds."""
    return {
        "link_floor": LINK_FLOOR,
        "source_rank": SOURCE_RANK,
        "transform_version": TRANSFORM_VERSION,
    }


def build_silver(
    con: duckdb.DuckDBPyConnection,
    snapshot_set: SnapshotSet,
    ontology: OntologyConfig,
    rebuild: bool = False,
) -> SilverSummary:
    """Build Silver for one snapshot set. Building a built set changes nothing."""
    create_schema(con)
    set_id = snapshot_set.snapshot_set_id
    params = build_params()

    existing = load_build(con, set_id)
    if existing is not None and not rebuild:
        if (existing["engine_version"], existing["params"]) != (__version__, params):
            raise SilverError(
                f"Snapshot set {set_id} was built by engine {existing['engine_version']} with"
                f" {existing['params']}; this is engine {__version__} with {params}."
                " Pass --rebuild to replace it."
            )
        return SilverSummary(set_id, "already built", existing["counts"], existing["silver_digest"])

    _register_functions(con)
    if existing is not None:
        with transaction(con):
            for table in (*SILVER_TABLES, "silver_builds"):
                con.execute(f"DELETE FROM {table} WHERE snapshot_set_id = ?", [set_id])

    with transaction(con):
        _stage(con, snapshot_set)
        refused = _resolve_entities(con, set_id)
        _write_entities(con, set_id)
        _write_links(con, set_id, refused)
        _write_relationships(con, set_id, ontology)
        _write_sanctions(con, set_id)
        _write_reporting_exceptions(con, set_id)
        _write_gaps(con, set_id)
        counts = _counts(con, set_id)
        digest = silver_digest(con, set_id)
        con.execute(
            "INSERT INTO silver_builds VALUES (?, ?, ?, ?, ?, ?)",
            [
                set_id,
                __version__,
                canonical_json(params).decode("utf-8"),
                canonical_json(counts).decode("utf-8"),
                digest,
                datetime.now(UTC).replace(tzinfo=None),
            ],
        )
    return SilverSummary(set_id, "rebuilt" if existing else "built", counts, digest)


def _register_functions(con: duckdb.DuckDBPyConnection) -> None:
    """Let SQL call the one name scorer and the one definition of each id."""
    present = {
        name
        for (name,) in con.execute(
            "SELECT function_name FROM duckdb_functions() WHERE function_name LIKE 'ubo_%'"
        ).fetchall()
    }
    for name, (function, arguments, result) in _FUNCTIONS.items():
        if name not in present:
            con.create_function(name, function, arguments, result)


# --- a. verify and stage ----------------------------------------------------------------


def _stage(con: duckdb.DuckDBPyConnection, snapshot_set: SnapshotSet) -> None:
    create_stage_tables(con)
    for dataset, snapshot_id in snapshot_set.snapshot_ids.items():
        if dataset not in MAPPERS:
            raise SilverError(f"No mapper for dataset '{dataset}'.")
        table, recorded = con.execute(
            "SELECT table_name, rows_digest FROM bronze_snapshots WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone()
        if rows_digest(con, table, snapshot_id) != recorded:
            raise SilverError(
                f"Snapshot {snapshot_id} ({dataset}): the rows in {table} are not the rows"
                " that were ingested."
            )
        stage(con, dataset, table, snapshot_id)

    unknown = con.execute(
        "SELECT DISTINCT source FROM stg_entities WHERE source NOT IN (SELECT unnest(?))"
        " ORDER BY source",
        [list(SOURCE_RANK)],
    ).fetchall()
    if unknown:
        raise SilverError(
            f"Sources {[source for (source,) in unknown]} have no rank in pipeline/sources.py."
        )


# --- b. exact resolution ----------------------------------------------------------------


def _resolve_entities(con: duckdb.DuckDBPyConnection, set_id: str) -> list[tuple[str, str, str]]:
    """Fill `stg_members` (one row per record, with its entity) and the xref.

    Returns the registration joins that the merge guard refused.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE stg_records AS"
        " SELECT *, source || ':' || source_record_id AS ref FROM stg_entities"
    )
    shared_keys = con.execute(
        f"""
        SELECT kind, key, list(ref ORDER BY ref) FROM (
            SELECT ref, '{KEY_LEI}' AS kind, lei AS key FROM stg_records WHERE lei IS NOT NULL
            UNION ALL
            SELECT ref, '{KEY_REGISTRATION}',
                   registration_authority_id || chr(31) || registration_number
            FROM stg_records
            WHERE registration_authority_id IS NOT NULL AND registration_number IS NOT NULL
        )
        GROUP BY kind, key HAVING count(*) > 1
        ORDER BY kind, key
        """
    ).fetchall()
    involved = sorted({ref for _, _, refs in shared_keys for ref in refs})
    attributes = {
        ref: (lei, entity_type)
        for ref, lei, entity_type in con.execute(
            "SELECT ref, lei, entity_type FROM stg_records WHERE ref IN (SELECT unnest(?))",
            [involved],
        ).fetchall()
    }
    roots, refused = resolve_groups(shared_keys, attributes)

    con.execute("CREATE OR REPLACE TEMP TABLE stg_roots (ref TEXT, root TEXT)")
    if roots:
        con.executemany("INSERT INTO stg_roots VALUES (?, ?)", list(roots.items()))
    con.execute("CREATE OR REPLACE TEMP TABLE stg_source_rank (source TEXT, source_rank INTEGER)")
    con.executemany("INSERT INTO stg_source_rank VALUES (?, ?)", list(SOURCE_RANK.items()))

    # The id is `entity_id()`: the LEI when a member has one, else the smallest
    # member id. `pos` 1 is the record that describes the entity: a registry
    # before a list, then the newest.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stg_members AS
        WITH grouped AS (
            SELECT r.*, coalesce(g.root, r.ref) AS root, k.source_rank
            FROM stg_records r
            LEFT JOIN stg_roots g ON g.ref = r.ref
            JOIN stg_source_rank k ON k.source = r.source
        ), placed AS (
            SELECT *,
                coalesce('lei:' || min(lei) OVER (PARTITION BY root),
                         min(ref) OVER (PARTITION BY root)) AS entity_id,
                row_number() OVER (PARTITION BY root ORDER BY source_rank, as_of DESC, ref) AS pos,
                count(*) OVER (PARTITION BY root, lei) AS same_lei
            FROM grouped
        )
        SELECT *,
            CASE WHEN pos = 1 THEN '{JOINED_SELF}'
                 WHEN lei IS NOT NULL AND same_lei > 1 THEN '{KEY_LEI}'
                 ELSE '{KEY_REGISTRATION}' END AS joined_by
        FROM placed
        """
    )
    con.execute(
        "INSERT INTO silver_entity_xref"
        " SELECT ?, source, source_record_id, snapshot_id, entity_id, joined_by, pos = 1"
        " FROM stg_members",
        [set_id],
    )
    return refused


def _write_entities(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    # A field comes from the describing record, or from the next member that has it.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stg_entity AS
        SELECT entity_id,
            first(lei ORDER BY pos) FILTER (WHERE lei IS NOT NULL) AS lei,
            first(registration_authority_id ORDER BY pos)
                FILTER (WHERE registration_number IS NOT NULL) AS registration_authority_id,
            first(registration_number ORDER BY pos)
                FILTER (WHERE registration_number IS NOT NULL) AS registration_number,
            first(legal_name ORDER BY pos) AS legal_name,
            first(jurisdiction ORDER BY pos) FILTER (WHERE jurisdiction IS NOT NULL)
                AS jurisdiction,
            first(status ORDER BY pos) AS status,
            first(entity_type ORDER BY pos) AS entity_type,
            first(source ORDER BY pos) AS source,
            first(source_record_id ORDER BY pos) AS source_record_id,
            first(snapshot_id ORDER BY pos) AS snapshot_id,
            first(as_of ORDER BY pos) AS as_of,
            first(confidence ORDER BY pos) AS confidence,
            to_json(list({_PROV_STRUCT} ORDER BY {_PROV_ORDER})) AS supporting_records
        FROM stg_members GROUP BY entity_id
        """
    )
    con.execute(
        """
        INSERT INTO silver_entity_names
        SELECT ?, n.entity_id, n.name, ubo_normalise_name(n.name),
               CASE WHEN n.name = e.legal_name THEN 'legal_name' ELSE 'alias' END
        FROM (
            SELECT DISTINCT entity_id, name FROM (
                SELECT entity_id, legal_name AS name FROM stg_members
                UNION ALL
                SELECT entity_id, unnest(aliases) FROM stg_members
            )
        ) n JOIN stg_entity e ON e.entity_id = n.entity_id
        """,
        [set_id],
    )
    con.execute(
        f"""
        INSERT INTO silver_entities
        SELECT ?, e.entity_id, e.lei, e.registration_authority_id, e.registration_number,
            e.legal_name, coalesce(a.aliases, CAST([] AS TEXT[])), e.jurisdiction, e.status,
            e.entity_type, r.repex_reason, {", ".join(f"e.{c.strip()}" for c in _PROV.split(","))},
            e.supporting_records
        FROM stg_entity e
        LEFT JOIN (
            SELECT entity_id, list(name ORDER BY name) AS aliases
            FROM silver_entity_names WHERE snapshot_set_id = ? AND kind = 'alias'
            GROUP BY entity_id
        ) a ON a.entity_id = e.entity_id
        LEFT JOIN (
            SELECT m.entity_id,
                array_to_string(
                    list_sort(list_distinct(list(x.category || ':' || x.reason))), '; '
                ) AS repex_reason
            FROM stg_repex x JOIN stg_members m ON m.ref = x.entity_ref
            GROUP BY m.entity_id
        ) r ON r.entity_id = e.entity_id
        """,
        [set_id, set_id],
    )


# --- c. links ---------------------------------------------------------------------------


def _write_links(
    con: duckdb.DuckDBPyConnection, set_id: str, refused: list[tuple[str, str, str]]
) -> None:
    # A refused merge is still a shared identifier: link the two entities at full confidence.
    con.execute("CREATE OR REPLACE TEMP TABLE stg_refused (ref_a TEXT, ref_b TEXT, kind TEXT)")
    if refused:
        con.executemany("INSERT INTO stg_refused VALUES (?, ?, ?)", refused)
    con.execute(
        """
        INSERT INTO silver_entity_links
        SELECT ?, a_id, b_id, min(kind), 1.0, any_value(name_a), any_value(name_b)
        FROM (
            SELECT least(a.entity_id, b.entity_id) AS a_id,
                   greatest(a.entity_id, b.entity_id) AS b_id, x.kind,
                   CASE WHEN a.entity_id < b.entity_id THEN ea.legal_name ELSE eb.legal_name END
                       AS name_a,
                   CASE WHEN a.entity_id < b.entity_id THEN eb.legal_name ELSE ea.legal_name END
                       AS name_b
            FROM stg_refused x
            JOIN stg_members a ON a.ref = x.ref_a
            JOIN stg_members b ON b.ref = x.ref_b
            JOIN stg_entity ea ON ea.entity_id = a.entity_id
            JOIN stg_entity eb ON eb.entity_id = b.entity_id
            WHERE a.entity_id <> b.entity_id
        )
        GROUP BY a_id, b_id
        """,
        [set_id],
    )

    # Names are compared only within a block: a shared word prefix. Two entities
    # that both have an LEI are different entities whatever their names, and so
    # are two whose jurisdictions differ.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE stg_blocks AS
        SELECT DISTINCT entity_id, name, left(token, ?) AS block FROM (
            SELECT entity_id, name, unnest(string_split(name_norm, ' ')) AS token
            FROM silver_entity_names WHERE snapshot_set_id = ?
        ) WHERE token <> ''
        """,
        [BLOCK_PREFIX_LENGTH, set_id],
    )
    con.execute(
        """
        INSERT INTO silver_entity_links
        WITH pairs AS (
            SELECT DISTINCT a.entity_id AS a_id, b.entity_id AS b_id,
                   a.name AS name_a, b.name AS name_b
            FROM stg_blocks a
            JOIN stg_blocks b ON b.block = a.block AND a.entity_id < b.entity_id
            JOIN stg_entity ea ON ea.entity_id = a.entity_id
            JOIN stg_entity eb ON eb.entity_id = b.entity_id
            WHERE NOT (ea.lei IS NOT NULL AND eb.lei IS NOT NULL)
              AND (ea.jurisdiction IS NULL OR eb.jurisdiction IS NULL
                   OR ea.jurisdiction = eb.jurisdiction)
        ), scored AS (
            SELECT *, ubo_name_similarity(name_a, name_b) AS score FROM pairs
        ), best AS (
            SELECT *, row_number() OVER (
                PARTITION BY a_id, b_id ORDER BY score DESC, name_a, name_b
            ) AS rn FROM scored
        )
        SELECT ?, a_id, b_id, 'FUZZY_NAME', score, name_a, name_b
        FROM best
        WHERE rn = 1 AND score >= ?
          AND NOT EXISTS (
              SELECT 1 FROM silver_entity_links l
              WHERE l.snapshot_set_id = ? AND l.entity_id_a = a_id AND l.entity_id_b = b_id
          )
        """,
        [set_id, LINK_FLOOR, set_id],
    )


# --- d. edges ---------------------------------------------------------------------------


def _write_relationships(
    con: duckdb.DuckDBPyConnection, set_id: str, ontology: OntologyConfig
) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE stg_edge_rules (rel_type TEXT, side TEXT, label TEXT)"
    )
    con.executemany(
        "INSERT INTO stg_edge_rules VALUES (?, ?, ?)",
        [
            (rel_type, side, label)
            for rel_type, spec in ontology.edge_types.items()
            for side, end in (("subject", spec.subject), ("object", spec.object))
            for label in end.labels
        ],
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stg_edges AS
        SELECT r.*, s.entity_id AS subject_id, o.entity_id AS object_id,
            CASE
                WHEN s.entity_id IS NULL OR o.entity_id IS NULL THEN '{REJECT_UNRESOLVED_REF}'
                WHEN s.entity_id = o.entity_id THEN '{REJECT_SELF_LOOP}'
                WHEN rs.label IS NULL OR ro.label IS NULL THEN '{REJECT_ENDPOINT_LABEL}'
            END AS reject,
            CASE
                WHEN s.entity_id IS NULL OR o.entity_id IS NULL THEN concat_ws(', ',
                    CASE WHEN s.entity_id IS NULL THEN r.subject_ref END,
                    CASE WHEN o.entity_id IS NULL THEN r.object_ref END)
                WHEN s.entity_id = o.entity_id THEN s.entity_id
                ELSE se.entity_type || ' ' || r.rel_type || ' ' || oe.entity_type
            END AS reject_detail
        FROM stg_relationships r
        LEFT JOIN stg_members s ON s.ref = r.subject_ref
        LEFT JOIN stg_members o ON o.ref = r.object_ref
        LEFT JOIN stg_entity se ON se.entity_id = s.entity_id
        LEFT JOIN stg_entity oe ON oe.entity_id = o.entity_id
        LEFT JOIN stg_edge_rules rs
            ON rs.rel_type = r.rel_type AND rs.side = 'subject' AND rs.label = se.entity_type
        LEFT JOIN stg_edge_rules ro
            ON ro.rel_type = r.rel_type AND ro.side = 'object' AND ro.label = oe.entity_type
        """
    )
    con.execute(
        f"INSERT INTO silver_rejects SELECT ?, dataset, reject, reject_detail, {_PROV}"
        " FROM stg_edges WHERE reject IS NOT NULL",
        [set_id],
    )
    # One edge per id. The newest record that gives a percentage decides it; a
    # later record without one does not erase a known stake.
    con.execute(
        f"""
        INSERT INTO silver_relationships
        WITH accepted AS (
            SELECT *, ubo_relationship_id(subject_id, object_id, rel_type, is_ultimate) AS id
            FROM stg_edges WHERE reject IS NULL
        ), ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY id
                ORDER BY (pct IS NULL), as_of DESC, confidence DESC, source, source_record_id
            ) AS rn FROM accepted
        ), supporting AS (
            SELECT id, to_json(list({_PROV_STRUCT} ORDER BY {_PROV_ORDER})) AS supporting_records
            FROM accepted GROUP BY id
        )
        SELECT ?, w.id, w.subject_id, w.object_id, w.rel_type, w.pct, w.basis, w.is_ultimate,
            {", ".join(f"w.{c.strip()}" for c in _PROV.split(","))}, s.supporting_records
        FROM ranked w JOIN supporting s ON s.id = w.id
        WHERE w.rn = 1
        """,
        [set_id],
    )


# --- e. designations --------------------------------------------------------------------


def _write_sanctions(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE stg_designations AS"
        " SELECT d.*, m.entity_id, m.joined_by"
        " FROM stg_sanctions d LEFT JOIN stg_members m ON m.ref = d.entity_ref"
    )
    con.execute(
        f"INSERT INTO silver_rejects"
        f" SELECT ?, dataset, '{REJECT_UNRESOLVED_REF}', entity_ref, {_PROV}"
        " FROM stg_designations WHERE entity_id IS NULL",
        [set_id],
    )
    # A designation reaches the entity its record belongs to, and each entity
    # linked to that one, one link away and no further.
    con.execute(
        f"""
        INSERT INTO silver_sanctions
        WITH reached AS (
            SELECT d.*, d.entity_id AS reached_id,
                CASE WHEN d.joined_by = '{JOINED_SELF}' THEN 'DIRECT' ELSE d.joined_by END
                    AS match_type,
                1.0 AS match_confidence
            FROM stg_designations d WHERE d.entity_id IS NOT NULL
            UNION ALL
            SELECT d.*,
                CASE WHEN l.entity_id_a = d.entity_id THEN l.entity_id_b ELSE l.entity_id_a END,
                l.match_type, l.confidence
            FROM stg_designations d
            JOIN silver_entity_links l
                ON l.snapshot_set_id = ? AND d.entity_id IN (l.entity_id_a, l.entity_id_b)
        )
        SELECT ?, ubo_sanction_id(source, source_record_id, reached_id), reached_id, program,
            list_date, list_source, is_active, match_type, match_confidence, {_PROV}
        FROM reached
        """,
        [set_id, set_id],
    )


# --- f. reporting exceptions ------------------------------------------------------------


def _write_reporting_exceptions(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    con.execute(
        f"INSERT INTO silver_rejects"
        f" SELECT ?, x.dataset, '{REJECT_UNRESOLVED_REF}', x.entity_ref,"
        f" {', '.join(f'x.{c.strip()}' for c in _PROV.split(','))}"
        " FROM stg_repex x LEFT JOIN stg_members m ON m.ref = x.entity_ref"
        " WHERE m.entity_id IS NULL",
        [set_id],
    )
    con.execute(
        f"INSERT INTO silver_reporting_exceptions"
        f" SELECT ?, m.entity_id, x.category, x.reason,"
        f" {', '.join(f'x.{c.strip()}' for c in _PROV.split(','))}"
        " FROM stg_repex x JOIN stg_members m ON m.ref = x.entity_ref",
        [set_id],
    )


# --- g. set-level gaps ------------------------------------------------------------------


def _write_gaps(con: duckdb.DuckDBPyConnection, set_id: str) -> None:
    # Computed over the whole set: a screen sees only part of the graph.
    con.execute(
        f"""
        INSERT INTO silver_gaps
        SELECT ?, 'PCT_SUM_OVER_100', e.id, to_json({{'sum_pct': CAST(t.total AS VARCHAR)}}),
            {", ".join(f"e.{c.strip()}" for c in _PROV.split(","))}
        FROM (
            SELECT object_id, CAST(sum(pct) AS DECIMAL(18,4)) AS total
            FROM silver_relationships
            WHERE snapshot_set_id = ? AND rel_type = 'OWNS' AND pct IS NOT NULL
            GROUP BY object_id HAVING sum(pct) > 100
        ) t JOIN silver_entities e ON e.snapshot_set_id = ? AND e.id = t.object_id
        """,
        [set_id, set_id, set_id],
    )


# --- h. summary -------------------------------------------------------------------------


def _counts(con: duckdb.DuckDBPyConnection, set_id: str) -> dict[str, Any]:
    def count(table: str, where: str = "") -> int:
        sql = f"SELECT count(*) FROM {table}"
        if table.startswith("silver_"):
            return con.execute(f"{sql} WHERE snapshot_set_id = ? {where}", [set_id]).fetchone()[0]
        return con.execute(sql).fetchone()[0]

    rejects = con.execute(
        "SELECT reason, count(*) FROM silver_rejects WHERE snapshot_set_id = ?"
        " GROUP BY reason ORDER BY reason",
        [set_id],
    ).fetchall()
    return {
        "entity_records": count("stg_entities"),
        "entities": count("silver_entities"),
        "merged_records": count("silver_entity_xref", "AND NOT is_primary"),
        "names": count("silver_entity_names"),
        "links": count("silver_entity_links"),
        "relationship_records": count("stg_relationships"),
        "relationships": count("silver_relationships"),
        "designations": count("stg_sanctions"),
        "sanctions": count("silver_sanctions"),
        "reporting_exceptions": count("silver_reporting_exceptions"),
        "gaps": count("silver_gaps"),
        "rejects": dict(rejects),
    }
