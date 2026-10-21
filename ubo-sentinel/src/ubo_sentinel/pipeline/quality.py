"""The data-quality report of a refresh, and the guard that keeps a shrunken feed off `real`.

`ubo refresh` builds a set, then reports what its sources held and what the
pipeline made of them: rows and rejects per dataset, records a mapper left
out, links by kind and score, designation records that name no entity, and how
many list companies joined the registry. It compares that with the last set
of the family it accepted.

A truncated file or a changed upstream format looks, downstream, like mass
delistings, and re-screening on it would clear what is still designated. So
when the entities or the active designations fall by more than the shrink
limit, the set is not accepted and the `real` alias stays where it was, unless
someone accepts the fall and says why. Each check is recorded in
`quality_reports`; the report is derived from the set and can be made again,
the acceptance cannot.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from ubo_sentinel.models.canonical import canonical_json
from ubo_sentinel.pipeline import silver
from ubo_sentinel.pipeline.bronze import QUALITY_TABLE, SnapshotSet
from ubo_sentinel.pipeline.entity_linking import LINK_FLOOR
from ubo_sentinel.pipeline.silver import load_build

# Share by which a guarded count may fall from the last accepted set before the
# new one is refused. `ubo download` holds each file's size to the same limit.
SHRINK_LIMIT_ENV_VAR = "UBO_SHRINK_LIMIT"
DEFAULT_SHRINK_LIMIT = 0.20

# Counts whose fall refuses a set: what a truncated list or registry lowers.
GUARDED = ("entities", "active_designations")

# Counts compared with the last accepted set, besides each dataset's rows.
COMPARED = (
    "entities",
    "active_designations",
    "relationships",
    "links",
    "targets_without_designation",
)

# `ubo_sentinel.pipeline.sources` names; the report reads them from the rows.
_REGISTRY_SOURCE = "gleif"
_LIST_SOURCE = "opensanctions"

# Width of a bin of the name-link score histogram.
_SCORE_BIN = 0.05

_DDL = f"""
CREATE TABLE IF NOT EXISTS {QUALITY_TABLE} (
    snapshot_set_id TEXT NOT NULL,
    family TEXT NOT NULL,
    compared_with TEXT,
    report JSON NOT NULL,
    shrunk TEXT[] NOT NULL,
    accepted BOOLEAN NOT NULL,
    reason TEXT,
    checked_at TIMESTAMP NOT NULL
)
"""


class QualityError(ValueError):
    """The set cannot be checked (it is not built)."""


@dataclass(frozen=True)
class QualityCheck:
    report: dict[str, Any]
    # The set the report is compared with: the last accepted set of the family.
    compared_with: str | None
    # Guarded counts that fell by more than the limit.
    shrunk: list[str]
    accepted: bool
    reason: str | None


def shrink_limit() -> float:
    value = os.environ.get(SHRINK_LIMIT_ENV_VAR)
    return DEFAULT_SHRINK_LIMIT if value is None else float(value)


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_DDL)


def quality_report(con: duckdb.DuckDBPyConnection, snapshot_set: SnapshotSet) -> dict[str, Any]:
    """What the set's sources held and what Silver made of them. The set must be built."""
    set_id = snapshot_set.snapshot_set_id
    silver.create_schema(con)
    build = load_build(con, set_id)
    if build is None:
        raise QualityError(f"Snapshot set {set_id} has no Silver build; run 'ubo normalize'.")
    counts = build["counts"]

    def scalar(sql: str, *params: Any) -> int:
        return con.execute(sql, [set_id, *params]).fetchone()[0]

    datasets = {}
    for name, snapshot_id in sorted(snapshot_set.snapshot_ids.items()):
        rows = con.execute(
            "SELECT row_count FROM bronze_snapshots WHERE snapshot_id = ?", [snapshot_id]
        ).fetchone()[0]
        rejects = con.execute(
            "SELECT split_part(reason, ':', 1) AS r, count(*) FROM bronze_rejects"
            " WHERE snapshot_id = ? GROUP BY r ORDER BY r",
            [snapshot_id],
        ).fetchall()
        datasets[name] = {"rows": rows, "rejects": dict(rejects)}

    by_type = con.execute(
        "SELECT match_type, count(*) FROM silver_entity_links WHERE snapshot_set_id = ?"
        " GROUP BY match_type ORDER BY match_type",
        [set_id],
    ).fetchall()
    # Bins of name-link scores, labelled by their lower edge.
    scores = con.execute(
        "SELECT printf('%.2f', greatest(floor(confidence / ? + 1e-9) * ?, ?)) AS bin, count(*)"
        " FROM silver_entity_links WHERE snapshot_set_id = ? AND match_type = 'FUZZY_NAME'"
        " GROUP BY bin ORDER BY bin",
        [_SCORE_BIN, _SCORE_BIN, LINK_FLOOR, set_id],
    ).fetchall()

    return {
        "snapshot_set_id": set_id,
        "family": snapshot_set.family,
        "datasets": datasets,
        "entities": counts["entities"],
        "relationships": counts["relationships"],
        "designations": counts["designations"],
        # Designation records in force, however many entities each reached.
        "active_designations": scalar(
            "SELECT count(*) FROM (SELECT DISTINCT source, source_record_id"
            " FROM silver_sanctions WHERE snapshot_set_id = ? AND is_active)"
        ),
        "links": counts["links"],
        "links_by_match_type": dict(by_type),
        "name_link_scores": dict(scores),
        "link_blocks_skipped": counts["link_blocks_skipped"],
        "rejects": counts["rejects"],
        "filtered": counts["filtered"],
        "unreadable": counts.get("unreadable", {}),
        "targets_without_designation": _targets_without_designation(con, snapshot_set),
        "list_companies_joined": _list_companies_joined(con, set_id),
    }


