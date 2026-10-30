"""What changed between two snapshot sets of one family, in the fields the rules read.

The diff reads the built graph (Gold) and Silver, not Bronze. A designation's
`is_active` is derived from the publish date (OpenSanctions' `endDate` against
`_as_of`), so a designation can lapse with its source row unchanged; a list
record that starts to state an LEI moves a designation to another entity with
no sanctions row changed. Both show only after the mapper. Provenance
(`snapshot_id`, `as_of`) differs between any two sets and is never compared.

Both sets must be built by the same code: a diff across a change of
transform would report the code's work as the data's.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import duckdb

from ubo_sentinel.models.canonical import quantize, sha256_hex, short_id
from ubo_sentinel.models.change import Cause, Change, Domain, SetDiff
from ubo_sentinel.pipeline import gold, silver
from ubo_sentinel.pipeline.bronze import SnapshotSet, resolve_set


class DiffRefused(ValueError):
    """The two sets cannot be compared: other families, or built by other code."""


class SetNotBuilt(LookupError):
    """A set is not built up to Gold. The message names the command to run."""


@dataclass(frozen=True)
class _Table:
    domain: Domain
    table: str
    # A single SQL expression: the row's key within the set.
    key: str
    # The fields compared, as SQL expressions with their names.
    compared: tuple[tuple[str, str], ...]
    # SQL expressions naming the entities a row is about: one entity's history
    # (`diff_entity`) is the rows where one of them is that entity.
    about: tuple[str, ...]
    # Read alongside, never compared.
    info: tuple[tuple[str, str], ...] = ()
    where: str = "TRUE"


_DESIGNATION_FIELDS = ("program", "list_date", "list_source", "is_active")
_MATCH_FIELDS = ("node_id", "designated_node_id", "match_type", "match_confidence")

_TABLES = (
    _Table(
        "designation",
        "graph_sanctions",
        "id",
        tuple((name, name) for name in (*_MATCH_FIELDS, *_DESIGNATION_FIELDS)),
        about=("node_id", "designated_node_id"),
        info=(("source", "source"), ("source_record_id", "source_record_id")),
    ),
    _Table(
        "edge",
        "graph_edges",
        "id",
        tuple(
            (name, name)
            for name in ("upper_id", "lower_id", "rel_type", "pct", "basis", "is_ultimate")
        ),
        about=("upper_id", "lower_id"),
    ),
    # Each link once, from its smaller end.
    _Table(
        "link",
        "graph_links",
        "node_id || '|' || linked_node_id",
        (
            ("node_id", "node_id"),
            ("linked_node_id", "linked_node_id"),
            ("match_type", "match_type"),
            ("confidence", "confidence"),
        ),
        about=("node_id", "linked_node_id"),
        where="node_id < linked_node_id",
    ),
    # Names are left out: a new alias shows as the link it makes, and a renamed
    # record changes no rule.
    _Table(
        "entity",
        "graph_nodes",
        "id",
        (
            ("entity_type", "entity_type"),
            ("jurisdiction", "jurisdiction"),
            ("status", "status"),
            ("lei_status", "lei_status"),
            ("repex_reason", "repex_reason"),
            ("has_upstream", "up_degree > 0"),
        ),
        about=("id",),
    ),
    _Table(
        "exception",
        "silver_reporting_exceptions",
        "source || '|' || source_record_id",
        (("entity_id", "entity_id"), ("category", "category"), ("reason", "reason")),
        about=("entity_id",),
    ),
    _Table(
        "set_gap",
        "silver_gaps",
        "code || '|' || subject_id",
        (("code", "code"), ("subject_id", "subject_id")),
        about=("subject_id",),
    ),
)


def check_comparable(con: duckdb.DuckDBPyConnection, prev: SnapshotSet, curr: SnapshotSet) -> None:
    """Raise unless the two sets can be diffed: one family, both built, by the same code."""
    if prev.family != curr.family:
        raise DiffRefused(
            f"Set {prev.snapshot_set_id} is of the {prev.family} family and"
            f" {curr.snapshot_set_id} of the {curr.family} family: a diff compares one family."
        )
    for snapshot_set in (prev, curr):
        set_id = snapshot_set.snapshot_set_id
        if not silver.is_built(con, set_id):
            raise SetNotBuilt(
                f"Set {set_id} is not normalized: run `ubo normalize --snapshot {set_id}`."
            )
        if not gold.is_built(con, set_id):
            raise SetNotBuilt(
                f"Set {set_id} has no current graph: run `ubo build-graph --snapshot {set_id}`."
            )
    built = {s.snapshot_set_id: silver.load_build(con, s.snapshot_set_id) for s in (prev, curr)}
    a, b = built[prev.snapshot_set_id], built[curr.snapshot_set_id]
    if (a["engine_version"], a["params"]) != (b["engine_version"], b["params"]):
        raise DiffRefused(
            f"Sets {prev.snapshot_set_id} and {curr.snapshot_set_id} were built by other code:"
            " a diff would report the code's change as the data's. Build the older one again"
            f" with this code: `ubo normalize --rebuild --snapshot {prev.snapshot_set_id}`,"
            f" then `ubo build-graph --snapshot {prev.snapshot_set_id}`."
        )


def diff_sets(con: duckdb.DuckDBPyConnection, prev_set: str, curr_set: str) -> SetDiff:
    """Every change between two built sets of one family, in a stable order.

    Raises `UnknownSnapshotSet`, `SetNotBuilt` and `DiffRefused`.
    """
    prev, curr = resolve_set(con, prev_set), resolve_set(con, curr_set)
    check_comparable(con, prev, curr)
    a, b = prev.snapshot_set_id, curr.snapshot_set_id
    changes: list[Change] = []
    for spec in _TABLES:
        rows = _compare(con, spec, a, b)
        changes += _changes(con, spec, rows, a, b)
    changes.sort(key=lambda c: (c.domain, c.key, c.kind))
    return SetDiff(
        prev_set_id=a,
        curr_set_id=b,
        family=curr.family,
        changes=changes,
        diff_digest=sha256_hex([a, b, changes]),
    )


def diff_entity(
    con: duckdb.DuckDBPyConnection, prev_set: str, curr_set: str, entity_id: str
) -> list[Change]:
    """The changes between two built sets in the rows about one entity, in a stable order.

    The same fields and causes as `diff_sets`, read only where the entity is
    one of a row's ends (`_Table.about`). Raises as `diff_sets` does.
    """
    prev, curr = resolve_set(con, prev_set), resolve_set(con, curr_set)
    check_comparable(con, prev, curr)
    a, b = prev.snapshot_set_id, curr.snapshot_set_id
    changes: list[Change] = []
    for spec in _TABLES:
        changes += _changes(con, spec, _compare(con, spec, a, b, entity_id), a, b)
    return sorted(changes, key=lambda c: (c.domain, c.key, c.kind))


def _compare(
    con: duckdb.DuckDBPyConnection, spec: _Table, a: str, b: str, about: str | None = None
) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
    """(key, row in a, row in b) for every key whose compared fields differ, in key order.

    With `about`, only the rows about that entity.
    """
    fields = [*spec.compared, *spec.info]
    select = ", ".join(f"{expr} AS {name}" for name, expr in fields)
    where = spec.where
    if about is not None:
        where += " AND $about IN (" + ", ".join(spec.about) + ")"
    side = (
        f"SELECT {spec.key} AS k, {select} FROM {spec.table}"
        f" WHERE snapshot_set_id = $set AND {where}"
    )
    differs = " OR ".join(f"a.{name} IS DISTINCT FROM b.{name}" for name, _ in spec.compared)
    columns = ", ".join(f"a.{name}, b.{name}" for name, _ in fields)
    found = con.execute(
        f"""
        WITH a AS ({side.replace("$set", "$a")}), b AS ({side.replace("$set", "$b")})
        SELECT coalesce(a.k, b.k) AS k, a.k IS NOT NULL, b.k IS NOT NULL, {columns}
        FROM a FULL OUTER JOIN b ON a.k = b.k
        WHERE a.k IS NULL OR b.k IS NULL OR {differs}
        ORDER BY 1
        """,
        {"a": a, "b": b} | ({} if about is None else {"about": about}),
    ).fetchall()
    rows = []
    for key, in_a, in_b, *values in found:
        before = {name: _plain(values[2 * i]) for i, (name, _) in enumerate(fields)}
        after = {name: _plain(values[2 * i + 1]) for i, (name, _) in enumerate(fields)}
        rows.append((key, before if in_a else None, after if in_b else None))
    return rows


def _plain(value: Any) -> Any:
    """A value as JSON holds it."""
    if isinstance(value, Decimal):
        return str(quantize(value))
    if isinstance(value, date):
        return value.isoformat()
    return value


def _changes(
    con: duckdb.DuckDBPyConnection,
    spec: _Table,
    rows: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]],
    a: str,
    b: str,
) -> list[Change]:
    compared = [name for name, _ in spec.compared]
    records = _designation_records(con, a, b) if spec.domain == "designation" else {}
    changes = []
    for key, before, after in rows:
        kind = "CHANGED" if before and after else "ADDED" if after else "REMOVED"
        fields = (
            sorted(name for name in compared if before[name] != after[name])
            if kind == "CHANGED"
            else []
        )
        rows_ = [row for row in (before, after) if row is not None]
        anchors = set()
        cause: Cause
        if spec.domain == "designation":
            anchors = {row["node_id"] for row in rows_}
            if kind == "CHANGED":
                cause = "LIST" if set(fields) & set(_DESIGNATION_FIELDS) else "LINK"
            else:
                # A record that both sets hold, reaching another entity, is a link's work.
                row = before or after
                other = b if kind == "REMOVED" else a
                held = (row["source"], row["source_record_id"]) in records[other]
                cause = "LINK" if held else "LIST"
        elif spec.domain == "edge":
            anchors, cause = {row["lower_id"] for row in rows_}, "OWNERSHIP"
        elif spec.domain == "link":
            anchors = {row[end] for row in rows_ for end in ("node_id", "linked_node_id")}
            cause = "LINK"
        elif spec.domain == "entity":
            anchors, cause = {key}, "ENTITY"
            # A record that gains or loses owners of its own changes the screens of
            # the records linked to it (`POSSIBLE_DUPLICATE`).
            if "has_upstream" in fields:
                anchors |= _linked(con, key, a, b)
        elif spec.domain == "exception":
            anchors, cause = {row["entity_id"] for row in rows_}, "ENTITY"
        else:
            anchors, cause = {row["subject_id"] for row in rows_}, "OWNERSHIP"
        changes.append(
            Change(
                change_id=short_id([spec.domain, key, kind]),
                domain=spec.domain,
                kind=kind,
                cause=cause,
                key=key,
                anchor_ids=sorted(anchors),
                fields=fields,
                before=_compared(before, compared),
                after=_compared(after, compared),
            )
        )
    return changes


def _compared(row: dict[str, Any] | None, compared: list[str]) -> dict[str, Any] | None:
    return None if row is None else {name: row[name] for name in compared}


def _designation_records(
    con: duckdb.DuckDBPyConnection, a: str, b: str
) -> dict[str, set[tuple[str, str]]]:
    """Set -> the designation records (source, record id) it holds."""
    found: dict[str, set[tuple[str, str]]] = {a: set(), b: set()}
    for set_id, source, record in con.execute(
        "SELECT DISTINCT snapshot_set_id, source, source_record_id FROM graph_sanctions"
        " WHERE snapshot_set_id IN (?, ?)",
        [a, b],
    ).fetchall():
        found[set_id].add((source, record))
    return found


def _linked(con: duckdb.DuckDBPyConnection, node_id: str, a: str, b: str) -> set[str]:
    return {
        linked
        for (linked,) in con.execute(
            "SELECT DISTINCT linked_node_id FROM graph_links"
            " WHERE snapshot_set_id IN (?, ?) AND node_id = ?",
            [a, b, node_id],
        ).fetchall()
    }
