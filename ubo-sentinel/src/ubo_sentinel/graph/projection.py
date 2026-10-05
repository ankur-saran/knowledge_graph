"""Ego-graph extraction: the part of the graph one screen needs.

The full graph stays in DuckDB. A subgraph is found with one indexed query per
hop and only the result is turned into models.
"""

import json
from typing import Any

from ubo_sentinel.graph.store import GraphStore, id_list
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.graph import EntityLink, SetGap, SubGraph
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.models.relationship import Relationship
from ubo_sentinel.models.reporting_exception import ReportingException
from ubo_sentinel.pipeline.silver import fold_provenance

# How many context nodes below the target a subgraph holds at most.
MAX_DOWN_NODES = 200

_PROVENANCE = ", ".join(Provenance.model_fields)
_NODE_COLUMNS = (
    "id, lei, registration_authority_id, registration_number, legal_name, aliases,"
    f" jurisdiction, status, entity_type, repex_reason, {_PROVENANCE}, supporting_records"
)
_EDGE_COLUMNS = (
    "id, subject_id, object_id, rel_type, pct, basis, is_ultimate,"
    f" {_PROVENANCE}, supporting_records"
)


class UnknownEntity(LookupError):
    pass


def read_nodes(store: GraphStore, ids: list[str]) -> list[Entity]:
    rows = store.rows(
        f"SELECT {_NODE_COLUMNS} FROM graph_nodes"
        f" WHERE snapshot_set_id = ? AND id IN {id_list()} ORDER BY id",
        [store.set_id, ids],
    )
    return [Entity.model_validate(fold_provenance(row)) for row in rows]


def read_edges(store: GraphStore, where: str, parameters: list[Any]) -> list[Relationship]:
    """The edges of the store's set that satisfy `where`, a condition on `graph_edges`.

    `where` names its parameters `$2`, `$3`, ...; `$1` is the set id.
    """
    rows = store.rows(
        f"SELECT {_EDGE_COLUMNS} FROM graph_edges WHERE snapshot_set_id = $1 AND ({where})"
        " ORDER BY id",
        [store.set_id, *parameters],
    )
    return [Relationship.model_validate(fold_provenance(row)) for row in rows]


def _neighbours(store: GraphStore, frontier: list[str], end: str) -> list[str]:
    """The nodes one hop from `frontier`: `end` is the side of the edge to fetch."""
    other = "lower_id" if end == "upper_id" else "upper_id"
    rows = store.con.execute(
        f"SELECT DISTINCT {end} FROM graph_edges"
        f" WHERE snapshot_set_id = ? AND {other} IN {id_list()} ORDER BY {end}",
        [store.set_id, frontier],
    ).fetchall()
    return [node_id for (node_id,) in rows]


def extract_ego_graph(
    store: GraphStore,
    entity_id: str,
    max_depth_up: int,
    max_depth_down: int,
    max_down_nodes: int = MAX_DOWN_NODES,
) -> SubGraph:
    """The target, its owners and controllers up to `max_depth_up` hops, and some context below.

    Upward, every relation is followed, direct and ultimate; an ultimate-parent
    edge is one hop. Raises `UnknownEntity`.
    """
    if not read_nodes(store, [entity_id]):
        raise UnknownEntity(f"No entity '{entity_id}' in snapshot set {store.set_id}.")

    # Upward: one query per hop. A node is placed once, so a cycle ends.
    depth_up = {entity_id: 0}
    frontier = [entity_id]
    for depth in range(1, max_depth_up + 1):
        frontier = [n for n in _neighbours(store, frontier, "upper_id") if n not in depth_up]
        if not frontier:
            break
        depth_up.update(dict.fromkeys(frontier, depth))
    up_ids = sorted(depth_up)

    # `frontier` is now the nodes at the depth limit, or empty when the top was
    # reached first. An owner that is already in the subgraph is not a truncation.
    truncated_at = [
        node_id
        for (node_id,) in store.con.execute(
            "SELECT DISTINCT lower_id FROM graph_edges WHERE snapshot_set_id = ?"
            f" AND lower_id IN {id_list()} AND upper_id NOT IN {id_list()}",
            [store.set_id, frontier, up_ids],
        ).fetchall()
    ]

    # Downward, from the target only: context, capped so that a parent with
    # thousands of subsidiaries does not stall a screen.
    down_ids: list[str] = []
    down_truncated = False
    frontier = [entity_id]
    for _ in range(max_depth_down):
        found = [
            n
            for n in _neighbours(store, frontier, "lower_id")
            if n not in depth_up and n not in down_ids
        ]
        room = max_down_nodes - len(down_ids)
        if len(found) > room:
            found, down_truncated = found[:room], True
        if not found:
            break
        down_ids.extend(found)
        frontier = found

    # Every edge between two upward nodes, so the edge that closes a cycle is
    # present; below the target, the edges the walk went down.
    edges = read_edges(
        store,
        f"(lower_id IN {id_list('$2')} AND upper_id IN {id_list('$2')})"
        f" OR (lower_id IN {id_list('$3')} AND upper_id IN {id_list('$4')})",
        [up_ids, down_ids, [entity_id, *down_ids]],
    )

    return SubGraph(
        target_id=entity_id,
        snapshot_set_id=store.set_id,
        max_depth_up=max_depth_up,
        max_depth_down=max_depth_down,
        nodes=read_nodes(store, [*up_ids, *down_ids]),
        edges=edges,
        depth_up=depth_up,
        down_ids=down_ids,
        truncated_at=truncated_at,
        down_truncated=down_truncated,
        reporting_exceptions=read_reporting_exceptions(store, up_ids),
        set_gaps=_set_gaps(store, up_ids),
        links=_links(store, up_ids),
    )


def read_reporting_exceptions(store: GraphStore, ids: list[str]) -> list[ReportingException]:
    rows = store.rows(
        f"SELECT entity_id, category, reason, {_PROVENANCE} FROM silver_reporting_exceptions"
        f" WHERE snapshot_set_id = ? AND entity_id IN {id_list()}"
        " ORDER BY source, source_record_id",
        [store.set_id, ids],
    )
    return [ReportingException.model_validate(fold_provenance(row)) for row in rows]


def _set_gaps(store: GraphStore, ids: list[str]) -> list[SetGap]:
    rows = store.rows(
        f"SELECT code, subject_id, detail, {_PROVENANCE} FROM silver_gaps"
        f" WHERE snapshot_set_id = ? AND subject_id IN {id_list()}"
        " ORDER BY code, subject_id",
        [store.set_id, ids],
    )
    return [
        SetGap.model_validate(fold_provenance({**row, "detail": json.loads(row["detail"])}))
        for row in rows
    ]


def _links(store: GraphStore, ids: list[str]) -> list[EntityLink]:
    rows = store.rows(
        "SELECT l.node_id, l.linked_node_id, l.match_type, l.confidence, l.linked_name,"
        " n.up_degree > 0 AS linked_has_upstream"
        " FROM graph_links l JOIN graph_nodes n"
        " ON n.snapshot_set_id = l.snapshot_set_id AND n.id = l.linked_node_id"
        f" WHERE l.snapshot_set_id = ? AND l.node_id IN {id_list()}"
        " ORDER BY l.node_id, l.linked_node_id",
        [store.set_id, ids],
    )
    return [EntityLink.model_validate(row) for row in rows]