def _targets_without_designation(
    con: duckdb.DuckDBPyConnection, snapshot_set: SnapshotSet
) -> int | None:
    """List records marked as targets that no designation record names. None without a list.

    Silver has nothing to stage for them, so they are counted from Bronze.
    """
    ids = snapshot_set.snapshot_ids
    if "os_entities" not in ids or "os_sanctions" not in ids:
        return None
    return con.execute(
        """
        SELECT count(*) FROM bronze_entities e
        WHERE e._snapshot_id = ? AND json_extract_string(e._raw, '$.target') = 'true'
          AND e._source_record_id NOT IN (
              SELECT unnest(CAST(json_extract(s._raw, '$.properties.entity') AS VARCHAR[]))
              FROM bronze_sanctions s
              WHERE s._snapshot_id = ?
                AND json_extract(s._raw, '$.properties.entity') IS NOT NULL
          )
        """,
        [ids["os_entities"], ids["os_sanctions"]],
    ).fetchone()[0]


def _list_companies_joined(con: duckdb.DuckDBPyConnection, set_id: str) -> dict[str, int]:
    """List records of legal entities, by how each reached a registry record.

    By the identifier it shares (`LEI`, `SUCCESSOR_LEI`, `REGISTRATION`): one
    entity with a registry record. Else by the best link (`FUZZY_NAME`, or an
    identifier a merge guard refused) to an entity with one. Else `NONE`.
    """
    rows = con.execute(
        """
        WITH registry AS (
            SELECT DISTINCT entity_id FROM silver_entity_xref
            WHERE snapshot_set_id = ? AND source = ?
        ), listed AS (
            SELECT x.entity_id, x.joined_by FROM silver_entity_xref x
            JOIN silver_entities e
              ON e.snapshot_set_id = x.snapshot_set_id AND e.id = x.entity_id
            WHERE x.snapshot_set_id = ? AND x.source = ? AND e.entity_type = 'LegalEntity'
        ), linked AS (
            SELECT l.entity_id_a AS entity_id, l.entity_id_b AS other, l.match_type,
                   l.confidence
            FROM silver_entity_links l WHERE l.snapshot_set_id = ?
            UNION ALL
            SELECT l.entity_id_b, l.entity_id_a, l.match_type, l.confidence
            FROM silver_entity_links l WHERE l.snapshot_set_id = ?
        ), best_link AS (
            SELECT k.entity_id, arg_max(k.match_type, k.confidence) AS match_type
            FROM linked k JOIN registry r ON r.entity_id = k.other
            GROUP BY k.entity_id
        )
        SELECT CASE
                   WHEN s.joined_by <> 'SELF' AND r.entity_id IS NOT NULL THEN s.joined_by
                   WHEN b.match_type IS NOT NULL THEN b.match_type
                   ELSE 'NONE'
               END AS how, count(*)
        FROM listed s
        LEFT JOIN registry r ON r.entity_id = s.entity_id
        LEFT JOIN best_link b ON b.entity_id = s.entity_id
        GROUP BY how ORDER BY how
        """,
        [set_id, _REGISTRY_SOURCE, set_id, _LIST_SOURCE, set_id, set_id],
    ).fetchall()
    return dict(rows)


