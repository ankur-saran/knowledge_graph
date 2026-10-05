"""The allow-listed query API. Agents, CLI and UI read the graph only through here.

Every function is registered in `QUERIES` by `@query`; there are no free-form
queries. Each takes a `GraphStore`, which fixes the snapshot set it reads.
"""

import inspect
import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from ubo_sentinel.graph import entity_resolution, projection
from ubo_sentinel.graph.store import GraphStore, QueryCall, id_list
from ubo_sentinel.models.entity import Entity
from ubo_sentinel.models.graph import (
    EntityMatch,
    OwnerEdge,
    RepexRecord,
    SanctionMatch,
    SubGraph,
    upper_lower,
)
from ubo_sentinel.models.provenance import Provenance
from ubo_sentinel.models.sanction import Sanction
from ubo_sentinel.pipeline.bronze import BRONZE_TABLES
from ubo_sentinel.pipeline.silver import fold_provenance

__all__ = [
    "QUERIES",
    "get_affected_entities",
    "get_direct_owners",
    "get_entity_by_id",
    "get_ownership_subgraph",
    "get_reporting_exceptions",
    "get_sanctioned_nodes",
    "get_source_record",
    "resolve_entity",
]


@dataclass(frozen=True)
class QuerySpec:
    name: str
    function: Callable[..., Any]
    # The typed signature, without the store.
    signature: inspect.Signature


# Name -> query. This is the allow-list.
QUERIES: dict[str, QuerySpec] = {}


def query[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Register a function as an allow-listed query and log each call on its store."""

    @wraps(function)
    def call(store: GraphStore, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        result = function(store, *args, **kwargs)
        rows = len(result) if isinstance(result, list) else int(result is not None)
        store.calls.append(QueryCall(function.__name__, rows, time.perf_counter() - started))
        return result

    signature = inspect.signature(function)
    parameters = list(signature.parameters.values())[1:]
    QUERIES[function.__name__] = QuerySpec(
        function.__name__, call, signature.replace(parameters=parameters)
    )
    return call


@query
def resolve_entity(
    store: GraphStore,
    query: str,
    lei: str | None = None,
    registration_number: str | None = None,
    jurisdiction: str | None = None,
    max_candidates: int = 10,
) -> list[EntityMatch]:
    """Candidates for a name or an LEI, best first. Empty when nothing resembles it."""
    return entity_resolution.resolve(
        store, query, lei, registration_number, jurisdiction, max_candidates
    )


@query
def get_ownership_subgraph(
    store: GraphStore, entity_id: str, max_depth_up: int, max_depth_down: int
) -> SubGraph:
    """The entity, its owners and controllers up to `max_depth_up` hops, and context below."""
    return projection.extract_ego_graph(store, entity_id, max_depth_up, max_depth_down)


@query
def get_sanctioned_nodes(store: GraphStore, subgraph: SubGraph) -> list[SanctionMatch]:
    """The designations that reach a node of the subgraph, active or not.

    One match per node and designated entity. Which matches block is for the
    rule engine to say.
    """
    rows = store.rows(
        "SELECT id, node_id AS entity_id, designated_node_id AS designated_entity_id, program,"
        " list_date, list_source, is_active, match_type, match_confidence,"
        f" {', '.join(Provenance.model_fields)} FROM graph_sanctions"
        f" WHERE snapshot_set_id = ? AND node_id IN {id_list()} ORDER BY id",
        [store.set_id, [node.id for node in subgraph.nodes]],
    )
    groups: dict[tuple[str, str], list[Sanction]] = defaultdict(list)
    for row in rows:
        sanction = Sanction.model_validate(fold_provenance(row))
        groups[sanction.entity_id, sanction.designated_entity_id].append(sanction)
    matches = [
        SanctionMatch(
            entity_id=entity_id,
            designated_entity_id=designated_id,
            match_type=records[0].match_type,
            confidence=records[0].match_confidence,
            is_active=any(record.is_active for record in records),
            sanction_records=records,
        )
        for (entity_id, designated_id), records in groups.items()
    ]
    return sorted(matches, key=SanctionMatch.sort_key)


@query
def get_reporting_exceptions(store: GraphStore, entity_id: str) -> list[RepexRecord]:
    """Why the entity does not report a parent, if it says."""
    return projection.read_reporting_exceptions(store, [entity_id])


@query
def get_entity_by_id(store: GraphStore, entity_id: str) -> Entity | None:
    found = projection.read_nodes(store, [entity_id])
    return found[0] if found else None


@query
def get_direct_owners(store: GraphStore, entity_id: str) -> list[OwnerEdge]:
    """The owners, parents and controllers one hop above the entity."""
    edges = projection.read_edges(store, "lower_id = $2", [entity_id])
    upper = {edge.id: upper_lower(edge)[0] for edge in edges}
    owners = {node.id: node for node in projection.read_nodes(store, sorted(set(upper.values())))}
    found = [OwnerEdge(owner=owners[upper[edge.id]], relationship=edge) for edge in edges]
    return sorted(found, key=lambda item: (item.owner.id, item.relationship.id))


@query
def get_affected_entities(store: GraphStore, changed_node_id: str, max_depth: int) -> list[str]:
    """Every entity a change to this node's designations could reach.

    The node itself, the entities linked to it (a designation crosses one
    link), and everything they own or control down to `max_depth` hops.
    """
    linked = store.con.execute(
        "SELECT linked_node_id FROM graph_links WHERE snapshot_set_id = ? AND node_id = ?",
        [store.set_id, changed_node_id],
    ).fetchall()
    frontier = sorted({changed_node_id, *(node_id for (node_id,) in linked)})
    reached = set(frontier)
    for _ in range(max_depth):
        below = store.con.execute(
            "SELECT DISTINCT lower_id FROM graph_edges"
            f" WHERE snapshot_set_id = ? AND upper_id IN {id_list()}",
            [store.set_id, frontier],
        ).fetchall()
        frontier = sorted({node_id for (node_id,) in below} - reached)
        if not frontier:
            break
        reached.update(frontier)
    return sorted(reached)


@query
def get_source_record(store: GraphStore, provenance: Provenance) -> dict[str, Any] | None:
    """The source record behind a citation, as published. None when there is no such record."""
    table = store.con.execute(
        "SELECT table_name FROM bronze_snapshots WHERE snapshot_id = ?", [provenance.snapshot_id]
    ).fetchone()
    if table is None or table[0] not in BRONZE_TABLES:
        return None
    row = store.con.execute(
        f"SELECT _raw FROM {table[0]}"
        " WHERE _snapshot_id = ? AND _source = ? AND _source_record_id = ?",
        [provenance.snapshot_id, provenance.source, provenance.source_record_id],
    ).fetchone()
    return json.loads(row[0]) if row else None