def last_accepted(con: duckdb.DuckDBPyConnection, family: str, excluding: str) -> str | None:
    """The newest set of the family a check accepted, other than `excluding`."""
    create_schema(con)
    row = con.execute(
        f"""
        SELECT i.snapshot_set_id FROM snapshot_set_info i
        WHERE i.family = ? AND i.snapshot_set_id <> ?
          AND EXISTS (SELECT 1 FROM {QUALITY_TABLE} q
                      WHERE q.snapshot_set_id = i.snapshot_set_id AND q.accepted)
        ORDER BY i.seq DESC LIMIT 1
        """,
        [family, excluding],
    ).fetchone()
    return row[0] if row else None


def _stored_report(con: duckdb.DuckDBPyConnection, set_id: str) -> dict[str, Any]:
    (report,) = con.execute(
        f"SELECT report FROM {QUALITY_TABLE} WHERE snapshot_set_id = ? AND accepted"
        " ORDER BY checked_at DESC LIMIT 1",
        [set_id],
    ).fetchone()
    return json.loads(report)


def changes(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each compared count: the last accepted set's, this set's, and the change as a share."""

    def counts(report: dict[str, Any]) -> dict[str, int | None]:
        values = {name: report.get(name) for name in COMPARED}
        values |= {f"rows.{name}": d["rows"] for name, d in report["datasets"].items()}
        return values

    now, before = counts(current), counts(previous)
    out = {}
    for name in sorted(now.keys() | before.keys()):
        old, new = before.get(name), now.get(name)
        change = None if not old or new is None else round((new - old) / old, 4)
        out[name] = {"previous": old, "current": new, "change": change}
    return out


def check(
    con: duckdb.DuckDBPyConnection,
    snapshot_set: SnapshotSet,
    accept_shrink: bool = False,
    reason: str | None = None,
) -> QualityCheck:
    """Report on a built set, compare it with the last accepted one, and record the outcome.

    The set is accepted unless a guarded count fell by more than the limit;
    `accept_shrink` accepts it anyway, and then `reason` is required.
    """
    if accept_shrink and not (reason and reason.strip()):
        raise QualityError("Accepting a shrunken set needs a reason.")
    create_schema(con)
    set_id = snapshot_set.snapshot_set_id
    report = quality_report(con, snapshot_set)
    compared_with = last_accepted(con, snapshot_set.family, set_id)
    shrunk: list[str] = []
    if compared_with is not None:
        report["changes"] = changes(report, _stored_report(con, compared_with))
        limit = shrink_limit()
        shrunk = [
            name
            for name in GUARDED
            if (c := report["changes"][name])["change"] is not None and c["change"] < -limit
        ]
    accepted = not shrunk or accept_shrink
    con.execute(
        f"INSERT INTO {QUALITY_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            set_id,
            snapshot_set.family,
            compared_with,
            canonical_json(report).decode("utf-8"),
            shrunk,
            accepted,
            reason if shrunk and accept_shrink else None,
            datetime.now(UTC).replace(tzinfo=None),
        ],
    )
    return QualityCheck(
        report=report,
        compared_with=compared_with,
        shrunk=shrunk,
        accepted=accepted,
        reason=reason if shrunk and accept_shrink else None,
    )
